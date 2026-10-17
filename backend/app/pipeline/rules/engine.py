"""The rule engine (Docs/backend_plan.md, Step 11).

Pure functions over grounded claims: the same claims and parameters always
give the same findings, with no model involved. All arithmetic is Decimal.

Documents are grouped by order. A purchase order is identified by its
number; a delivery note or invoice belongs to the order it references, or
to the only purchase order in the case when it references none.

Templates (Docs/Integrations_API_Contract.md §4):

- ``quantity_shortfall``: per item, ordered minus delivered (summed over
  the order's delivery notes) above ``tolerance_units``.
- ``amount_mismatch``: invoiced total vs the order total, beyond
  ``tolerance_percent`` of the order total.
- ``delivery_date_window``: the last delivery later than the promised date
  plus ``grace_days``.
- ``missing_document``: an order with no delivery note or no invoice among
  the documents reviewed, or a document whose order isn't there. That's
  absence of evidence, never proof that something didn't happen.

Units or currencies that differ produce a ``data_quality`` finding instead
of a comparison: nothing is ever converted.
"""

import hashlib
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from app.enums import RuleTemplate, Severity
from app.pipeline.claims.schema import DocumentType, normalise_reference
from app.pipeline.rules import explain

ENGINE_VERSION = "engine-1"
# Word-overlap matching compares every unmatched pair; beyond this, exact only.
FUZZY_LIMIT = 250_000


@dataclass(frozen=True)
class Fact:
    """One grounded claim."""

    claim_id: str
    evidence_id: str
    file_name: str
    field: str
    value: str
    unit: str | None = None
    item_key: str | None = None
    item_label: str = ""  # the item as written, for explanations


@dataclass
class Doc:
    evidence_id: str
    file_name: str
    kind: DocumentType
    facts: list[Fact] = field(default_factory=list)

    def first(self, name: str) -> Fact | None:
        return next((f for f in self.facts if f.field == name), None)

    def all(self, name: str) -> list[Fact]:
        return [f for f in self.facts if f.field == name]

    @property
    def number(self) -> str:
        fact = self.first("document_number")
        return normalise_reference(fact.value) if fact else ""

    @property
    def order_ref(self) -> str:
        fact = self.first("order_reference")
        return normalise_reference(fact.value) if fact else ""

    @property
    def label(self) -> str:
        fact = self.first("document_number")
        return f"{fact.value} ({self.file_name})" if fact else self.file_name

    @property
    def type_claims(self) -> list[str]:
        return [f.claim_id for f in self.facts if f.field in ("document_type", "document_number")]


@dataclass
class Proposed:
    template: RuleTemplate
    finding_type: str
    severity: Severity
    claim_ids: list[str]
    expected: dict[str, str]
    observed: dict[str, str]
    difference: dict[str, str] | None
    explanation: str
    discriminator: str = ""  # keeps two findings on the same claims apart

    def dedupe_key(self, rule_id: str, version: int) -> str:
        basis = "|".join([*sorted(self.claim_ids), self.finding_type, self.discriminator])
        digest = hashlib.sha256(basis.encode()).hexdigest()[:40]
        return f"{rule_id}:v{version}:{self.finding_type}:{digest}"[:200]


@dataclass
class Order:
    po: Doc | None
    deliveries: list[Doc] = field(default_factory=list)
    invoices: list[Doc] = field(default_factory=list)


def _dec(text: str) -> Decimal | None:
    try:
        value = Decimal(text)
    except (InvalidOperation, TypeError):
        return None
    return value if value.is_finite() else None


