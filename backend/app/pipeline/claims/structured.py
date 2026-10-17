"""A deterministic claim extractor: no model, no network.

It reads what purchase orders, delivery notes and invoices state in a
predictable shape, and nothing else:

- the document type, from its title line ("Purchase Order", "Delivery
  Order", "Tax Invoice", ...);
- labelled fields ("PO No: 4500012", "Delivery Date: 03/10/2026",
  "Grand Total: SGD 1,250.00"), in text lines or two-cell table rows;
- line-item tables: a header row with an item column and a quantity column
  (plus unit, price and amount when present), in spreadsheets, CSV, Word
  tables and PDF text.

Every claim quotes the line it came from, so grounding can find it again.
It doesn't guess: a value it can't tie to a label or a column isn't
claimed. Free-form or unusual layouts need the Gemini extractor.
"""

import re
from collections import Counter
from dataclasses import dataclass

from app.pipeline.claims.schema import (
    UNIT_ALIASES,
    DocumentType,
    RawClaim,
    parse_date,
    parse_decimal,
    parse_money,
)
from app.pipeline.document import Page

NAME = "pruflo_structured"
VERSION = "structured-1"
SEPARATOR = " | "
TITLE_LINES = 15

# Title words, checked on the first lines of page 1. A match followed by a
# label word ("Purchase Order No:") is a reference to another document, not
# the title, and is skipped.
TITLES: list[tuple[DocumentType, str]] = [
    (DocumentType.DELIVERY_NOTE, r"delivery\s+(?:note|order|receipt|docket)"),
    (DocumentType.DELIVERY_NOTE, r"goods\s+receiv(?:ed|ing)\s+(?:note|report)"),
    (DocumentType.DELIVERY_NOTE, r"receiving\s+report|packing\s+(?:list|slip)|\bGRN\b"),
    (DocumentType.DELIVERY_NOTE, r"surat\s+jalan"),  # Indonesian delivery note
    (DocumentType.INVOICE, r"(?:tax\s+|commercial\s+|vendor\s+)?invoice|vendor\s+bill|faktur"),
    (DocumentType.PURCHASE_ORDER, r"purchase\s+order|\bP\.?O\.?\b(?=\s*$)|pesanan\s+pembelian"),
]
LABEL_AFTER = re.compile(r"^\s*(?:no\b|no\.|number|num\b|#|ref|reference|date|:)", re.I)

REF = r"[A-Za-z0-9][A-Za-z0-9\-/_.]*"
NUMBER_WORDS = r"(?:no\.?|number|num|#)"

