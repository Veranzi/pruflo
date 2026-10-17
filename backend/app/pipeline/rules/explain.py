"""Finding explanations from fixed templates (Docs/backend_plan.md, Step 12).

Each sentence states only the finding's own values and files, so it reads
the same every time and never says more than the evidence does. An optional
Gemini rewrite (``app.services.reconcile``) must keep exactly these numbers
or it's discarded.
"""

from app.pipeline.claims.schema import DocumentType

NAMES = {
    DocumentType.PURCHASE_ORDER: "purchase order",
    DocumentType.DELIVERY_NOTE: "delivery note",
    DocumentType.INVOICE: "invoice",
    DocumentType.OTHER: "document",
}


def _files(names: list[str]) -> str:
    return ", ".join(names) if len(names) < 3 else f"{names[0]} and {len(names) - 1} others"


def shortfall(
    item: str,
    ordered: str,
    delivered: str,
    short: str,
    unit: str,
    po_file: str,
    delivery_files: list[str],
    *,
    found: bool,
) -> str:
    if not found:
        return (
            f"The purchase order ({po_file}) lists {ordered} {unit} of {item}; it doesn't "
            f"appear on the delivery note ({_files(delivery_files)}), so {short} {unit} "
            "are unaccounted for."
        )
    return (
        f"The purchase order ({po_file}) lists {ordered} {unit} of {item}; the delivery "
        f"note ({_files(delivery_files)}) records {delivered} {unit}, {short} {unit} short."
    )


def amount(
    expected: str,
    invoiced: str,
    difference: str,
    percent: str,
    currency: str,
    po_file: str,
    invoice_files: list[str],
) -> str:
    money = f"{currency} " if currency else ""
    direction = "more" if not difference.startswith("-") else "less"
    gap = difference.lstrip("-")
    return (
        f"The purchase order ({po_file}) totals {money}{expected}; the invoice "
        f"({_files(invoice_files)}) totals {money}{invoiced}, "
        f"{money}{gap} ({percent}%) {direction}."
    )


def late(promised: str, delivered: str, days: int, po_file: str, delivery_file: str) -> str:
    unit = "day" if days == 1 else "days"
    return (
        f"The purchase order ({po_file}) asks for delivery by {promised}; the delivery note "
        f"({delivery_file}) is dated {delivered}, {days} {unit} late."
    )


def missing(po_label: str, kind: DocumentType) -> str:
    return (
        f"No {NAMES[kind]} for purchase order {po_label} was found among the documents "
        f"reviewed. This is absence of evidence, not proof that it doesn't exist."
    )


def missing_order(doc_label: str, kind: DocumentType, reference: str) -> str:
    which = f"purchase order {reference}" if reference else "a purchase order"
    return (
        f"The {NAMES[kind]} {doc_label} refers to {which}, which isn't among the documents "
        f"reviewed. This is absence of evidence, not proof that it doesn't exist."
    )


def unplaced(doc_label: str, kind: DocumentType, orders: int) -> str:
    return (
        f"The {NAMES[kind]} {doc_label} doesn't name a purchase order, and the case has "
        f"{orders} purchase orders, so it can't be matched to one. Check it by hand."
    )


def unit_mismatch(item: str, units: list[str]) -> str:
    return (
        f"Quantities for {item} are recorded in different units ({', '.join(units)}). "
        f"They weren't compared, because units are never converted. Check them by hand."
    )


def currency_mismatch(currencies: list[str]) -> str:
    return (
        f"The order and invoice totals are in different currencies ({', '.join(currencies)}). "
        f"They weren't compared, because currencies are never converted. Check them by hand."
    )
