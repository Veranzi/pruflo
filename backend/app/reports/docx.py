"""Render a case report as a Word document (.docx) with python-docx. The
content comes from ``app.reports.outline``, so it matches the PDF."""

import io
from datetime import UTC, datetime
from typing import Any

from docx import Document
from docx.enum.text import WD_TAB_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

from app.db.models import AuditEvent, Case, EvidenceFile, User
from app.reports import outline


class _Docx:
    def __init__(self, header_text: str, footer_text: str) -> None:
        self.doc = Document()
        style = self.doc.styles["Normal"]
        style.font.name = "Calibri"
        style.font.size = Pt(10)
        section = self.doc.sections[0]
        section.left_margin = section.right_margin = Cm(1.8)
        width = (section.page_width or Cm(21)) - Cm(3.6)  # A4/Letter less both margins

        # Header: logo and name on the left, the case on the right, a brand rule below.
        head = section.header.paragraphs[0]
        head.paragraph_format.tab_stops.add_tab_stop(width, WD_TAB_ALIGNMENT.RIGHT)
        if outline.LOGO.exists():
            head.add_run().add_picture(str(outline.LOGO), height=Cm(0.6))
        brand = head.add_run(f"  {outline.BRAND}")
        brand.bold = True
        brand.font.size = Pt(12)
        brand.font.color.rgb = RGBColor(*outline.NAVY)
        case_run = head.add_run(f"\t{header_text}")
        case_run.font.size = Pt(8.5)
        case_run.font.color.rgb = _grey(110)
        _bottom_border(head, outline.BLUE)

        # Footer: the same line as the PDF, with the page number.
        foot = section.footer.paragraphs[0]
        text = foot.add_run(f"{footer_text}  ·  page ")
        text.font.size = Pt(8)
        text.font.color.rgb = _grey(110)
        _page_number(foot)

    def heading(self, text: str, size: float = 13) -> None:
        paragraph = self.doc.add_heading(level=1 if size > 15 else 2)
        run = paragraph.add_run(text)
        run.font.size = Pt(size)
        run.font.color.rgb = _grey(20)

    def para(self, text: str, size: float = 10, bold: bool = False, color: int = 40) -> None:
        run = self.doc.add_paragraph().add_run(text)
        run.bold = bold
        run.font.size = Pt(size)
        run.font.color.rgb = _grey(color)

    def row(self, label: str, value: Any) -> None:
        paragraph = self.doc.add_paragraph()
        name = paragraph.add_run(f"{label}: ")
        name.bold = True
        name.font.color.rgb = _grey(70)
        paragraph.add_run("" if value is None else str(value))

    def gap(self, mm: float) -> None:
        self.doc.add_paragraph().paragraph_format.space_after = Pt(mm * 2)

    def output(self) -> bytes:
        out = io.BytesIO()
        self.doc.save(out)
        return out.getvalue()


def _grey(level: int) -> RGBColor:
    return RGBColor(level, level, level)


def _bottom_border(paragraph: Any, rgb: tuple[int, int, int]) -> None:
    borders = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    for key, value in (("w:val", "single"), ("w:sz", "8"), ("w:space", "4")):
        bottom.set(qn(key), value)
    bottom.set(qn("w:color"), "{:02X}{:02X}{:02X}".format(*rgb))
    borders.append(bottom)
    paragraph._p.get_or_add_pPr().append(borders)


def _page_number(paragraph: Any) -> None:
    field = OxmlElement("w:fldSimple")
    field.set(qn("w:instr"), "PAGE")
    run = OxmlElement("w:r")
    text = OxmlElement("w:t")
    text.text = "1"
    run.append(text)
    field.append(run)
    paragraph._p.append(field)


def render(
    case: Case,
    options: dict[str, Any],
    evidence: list[EvidenceFile],
    export: dict[str, Any],
    timeline: list[tuple[AuditEvent, User | None]],
) -> bytes:
    generated = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    doc = _Docx(outline.header_text(case), outline.footer_text(case, generated))
    outline.write(doc, case, options, evidence, export, timeline, generated)
    return doc.output()
