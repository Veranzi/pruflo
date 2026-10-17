"""The claim schema every extractor fills, and the code that checks it
(Docs/backend_plan.md, Step 9).

An extractor (Gemini, or the structured reader) only proposes values with
the quote they came from. Everything here is deterministic: field names and
document types are checked against fixed lists, numbers, dates and
currencies are parsed by code, and lengths are capped. Nothing an extractor
returns is executed or trusted beyond these checks.
"""

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import StrEnum

SCHEMA_VERSION = "claims-1"
MAX_VALUE = 200
MAX_QUOTE = 500
MAX_ITEM = 200


class DocumentType(StrEnum):
    PURCHASE_ORDER = "purchase_order"
    DELIVERY_NOTE = "delivery_note"  # delivery orders, goods received notes, packing lists
    INVOICE = "invoice"
    OTHER = "other"


class DataType(StrEnum):
    TEXT = "text"
    QUANTITY = "quantity"
    MONEY = "money"
    DATE = "date"


@dataclass(frozen=True)
class FieldSpec:
    data_type: DataType
    line_item: bool  # one per line item (needs an item key)
    description: str


FIELDS: dict[str, FieldSpec] = {
    "document_type": FieldSpec(
        DataType.TEXT, False, "purchase_order, delivery_note, invoice or other"
    ),
    "document_number": FieldSpec(DataType.TEXT, False, "This document's own number"),
    "order_reference": FieldSpec(
        DataType.TEXT, False, "The purchase order number this document refers to"
    ),
    "supplier_name": FieldSpec(DataType.TEXT, False, "The supplier or vendor"),
    "document_date": FieldSpec(DataType.DATE, False, "The date the document was issued"),
    "promised_delivery_date": FieldSpec(
        DataType.DATE, False, "The delivery date promised or required (purchase orders)"
    ),
    "delivery_date": FieldSpec(DataType.DATE, False, "The date goods were delivered or received"),
    "total_amount": FieldSpec(DataType.MONEY, False, "The document total, with currency"),
    "ordered_quantity": FieldSpec(DataType.QUANTITY, True, "Quantity ordered for one item"),
    "delivered_quantity": FieldSpec(
        DataType.QUANTITY, True, "Quantity delivered or received for one item"
    ),
    "invoiced_quantity": FieldSpec(DataType.QUANTITY, True, "Quantity invoiced for one item"),
    "unit_price": FieldSpec(DataType.MONEY, True, "Price per unit for one item"),
    "line_amount": FieldSpec(DataType.MONEY, True, "Amount for one item line"),
}

# Currency codes and the symbols that name exactly one currency in our
# markets. A bare "$" is ambiguous (SGD, USD, ...) and is left unresolved.
CURRENCIES = {"SGD", "MYR", "IDR", "USD", "EUR", "GBP", "THB", "PHP", "VND", "AUD", "CNY", "JPY"}
SYMBOLS = {"S$": "SGD", "RM": "MYR", "RP": "IDR", "US$": "USD", "€": "EUR", "£": "GBP"}

UNIT_ALIASES = {
    "pc": "pcs",
    "pcs": "pcs",
    "piece": "pcs",
    "pieces": "pcs",
    "ea": "pcs",
    "each": "pcs",
    "unit": "units",
    "units": "units",
    "nos": "pcs",
    "no": "pcs",
    "box": "boxes",
    "boxes": "boxes",
    "ctn": "cartons",
    "carton": "cartons",
    "cartons": "cartons",
    "kg": "kg",
    "kgs": "kg",
    "g": "g",
    "l": "l",
    "litre": "l",
    "liter": "l",
    "litres": "l",
    "m": "m",
    "pallet": "pallets",
    "pallets": "pallets",
    "set": "sets",
    "sets": "sets",
    "pack": "packs",
    "packs": "packs",
    "roll": "rolls",
    "rolls": "rolls",
    "bag": "bags",
    "bags": "bags",
    "drum": "drums",
    "drums": "drums",
}

MONTHS = {
    m: i
    for i, names in enumerate(
        (
            ("jan", "january"),
            ("feb", "february"),
            ("mar", "march"),
            ("apr", "april"),
            ("may",),
            ("jun", "june"),
            ("jul", "july"),
            ("aug", "august"),
            ("sep", "sept", "september"),
            ("oct", "october"),
            ("nov", "november"),
            ("dec", "december"),
        ),
        start=1,
    )
    for m in names
}


def parse_decimal(text: str) -> Decimal | None:
    """'1,200.50' -> 1200.50. Thousands separators are commas; a value with
    more than one dot or stray letters isn't a number."""
    cleaned = text.strip().replace(",", "").replace(" ", "")
    negative = cleaned.startswith("(") and cleaned.endswith(")")
    cleaned = cleaned.strip("()")
    if not re.fullmatch(r"-?\d+(\.\d+)?", cleaned):
        return None
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    return -value if negative else value


