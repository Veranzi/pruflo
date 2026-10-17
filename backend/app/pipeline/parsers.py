"""Native text extraction: exact, cheap and in-region (Docs/backend_plan.md, Step 8).

OCR is only used for images and for PDF pages with no text layer; this
module marks those pages with ``needs_ocr`` and leaves them empty.
"""

import csv
import io
from collections.abc import Iterable
from datetime import date, datetime, time
from decimal import Decimal
from importlib.metadata import version
from typing import Any

from app.pipeline.document import BBox, Block, Page
from app.pipeline.errors import PermanentError

# A page with an image and fewer real characters than this is treated as a
# scan (e.g. a scanned page with a printed header). No text at all always is.
MIN_TEXT_CHARS = 20
MAX_SPREADSHEET_ROWS = 50_000
WORD_GAP = 1.5  # points between characters before they count as separate words
CELL_SEPARATOR = " | "


def _pkg(name: str) -> str:
    return f"{name}-{version(name)}"


def _clamp(v: float) -> float:
    return max(0.0, min(1.0, v))


def parse_pdf(data: bytes) -> list[Page]:
    import pdfplumber
    from pdfminer.pdfparser import PDFSyntaxError
    from pdfminer.psexceptions import PSException
    from pdfplumber.utils.exceptions import PdfminerException

    parser_version = _pkg("pdfplumber")
    pages: list[Page] = []
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for number, pdf_page in enumerate(pdf.pages, start=1):
                width, height = float(pdf_page.width), float(pdf_page.height)
                page = Page(
                    index=number,
                    width=Decimal(str(round(width, 2))),
                    height=Decimal(str(round(height, 2))),
                    parser="pdf_text",
                    parser_version=parser_version,
                )
                # x_tolerance 1.5 (default 3) keeps tight table columns apart, e.g.
                # "Payment Method" and "MPESA Code" instead of "Payment MethodMPESA Code".
                lines = pdf_page.extract_text_lines(
                    strip=True, return_chars=False, x_tolerance=WORD_GAP
                )
                for line in lines:
                    text = str(line["text"]).strip()
                    if not text:
                        continue
                    page.blocks.append(
                        Block(
                            text=text,
                            bbox=BBox(
                                _clamp(line["x0"] / width),
                                _clamp(line["top"] / height),
                                _clamp(line["x1"] / width),
                                _clamp(line["bottom"] / height),
                            ),
                        )
                    )
                real_chars = sum(len(b.text.replace(" ", "")) for b in page.blocks)
                if real_chars == 0 or (real_chars < MIN_TEXT_CHARS and pdf_page.images):
                    page.needs_ocr = True
                    page.blocks = []
                pages.append(page)
    except (PdfminerException, PDFSyntaxError, PSException, ValueError, KeyError, TypeError) as exc:
        raise PermanentError("unreadable_file", "This PDF couldn't be read.") from exc
    return pages


def parse_docx(data: bytes) -> list[Page]:
    """Word files have no fixed pages; the whole document is page 1."""
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as exc:  # python-docx raises a variety of zip/XML errors
        raise PermanentError("unreadable_file", "This Word file couldn't be read.") from exc

    page = Page(index=1, label="Document", parser="docx", parser_version=_pkg("python-docx"))
    table_number = 0
    for item in document.iter_inner_content():
        if isinstance(item, Paragraph):
            text = item.text.strip()
            if text:
                page.blocks.append(Block(text=text))
        elif isinstance(item, Table):
            table_number += 1
            for row_number, row in enumerate(item.rows, start=1):
                raw = [cell.text.strip() for cell in row.cells]  # one per column
                cells = _dedupe_merged(raw)
                text = CELL_SEPARATOR.join(c for c in cells if c)
                if text:
                    page.blocks.append(
                        Block(
                            text=text,
                            cell_ref=f"table {table_number}, row {row_number}",
                            cells=raw,
                        )
                    )
    return [page]


def _dedupe_merged(cells: list[str]) -> list[str]:
    # Merged cells repeat their text once per spanned column.
    out: list[str] = []
    for cell in cells:
        if not out or cell != out[-1]:
            out.append(cell)
    return out


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float):
        # Avoid float noise ("80.0", "0.30000000000000004") in what people quote.
        return format(Decimal(repr(value)).normalize(), "f")
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="seconds").removesuffix(" 00:00:00")
    if isinstance(value, date | time):
        return value.isoformat()
    return str(value).strip()


def _rows_to_page(
    rows: Iterable[tuple[int, list[str]]], page: Page, ref: str, last_column: str = ""
) -> Page:
    for row_number, cells in rows:
        if row_number > MAX_SPREADSHEET_ROWS:
            raise PermanentError(
                "too_many_rows", f"Sheets can have at most {MAX_SPREADSHEET_ROWS:,} rows."
            )
        text = CELL_SEPARATOR.join(c for c in cells if c)
        if text:
            cell_ref = (
                f"{ref}A{row_number}:{last_column}{row_number}"
                if last_column
                else (f"row {row_number}")
            )
            page.blocks.append(Block(text=text, cell_ref=cell_ref, cells=cells))
    return page


def parse_xlsx(data: bytes) -> list[Page]:
    """One page per sheet. Values, not formulas; each row is one block."""
    from openpyxl import load_workbook
    from openpyxl.utils import get_column_letter

    try:
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # openpyxl raises zip, XML and key errors
        raise PermanentError("unreadable_file", "This Excel file couldn't be read.") from exc
    parser_version = _pkg("openpyxl")
    pages: list[Page] = []
    try:
        for number, sheet in enumerate(workbook.worksheets, start=1):
            page = Page(
                index=number, label=sheet.title, parser="xlsx", parser_version=parser_version
            )
            last_column = get_column_letter(max(sheet.max_column or 1, 1))
            rows = (
                (row_number, [_cell_text(v) for v in values])
                for row_number, values in enumerate(sheet.iter_rows(values_only=True), start=1)
            )
            ref = f"'{sheet.title}'!" if " " in sheet.title else f"{sheet.title}!"
            pages.append(_rows_to_page(rows, page, ref, last_column))
    finally:
        workbook.close()
    return pages


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise PermanentError("unreadable_file", "The file's text encoding isn't supported.")


def parse_csv(data: bytes) -> list[Page]:
    text = _decode(data)
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    page = Page(index=1, label="Rows", parser="csv", parser_version="python-csv")
    try:
        rows = ((n, [c.strip() for c in row]) for n, row in enumerate(reader, start=1))
        return [_rows_to_page(rows, page, "")]
    except csv.Error as exc:
        raise PermanentError("unreadable_file", "This CSV file couldn't be read.") from exc


def parse_txt(data: bytes) -> list[Page]:
    page = Page(index=1, label="Text", parser="txt", parser_version="plain")
    for line in _decode(data).splitlines():
        if line.strip():
            page.blocks.append(Block(text=line.rstrip()))
    return [page]


def image_pages() -> list[Page]:
    """Images have no text layer: one page, sent to OCR."""
    return [Page(index=1, label="Image", parser="image", parser_version="-", needs_ocr=True)]