# (field, label pattern, applies to). Order matters within a field: earlier
# labels are more specific and win.
LABELS: list[tuple[str, str, set[DocumentType]]] = [
    (
        "document_number",
        rf"purchase\s+order\s*{NUMBER_WORDS}|p\.?o\.?\s*{NUMBER_WORDS}|"
        rf"order\s*{NUMBER_WORDS}",
        {DocumentType.PURCHASE_ORDER},
    ),
    (
        "document_number",
        rf"(?:delivery\s+(?:note|order)|d\.?[no]\.?|grn|goods\s+receiv(?:ed|ing)"
        rf"\s+note|packing\s+list|receipt)\s*{NUMBER_WORDS}",
        {DocumentType.DELIVERY_NOTE},
    ),
    (
        "document_number",
        rf"(?:tax\s+)?invoice\s*{NUMBER_WORDS}|bill\s*{NUMBER_WORDS}",
        {DocumentType.INVOICE},
    ),
    (
        "order_reference",
        rf"(?:customer\s+|your\s+)?(?:purchase\s+order|p\.?o\.?)\s*"
        rf"(?:{NUMBER_WORDS}|ref(?:erence)?)|order\s+ref(?:erence)?|your\s+order(?:\s*{NUMBER_WORDS})?|"
        rf"order\s*{NUMBER_WORDS}",
        {DocumentType.DELIVERY_NOTE, DocumentType.INVOICE},
    ),
    ("supplier_name", r"supplier(?:\s+name)?|vendor(?:\s+name)?", set(DocumentType)),
    (
        "promised_delivery_date",
        r"required\s+(?:by|date)|expected\s+delivery(?:\s+date)?|"
        r"delivery\s+(?:by|date|required)|promised\s+(?:delivery\s+)?date|deliver\s+by",
        {DocumentType.PURCHASE_ORDER},
    ),
    (
        "delivery_date",
        r"date\s+(?:delivered|received)|delivered\s+on|received\s+on|"
        r"delivery\s+date|receipt\s+date",
        {DocumentType.DELIVERY_NOTE},
    ),
    (
        "document_date",
        r"(?:invoice|order|po|issue|document)\s+date|date(?:\s+issued)?",
        set(DocumentType),
    ),
    (
        "total_amount",
        r"grand\s+total|total\s+amount(?:\s+due)?|total\s+due|amount\s+due|"
        r"invoice\s+total|order\s+total|total\s+payable|total(?!\s*(?:qty|quantity|items|units|tax|"
        r"gst|vat|weight|pcs))",
        set(DocumentType),
    ),
]
LABEL_RE = [
    (field, re.compile(rf"(?<![A-Za-z]){pattern}(?![A-Za-z])", re.I), kinds)
    for field, pattern, kinds in LABELS
]
ANY_LABEL = re.compile("|".join(f"(?:{p})" for _, p, _ in LABELS), re.I)
SUBTOTAL = re.compile(r"\b(?:sub\s*-?\s*total|discount|tax|gst|vat|shipping)\b", re.I)

# Column headers of line-item tables.
COLUMNS: list[tuple[str, str]] = [
    ("unit_price", r"unit\s+price|unit\s+cost|price\s*/\s*unit|price|rate"),
    ("line_amount", r"line\s+total|(?:net\s+)?amount|total|ext(?:ended)?\.?(?:\s+price)?|value"),
    ("qty_ordered", r"(?:qty|quantity)\s+ordered|ordered(?:\s+(?:qty|quantity))?|order\s+qty"),
    (
        "qty_delivered",
        r"(?:qty|quantity)\s+(?:delivered|received|shipped|supplied)|"
        r"(?:delivered|received|shipped|supplied)(?:\s+(?:qty|quantity))?",
    ),
    ("qty_invoiced", r"(?:qty|quantity)\s+(?:invoiced|billed)|invoiced(?:\s+(?:qty|quantity))?"),
    ("qty", r"qty|quantity|kuantitas|jumlah"),
    ("unit", r"uom|u/m|unit(?!\s+(?:price|cost))|units"),
    ("code", r"item\s+code|sku|part\s+(?:no|number)|product\s+code|material(?:\s+no)?|code"),
    ("item", r"item\s+description|description|item|product|goods|particulars|barang"),
    ("line_no", r"no\.?|#|line|s/n|sr\.?"),
]
COLUMN_RE = re.compile(
    "|".join(f"(?P<{role}>(?<![A-Za-z]){p}(?![A-Za-z]))" for role, p in COLUMNS), re.I
)
QTY_ROLE = {
    DocumentType.PURCHASE_ORDER: ("qty_ordered", "ordered_quantity"),
    DocumentType.DELIVERY_NOTE: ("qty_delivered", "delivered_quantity"),
    DocumentType.INVOICE: ("qty_invoiced", "invoiced_quantity"),
}
NUMBER_TOKEN = re.compile(r"^\(?-?[\d,]+(?:\.\d+)?\)?$")
CURRENCY_IN_HEADER = re.compile(r"\((SGD|MYR|IDR|USD|EUR|GBP|THB|PHP|VND|AUD|CNY|JPY)\)", re.I)


@dataclass
class _Line:
    page: int
    text: str
    cells: list[str] | None
    ref: str | None


