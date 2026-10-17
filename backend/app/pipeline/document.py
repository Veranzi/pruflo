"""The normalized document every parser and OCR provider produces.

A page's ``text`` is its blocks joined by newlines, and each block records
its ``[char_start, char_end)`` span in that text. Grounding (Step 10) finds
quotes in the page text and maps the span back to blocks for highlighting.

Bounding boxes are normalized to 0..1 of the page (x0, y0 top-left; x1, y1
bottom-right) so the viewer can draw them at any zoom. Spreadsheets and
CSV files have no geometry; their blocks carry a ``cell_ref`` instead.
"""

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass(frozen=True)
class BBox:
    x0: float
    y0: float
    x1: float
    y1: float

    def as_dict(self) -> dict[str, float]:
        return {"x0": self.x0, "y0": self.y0, "x1": self.x1, "y1": self.y1}


@dataclass
class Block:
    text: str
    bbox: BBox | None = None
    cell_ref: str | None = None  # e.g. "Sheet1!A7:D7" or "row 7"
    confidence: Decimal | None = None  # OCR only
    # Table rows (spreadsheets, CSV, Word tables): every cell in column order,
    # empty ones included, so values line up with their headers. In memory only.
    cells: list[str] | None = None
    char_start: int = 0
    char_end: int = 0


@dataclass
class Page:
    index: int  # 1-based
    blocks: list[Block] = field(default_factory=list)
    label: str = ""  # e.g. sheet name
    width: Decimal | None = None  # in points for PDFs, pixels for images
    height: Decimal | None = None
    parser: str = ""
    parser_version: str = ""
    needs_ocr: bool = False  # no text layer: send to the OCR provider

    @property
    def text(self) -> str:
        return "\n".join(b.text for b in self.blocks)

    def assign_spans(self) -> None:
        """Set each block's span within ``text`` (blocks joined by one newline)."""
        offset = 0
        for block in self.blocks:
            block.char_start = offset
            block.char_end = offset + len(block.text)
            offset = block.char_end + 1
