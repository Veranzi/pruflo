"""Render a case report as a PDF with fpdf2 (pure Python; no browser or system
libraries beyond a Unicode font). The content comes from ``app.reports.outline``.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fpdf import FPDF
from fpdf.enums import XPos, YPos

from app.db.models import AuditEvent, Case, EvidenceFile, User
from app.reports import outline

FONT_DIR = Path("/usr/share/fonts/truetype/dejavu")  # fonts-dejavu-core on Debian


class _Pdf(FPDF):
    def __init__(self, header_text: str, footer_text: str) -> None:
        super().__init__(format="A4")
        self.header_text = header_text
        self.footer_text = footer_text
        regular, bold = FONT_DIR / "DejaVuSans.ttf", FONT_DIR / "DejaVuSans-Bold.ttf"
        if regular.exists() and bold.exists():
            self.add_font("Body", "", str(regular))
            self.add_font("Body", "B", str(bold))
            self.body_font = "Body"
            self.unicode_ok = True
        else:  # Latin-1 core font; other characters are replaced
            self.body_font = "Helvetica"
            self.unicode_ok = False
        self.set_auto_page_break(auto=True, margin=18)
        self.set_margins(18, 26, 18)  # room for the header

    def text_safe(self, text: Any) -> str:
        value = "" if text is None else str(text)
        return value if self.unicode_ok else value.encode("latin-1", "replace").decode("latin-1")

    def header(self) -> None:
        # Logo and name on the left, the case on the right, a brand rule below.
        if outline.LOGO.exists():
            self.image(str(outline.LOGO), x=18, y=10, h=6)
        self.set_xy(33, 10)
        self.set_font(self.body_font, "B", 12)
        self.set_text_color(*outline.NAVY)
        self.cell(40, 6, outline.BRAND)
        self.set_xy(18, 10)
        self.set_font(self.body_font, "", 8.5)
        self.set_text_color(110)
        self.cell(0, 6, self.text_safe(self.header_text), align="R")
        self.set_draw_color(*outline.BLUE)
        self.set_line_width(0.5)
        self.line(18, 18.5, 192, 18.5)
        self.set_y(26)

    def footer(self) -> None:
        self.set_draw_color(220)
        self.set_line_width(0.2)
        self.line(18, self.h - 14, 192, self.h - 14)
        self.set_y(-12)
        self.set_font(self.body_font, "", 8)
        self.set_text_color(110)
        self.cell(0, 6, self.text_safe(f"{self.footer_text}  ·  page {self.page_no()}"), align="C")

    def heading(self, text: str, size: float = 13) -> None:
        self.ln(3)
        self.set_font(self.body_font, "B", size)
        self.set_text_color(20)
        self.multi_cell(0, 7, self.text_safe(text), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.ln(1)

    def para(self, text: str, size: float = 10, bold: bool = False, color: int = 40) -> None:
        self.set_font(self.body_font, "B" if bold else "", size)
        self.set_text_color(color)
        self.multi_cell(0, 5.2, self.text_safe(text), new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    def gap(self, mm: float) -> None:
        self.ln(mm)

    def row(self, label: str, value: Any) -> None:
        self.set_font(self.body_font, "B", 9.5)
        self.set_text_color(70)
        self.cell(42, 5.5, self.text_safe(label))
        self.set_font(self.body_font, "", 9.5)
        self.set_text_color(30)
        self.multi_cell(0, 5.5, self.text_safe(value), new_x=XPos.LMARGIN, new_y=YPos.NEXT)


def render(
    case: Case,
    options: dict[str, Any],
    evidence: list[EvidenceFile],
    export: dict[str, Any],
    timeline: list[tuple[AuditEvent, User | None]],
) -> bytes:
    generated = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    pdf = _Pdf(outline.header_text(case), outline.footer_text(case, generated))
    pdf.add_page()
    outline.write(pdf, case, options, evidence, export, timeline, generated)
    return bytes(pdf.output())