class StructuredExtractor:
    name = NAME
    model = VERSION
    prompt_version = "-"

    def __init__(self, day_first: bool = True) -> None:
        self.day_first = day_first

    def extract(self, pages: list[Page], file_name: str = "") -> list[RawClaim]:
        lines = [
            _Line(p.index, b.text, b.cells, b.cell_ref) for p in pages for b in p.blocks if b.text
        ]
        if not lines:
            return []
        kind, title = _document_type(lines)
        context = "title"
        if title is None:
            # No title (common in spreadsheets): a table whose quantity column
            # says what it counts ("Qty Ordered") tells the type.
            kind, title, context = _type_from_columns(lines)
        claims: list[RawClaim] = []
        if title is not None:
            claims.append(
                RawClaim("document_type", kind.value, title.text, title.page, context=context)
            )
        claims += self._labelled(lines, kind)
        currency = _currency(claims)
        claims += _line_items(lines, kind, currency)
        return claims

    # Labelled fields ----------------------------------------------------------

    def _labelled(self, lines: list[_Line], kind: DocumentType) -> list[RawClaim]:
        found: dict[str, list[tuple[int, RawClaim]]] = {}
        for line in lines:
            for field, priority, value in _label_values(line, kind):
                claim = self._value_claim(field, value, line)
                if claim is not None:
                    found.setdefault(field, []).append((priority, claim))
        claims: list[RawClaim] = []
        for field, candidates in found.items():
            best = min(p for p, _ in candidates)
            chosen = [c for p, c in candidates if p == best]
            # The last "Total" on a document is the grand total; for the other
            # fields the first mention is the header's.
            claims.append(chosen[-1] if field == "total_amount" else chosen[0])
        if kind == DocumentType.DELIVERY_NOTE and "delivery_date" not in found:
            # A delivery note's own date is the date of delivery.
            dated = next((c for c in claims if c.field == "document_date"), None)
            if dated is not None:
                claims.append(
                    RawClaim(
                        "delivery_date",
                        dated.raw_value,
                        dated.quote,
                        dated.page,
                        context="date of the delivery note",
                    )
                )
        return claims

    def _value_claim(self, field: str, value: str, line: _Line) -> RawClaim | None:
        value = value.strip().strip(":#-").strip()
        if not value:
            return None
        context = line.ref or ""
        if field.endswith("_date"):
            tokens = value.split()
            for size in (3, 2, 1):
                candidate = " ".join(tokens[:size]).rstrip(",")
                if parse_date(candidate, self.day_first):
                    return RawClaim(field, candidate, line.text, line.page, context=context)
            return None
        if field == "total_amount":
            match = re.match(
                r"((?:[A-Z]{3}|S\$|RM|Rp|US\$|\$|€|£)?\s*\(?-?[\d][\d.,]*\)?(?:\s*[A-Z]{3}\b)?)",
                value,
            )
            if not match or parse_money(match.group(1))[0] is None:
                return None
            return RawClaim(field, match.group(1).strip(), line.text, line.page, context=context)
        if field in ("document_number", "order_reference"):
            match = re.match(REF, value)
            if not match or not re.search(r"\d", match.group(0)):
                return None  # references carry digits; "No:" followed by a word isn't one
            return RawClaim(
                field, match.group(0).rstrip(".,"), line.text, line.page, context=context
            )
        return RawClaim(field, value[:200], line.text, line.page, context=context)


def _document_type(lines: list[_Line]) -> tuple[DocumentType, _Line | None]:
    first_page = [ln for ln in lines if ln.page == lines[0].page][:TITLE_LINES]
    best: tuple[int, int, DocumentType, _Line] | None = None
    for number, line in enumerate(first_page):
        for kind, pattern in TITLES:
            for match in re.finditer(pattern, line.text, re.I):
                if LABEL_AFTER.match(line.text[match.end() :]):
                    continue
                candidate = (number, match.start(), kind, line)
                if best is None or candidate[:2] < best[:2]:
                    best = candidate
    if best is None:
        return DocumentType.OTHER, None
    return best[2], best[3]