def _fmt(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text if "." not in text else text.rstrip("0").rstrip(".") or "0"


def _severity(share: Decimal, high: Decimal, medium: Decimal) -> Severity:
    if share >= high:
        return Severity.HIGH
    if share >= medium:
        return Severity.MEDIUM
    return Severity.LOW


def group(docs: list[Doc]) -> tuple[list[Order], list[Proposed]]:
    """Orders with their documents, and data-quality findings for documents
    that can't be placed."""
    orders: dict[str, Order] = {}
    for doc in docs:
        if doc.kind == DocumentType.PURCHASE_ORDER:
            orders.setdefault(doc.number or doc.evidence_id, Order(doc))
    issues: list[Proposed] = []
    orphans: dict[str, Order] = {}
    for doc in docs:
        if doc.kind not in (DocumentType.DELIVERY_NOTE, DocumentType.INVOICE):
            continue
        ref = doc.order_ref
        target: Order | None
        if ref:
            target = orders.get(ref)
            if target is None:
                target = orphans.setdefault(ref, Order(None))
        elif len(orders) == 1:
            target = next(iter(orders.values()))
        elif not orders:
            target = orphans.setdefault(f"unreferenced:{doc.evidence_id}", Order(None))
        else:
            issues.append(
                Proposed(
                    RuleTemplate.MISSING_DOCUMENT,
                    "data_quality",
                    Severity.LOW,
                    doc.type_claims,
                    {"value": "a purchase order reference"},
                    {"value": "none on the document"},
                    None,
                    explain.unplaced(doc.label, doc.kind, len(orders)),
                    discriminator=doc.evidence_id,
                )
            )
            continue
        (target.deliveries if doc.kind == DocumentType.DELIVERY_NOTE else target.invoices).append(
            doc
        )
    return list(orders.values()) + list(orphans.values()), issues


# quantity_shortfall -----------------------------------------------------------


def _match_items(
    ordered: dict[str, list[Fact]], delivered: dict[str, list[Fact]]
) -> dict[str, str]:
    """Ordered item key -> delivered item key. Exact keys first; then one
    item's words contained in the other's, when that picks exactly one."""
    pairs = {k: k for k in ordered if k in delivered}
    free = [k for k in delivered if k not in pairs.values()]
    if (len(ordered) - len(pairs)) * len(free) > FUZZY_LIMIT:
        return pairs  # very large tables: exact matches only
    for key in ordered:
        if key in pairs:
            continue
        words = set(key.split())
        hits = [d for d in free if words <= set(d.split()) or set(d.split()) <= words]
        if len(hits) == 1:
            pairs[key] = hits[0]
            free.remove(hits[0])
    if not pairs and len(ordered) == 1 and len(delivered) == 1:
        # One item on each side, described differently.
        pairs[next(iter(ordered))] = next(iter(delivered))
    return pairs


def quantity_shortfall(order: Order, tolerance: Decimal) -> list[Proposed]:
    if order.po is None or not order.deliveries:
        return []
    ordered: dict[str, list[Fact]] = defaultdict(list)
    for fact in order.po.all("ordered_quantity"):
        if fact.item_key:
            ordered[fact.item_key].append(fact)
    delivered: dict[str, list[Fact]] = defaultdict(list)
    for doc in order.deliveries:
        for fact in doc.all("delivered_quantity"):
            if fact.item_key:
                delivered[fact.item_key].append(fact)
    if not ordered or not delivered:
        return []  # no line items to compare on one side
    pairs = _match_items(ordered, delivered)
    out: list[Proposed] = []
    for key, facts in ordered.items():
        got = delivered.get(pairs.get(key, ""), [])
        units = {f.unit for f in facts + got if f.unit}
        if len(units) > 1:
            out.append(
                Proposed(
                    RuleTemplate.QUANTITY_SHORTFALL,
                    "data_quality",
                    Severity.LOW,
                    [f.claim_id for f in facts + got],
                    {"value": " / ".join(sorted({f.unit or "" for f in facts})), "item": key},
                    {"value": " / ".join(sorted({f.unit or "" for f in got})), "item": key},
                    None,
                    explain.unit_mismatch(facts[0].item_label, sorted(units)),
                    discriminator=key,
                )
            )
            continue
        unit = units.pop() if units else "units"
        values = [_dec(f.value) for f in facts]
        received = [_dec(f.value) for f in got]
        if any(v is None for v in values + received):
            continue
        want = sum((v for v in values if v is not None), Decimal(0))
        have = sum((v for v in received if v is not None), Decimal(0))
        short = want - have
        if short <= tolerance or want <= 0:
            continue
        files = sorted({f.file_name for f in got}) or sorted(
            {d.file_name for d in order.deliveries}
        )
        out.append(
            Proposed(
                RuleTemplate.QUANTITY_SHORTFALL,
                "quantity_shortfall",
                _severity(short / want, Decimal("0.10"), Decimal("0.02")),
                [f.claim_id for f in facts + got] + order.po.type_claims,
                {"value": _fmt(want), "unit": unit, "item": facts[0].item_label},
                {"value": _fmt(have), "unit": unit, "item": facts[0].item_label},
                {"value": _fmt(short), "unit": unit},
                explain.shortfall(
                    facts[0].item_label,
                    _fmt(want),
                    _fmt(have),
                    _fmt(short),
                    unit,
                    order.po.file_name,
                    files,
                    found=bool(got),
                ),
                discriminator=key,
            )
        )
    return out


# amount_mismatch ----------------------------------------------------------------


def amount_mismatch(order: Order, tolerance_percent: Decimal) -> list[Proposed]:
    if order.po is None or not order.invoices:
        return []
    po_total = order.po.first("total_amount")
    totals = [t for t in (doc.first("total_amount") for doc in order.invoices) if t]
    if po_total is None or not totals:
        return []
    currencies = {f.unit for f in [po_total, *totals] if f.unit}
    claim_ids = [po_total.claim_id] + [t.claim_id for t in totals]
    if len(currencies) > 1:
        return [
            Proposed(
                RuleTemplate.AMOUNT_MISMATCH,
                "data_quality",
                Severity.LOW,
                claim_ids,
                {"value": po_total.unit or ""},
                {"value": " / ".join(sorted({t.unit or "" for t in totals}))},
                None,
                explain.currency_mismatch(sorted(currencies)),
            )
        ]
    currency = currencies.pop() if currencies else ""
    expected = _dec(po_total.value)
    invoiced_values = [_dec(t.value) for t in totals]
    if expected is None or expected <= 0 or any(v is None for v in invoiced_values):
        return []
    invoiced = sum((v for v in invoiced_values if v is not None), Decimal(0))
    difference = invoiced - expected
    percent = abs(difference) / expected * 100
    if percent <= tolerance_percent:
        return []
    return [
        Proposed(
            RuleTemplate.AMOUNT_MISMATCH,
            "amount_mismatch",
            _severity(percent, Decimal(10), Decimal(2)),
            claim_ids,
            {"value": _fmt(expected), "unit": currency},
            {"value": _fmt(invoiced), "unit": currency},
            {
                "value": _fmt(difference),
                "unit": currency,
                "percent": _fmt(percent.quantize(Decimal("0.01"))),
            },
            explain.amount(
                _fmt(expected),
                _fmt(invoiced),
                _fmt(difference),
                _fmt(percent.quantize(Decimal("0.01"))),
                currency,
                order.po.file_name,
                sorted({t.file_name for t in totals}),
            ),
        )
    ]


# delivery_date_window -------------------------------------------------------------


def delivery_date_window(order: Order, grace_days: Decimal) -> list[Proposed]:
    if order.po is None or not order.deliveries:
        return []
    promised = order.po.first("promised_delivery_date")
    dated = [d for d in (doc.first("delivery_date") for doc in order.deliveries) if d]
    if promised is None or not dated:
        return []
    try:
        due = date.fromisoformat(promised.value)
        last_fact = max(dated, key=lambda f: date.fromisoformat(f.value))
        last = date.fromisoformat(last_fact.value)
    except ValueError:
        return []
    late = (last - due).days
    if late <= grace_days:
        return []
    return [
        Proposed(
            RuleTemplate.DELIVERY_DATE_WINDOW,
            "late_delivery",
            Severity.HIGH if late > 14 else Severity.MEDIUM if late > 3 else Severity.LOW,
            [promised.claim_id, last_fact.claim_id],
            {"value": due.isoformat(), "unit": ""},
            {"value": last.isoformat(), "unit": ""},
            {"value": str(late), "unit": "days"},
            explain.late(
                due.isoformat(), last.isoformat(), late, order.po.file_name, last_fact.file_name
            ),
        )
    ]


# missing_document -------------------------------------------------------------------


def missing_document(order: Order) -> list[Proposed]:
    out: list[Proposed] = []
    if order.po is None:
        for doc in order.deliveries + order.invoices:
            ref = doc.first("order_reference")
            reference = ref.value if ref else ""
            out.append(
                Proposed(
                    RuleTemplate.MISSING_DOCUMENT,
                    "missing_document",
                    Severity.MEDIUM,
                    doc.type_claims + ([ref.claim_id] if ref else []),
                    {"value": "purchase order", "reference": reference},
                    {"value": "none found"},
                    None,
                    explain.missing_order(doc.label, doc.kind, reference),
                    discriminator=f"po-for:{doc.evidence_id}",
                )
            )
        return out
    for kind, docs in (
        (DocumentType.DELIVERY_NOTE, order.deliveries),
        (DocumentType.INVOICE, order.invoices),
    ):
        if docs:
            continue
        out.append(
            Proposed(
                RuleTemplate.MISSING_DOCUMENT,
                "missing_document",
                Severity.MEDIUM,
                order.po.type_claims,
                {"value": explain.NAMES[kind]},
                {"value": "none found"},
                None,
                explain.missing(order.po.label, kind),
                discriminator=kind.value,
            )
        )
    return out


def evaluate(docs: list[Doc], params: dict[RuleTemplate, dict[str, str]]) -> list[Proposed]:
    """Run the enabled templates (the keys of ``params``) over the documents."""
    orders, issues = group(docs)
    out: list[Proposed] = []
    if RuleTemplate.MISSING_DOCUMENT in params:
        out += issues
    for order in orders:
        if RuleTemplate.QUANTITY_SHORTFALL in params:
            tolerance = Decimal(params[RuleTemplate.QUANTITY_SHORTFALL].get("tolerance_units", "0"))
            out += quantity_shortfall(order, tolerance)
        if RuleTemplate.AMOUNT_MISMATCH in params:
            percent = Decimal(params[RuleTemplate.AMOUNT_MISMATCH].get("tolerance_percent", "0"))
            out += amount_mismatch(order, percent)
        if RuleTemplate.DELIVERY_DATE_WINDOW in params:
            grace = Decimal(params[RuleTemplate.DELIVERY_DATE_WINDOW].get("grace_days", "0"))
            out += delivery_date_window(order, grace)
        if RuleTemplate.MISSING_DOCUMENT in params:
            out += missing_document(order)
    return out