def parse_money(text: str) -> tuple[Decimal | None, str | None]:
    """'SGD 1,200.00' -> (1200.00, 'SGD'); '$1,200' -> (1200, None)."""
    raw = text.strip()
    currency: str | None = None
    upper = raw.upper()
    for symbol, code in sorted(SYMBOLS.items(), key=lambda s: -len(s[0])):
        if symbol.upper() in upper:
            currency = code
            upper = upper.replace(symbol.upper(), " ")
            break
    for code in CURRENCIES:
        if re.search(rf"\b{code}\b", upper):
            currency = currency or code
            upper = re.sub(rf"\b{code}\b", " ", upper)
    number = re.sub(r"[^\d.,()\-]", "", upper)
    # Dots for thousands and a comma for decimals ("1.500.000,00"), as written
    # in Indonesia: only when it can't be read the other way.
    dotted = re.fullmatch(r"(\d{1,3}(?:\.\d{3})+)(,\d{1,2})?", number)
    if dotted and (number.count(".") > 1 or dotted.group(2) or currency == "IDR"):
        number = dotted.group(1).replace(".", "") + (dotted.group(2) or "").replace(",", ".")
    return parse_decimal(number), currency


def parse_quantity(text: str) -> tuple[Decimal | None, str | None]:
    """'100 pcs' -> (100, 'pcs'); '1,000' -> (1000, None)."""
    match = re.fullmatch(r"\s*([\d,]+(?:\.\d+)?)\s*([A-Za-z]+\.?)?\s*", text)
    if not match:
        return None, None
    unit = match.group(2)
    return parse_decimal(match.group(1)), normalise_unit(unit) if unit else None


def normalise_unit(unit: str | None) -> str | None:
    if not unit:
        return None
    key = unit.strip().strip(".").lower()
    return UNIT_ALIASES.get(key, key[:20] or None)


def parse_date(text: str, day_first: bool = True) -> date | None:
    """ISO dates, '3 Oct 2026', 'Oct 3, 2026' and numeric dates. Numeric dates
    are read day first (DD/MM/YYYY), as written in Singapore, Malaysia and
    Indonesia; impossible values are rejected rather than guessed."""
    raw = text.strip()
    try:
        if m := re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", raw):
            return date(int(m[1]), int(m[2]), int(m[3]))
        if m := re.fullmatch(r"(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})", raw):
            a, b, year = int(m[1]), int(m[2]), int(m[3])
            day, month = (a, b) if day_first else (b, a)
            return date(year, month, day)
        if m := re.fullmatch(r"(\d{1,2})[\s\-]([A-Za-z]{3,9})\.?[\s\-,]+(\d{4})", raw):
            named = MONTHS.get(m[2].lower())
            return date(int(m[3]), named, int(m[1])) if named else None
        if m := re.fullmatch(r"([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})", raw):
            named = MONTHS.get(m[1].lower())
            return date(int(m[3]), named, int(m[2])) if named else None
    except ValueError:
        return None
    return None


def item_key(text: str) -> str:
    """Line items are matched across documents by this key: case, spacing
    and punctuation don't matter."""
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()[:MAX_ITEM]


def normalise_reference(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", text.upper())[:80]


@dataclass
class RawClaim:
    """What an extractor proposes. ``quote`` must be copied from the page."""

    field: str
    raw_value: str
    quote: str
    page: int | None = None
    item: str | None = None  # the line item's description or code
    unit: str | None = None
    context: str = ""  # where in the document: a cell range, a column label
    confidence: Decimal | None = None


@dataclass
class ValidClaim:
    field: str
    data_type: DataType
    raw_value: str
    normalized_value: str | None
    unit: str | None  # a unit for quantities, a currency code for money
    quote: str
    page: int | None
    item_key: str | None
    context: str
    confidence: Decimal | None
    status: str  # "found", or "unreadable" when the value couldn't be parsed


def validate(raw: RawClaim, day_first: bool = True) -> ValidClaim | None:
    """Check one proposed claim. Returns None for anything outside the schema."""
    spec = FIELDS.get(raw.field)
    if spec is None:
        return None
    value = (raw.raw_value or "").strip()[:MAX_VALUE]
    quote = (raw.quote or "").strip()[:MAX_QUOTE]
    if not value or not quote:
        return None
    # The item as written; the rule engine normalises it (item_key) to match.
    item = re.sub(r"\s+", " ", raw.item).strip()[:MAX_ITEM] if raw.item else None
    if item is not None and not item_key(item):
        item = None
    if spec.line_item and not item:
        return None  # a quantity without its item can't be matched to anything
    normalized: str | None = None
    unit: str | None = None
    if spec.data_type == DataType.QUANTITY:
        number, parsed_unit = parse_quantity(value)
        unit = normalise_unit(raw.unit) or parsed_unit
        normalized = format(number, "f") if number is not None else None
    elif spec.data_type == DataType.MONEY:
        number, currency = parse_money(value)
        hinted = (raw.unit or "").strip().upper()
        unit = currency or (hinted if hinted in CURRENCIES else SYMBOLS.get(hinted))
        normalized = format(number, "f") if number is not None else None
    elif spec.data_type == DataType.DATE:
        parsed = parse_date(value, day_first)
        normalized = parsed.isoformat() if parsed else None
    elif raw.field == "document_type":
        lowered = value.lower().replace(" ", "_")
        normalized = lowered if lowered in DocumentType.__members__.values() else None
        if normalized is None:
            return None
    else:
        normalized = value
    page = raw.page if isinstance(raw.page, int) and raw.page > 0 else None
    confidence = raw.confidence
    if confidence is not None and not (0 <= confidence <= 1):
        confidence = None
    return ValidClaim(
        field=raw.field,
        data_type=spec.data_type,
        raw_value=value,
        normalized_value=normalized,
        unit=unit,
        quote=quote,
        page=page,
        item_key=item,
        context=(raw.context or "")[:300],
        confidence=confidence,
        status="found" if normalized is not None else "unreadable",
    )