def _type_from_columns(lines: list[_Line]) -> tuple[DocumentType, _Line | None, str]:
    for line in lines:
        texts = line.cells if line.cells else [line.text]
        roles = {role: label for text in texts for role, label in _header_roles(text)}
        if not ({"item", "code"} & set(roles)):
            continue
        for kind, (role, _) in QTY_ROLE.items():
            if role in roles:
                return kind, line, f"inferred from column {roles[role]}"
    return DocumentType.OTHER, None, ""


def _label_values(line: _Line, kind: DocumentType) -> list[tuple[str, int, str]]:
    """(field, label priority, the text after the label) for each label on the line."""
    out: list[tuple[str, int, str]] = []
    filled = [i for i, c in enumerate(line.cells or []) if c.strip()]
    if len(filled) == 2:
        # A key-value row: "PO Number | 4500012", label and value side by side.
        # A gap between them ("Total | | 150") is a table's summary row instead.
        if filled[1] != filled[0] + 1:
            return out
        assert line.cells is not None  # noqa: S101 (filled came from it)
        label, value = line.cells[filled[0]], line.cells[filled[1]]
        for priority, (field, pattern, kinds) in enumerate(LABEL_RE):
            if kind in kinds and pattern.fullmatch(label.strip().rstrip(":")):
                out.append((field, priority, value))
        return out
    text = line.text
    starts = [m.start() for m in ANY_LABEL.finditer(text)]
    for priority, (field, pattern, kinds) in enumerate(LABEL_RE):
        if kind not in kinds:
            continue
        for match in pattern.finditer(text):
            rest = text[match.end() :]
            if not re.match(r"\s*[:#]|\s+\S", rest):
                continue
            if field == "total_amount" and SUBTOTAL.search(text[: match.start()]):
                continue
            following = [s for s in starts if s > match.end()]
            end = following[0] - match.end() if following else len(rest)
            value = rest[:end]
            if field == "document_date" and re.match(r"\s*[:#]?\s*$", value):
                continue
            out.append((field, priority, value))
    return out


def _currency(claims: list[RawClaim]) -> str | None:
    found = Counter(
        c for c in (parse_money(x.raw_value)[1] for x in claims if x.field == "total_amount") if c
    )
    return found.most_common(1)[0][0] if found else None


# Line-item tables ---------------------------------------------------------------


def _header_roles(text: str) -> list[tuple[str, str]]:
    return [
        (next(role for role, value in m.groupdict().items() if value), m.group(0))
        for m in COLUMN_RE.finditer(text)
    ]


def _line_items(lines: list[_Line], kind: DocumentType, currency: str | None) -> list[RawClaim]:
    if kind not in QTY_ROLE:
        return []
    claims: list[RawClaim] = []
    index = 0
    while index < len(lines):
        header = lines[index]
        table = _parse_header(header, kind)
        if table is None:
            index += 1
            continue
        roles, labels, qty_role, by_cells = table
        header_currency = CURRENCY_IN_HEADER.search(header.text)
        money_unit = header_currency.group(1).upper() if header_currency else currency
        index += 1
        seen: list[str] = []
        while index < len(lines) and lines[index].page == header.page:
            row = lines[index]
            ordinal = len(seen) + 1
            values = (
                _row_values(row, roles) if by_cells else _text_row_values(row.text, roles, ordinal)
            )
            if values is None:
                break
            item = values.get("item") or values.get("code")
            qty = values.get(qty_role)
            if not item:
                break  # the end of the table
            if qty is None or parse_decimal(qty) is None:
                index += 1  # a line without a quantity (a note, a blank cell)
                continue
            seen.append(item)
            where = row.ref or "line"
            field = QTY_ROLE[kind][1]
            claims.append(
                RawClaim(
                    field,
                    qty,
                    row.text,
                    row.page,
                    item=item,
                    unit=values.get("unit"),
                    context=f"{where} · column {labels[qty_role]}",
                )
            )
            for role, name in (("unit_price", "unit_price"), ("line_amount", "line_amount")):
                if values.get(role):
                    claims.append(
                        RawClaim(
                            name,
                            values[role],
                            row.text,
                            row.page,
                            item=item,
                            unit=money_unit,
                            context=f"{where} · column {labels[role]}",
                        )
                    )
            index += 1
    return claims


def _parse_header(
    line: _Line, kind: DocumentType
) -> tuple[list[str | None], dict[str, str], str, bool] | None:
    specific = QTY_ROLE[kind][0]
    if line.cells:
        roles: list[str | None] = []
        labels: dict[str, str] = {}
        for cell in line.cells:
            found = _header_roles(cell)
            # A header cell is a short label, not a sentence that mentions one.
            role = found[0][0] if found and len(cell.strip()) <= 40 else None
            if role in labels:
                role = None  # a second "Total" column: keep the first
            roles.append(role)
            if role:
                labels[role] = cell.strip()
        by_cells = True
    else:
        found = _header_roles(line.text)
        covered = sum(len(label) for _, label in found)
        letters = len(re.sub(r"[^A-Za-z]", "", line.text))
        if not found or covered < 0.6 * letters:
            return None  # a sentence that happens to contain "item" and "qty"
        roles, labels = [], {}
        for role, label in found:
            if role not in labels:
                roles.append(role)
                labels[role] = label
        by_cells = False
    if not ({"item", "code"} & set(labels)):
        return None
    qty_role = specific if specific in labels else "qty" if "qty" in labels else None
    if qty_role is None:
        return None
    return roles, labels, qty_role, by_cells


def _row_values(row: _Line, roles: list[str | None]) -> dict[str, str] | None:
    if not row.cells:
        return None
    cells = row.cells + [""] * (len(roles) - len(row.cells))
    if any(SUBTOTAL.search(c) or re.match(r"^\s*total\b", c, re.I) for c in cells[:2]):
        return None
    return {role: cells[i].strip() for i, role in enumerate(roles) if role and cells[i].strip()}


def _text_row_values(text: str, roles: list[str | None], ordinal: int) -> dict[str, str] | None:
    """A PDF or text line: take values from the right by column type, and
    the remaining words on the left are the item."""
    if SUBTOTAL.search(text) or re.match(r"^\s*(?:grand\s+)?total\b", text, re.I):
        return None
    tokens = text.split()
    values: dict[str, str] = {}
    for role in reversed([r for r in roles if r and r not in ("item", "code", "line_no")]):
        if not tokens:
            return None
        token = tokens[-1]
        if role == "unit":
            if token.lower().strip(".") in UNIT_ALIASES:
                values["unit"] = tokens.pop()
            continue
        cleaned = re.sub(r"^(?:[A-Z]{3}|S\$|RM|Rp|\$)", "", token)
        if not NUMBER_TOKEN.match(cleaned):
            return None
        values[role] = tokens.pop()
    # A leading line number: a "No." column, or the row's own position (1, 2,
    # 3...) when the header has none. "12V battery" on row 3 keeps its "12V".
    if (
        len(tokens) > 1
        and re.fullmatch(r"\d{1,3}[.)]?", tokens[0])
        and ((roles and roles[0] == "line_no") or int(tokens[0].rstrip(".)")) == ordinal)
    ):
        tokens.pop(0)
    if (
        "code" in roles
        and "item" in roles
        and roles.index("code") < roles.index("item")
        and len(tokens) > 1
    ):
        values["code"] = tokens.pop(0)
    description = " ".join(tokens).strip()
    if not description:
        return None
    values["item" if "item" in roles else "code"] = description
    return values
