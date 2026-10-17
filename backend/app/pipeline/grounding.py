"""Grounding: trace every claim back to the page it came from
(Docs/backend_plan.md, Step 10).

A claim's quote is searched for in the page text, ignoring only differences
in whitespace and line wrapping ("deliv-\\nered"). Then:

- exactly one match: ``grounded``, with the span and the bounding box of the
  lines it covers;
- several matches: ``ambiguous``;
- none, or a quote that doesn't contain the claimed value: ``ungrounded``;
- a quote with no words at all (a bare "80"): ``ambiguous``, because a
  number without its label or row can't be trusted.

Only grounded claims feed the rules; the rest are shown for review.
"""

import re
from dataclasses import dataclass

from app.enums import GroundingStatus
from app.pipeline.document import Page

_WRAP = re.compile(r"(?<=[a-z])-\n(?=[a-z])")
_SPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class Grounding:
    status: GroundingStatus
    page: int | None = None
    span_start: int | None = None
    span_end: int | None = None
    bbox: dict[str, float] | None = None


def _normalise(text: str) -> tuple[str, list[int]]:
    """Collapse whitespace and join wrapped words. Returns the new text and,
    for each of its characters, the index of the original character."""
    out: list[str] = []
    origin: list[int] = []
    index = 0
    wraps = {m.start() for m in _WRAP.finditer(text)}
    while index < len(text):
        if index in wraps:
            index += 2  # drop "-\n"
            continue
        char = text[index]
        if char.isspace():
            end = index
            while end < len(text) and text[end].isspace():
                end += 1
            if out and end < len(text):
                out.append(" ")
                origin.append(index)
            index = end
            continue
        out.append(char)
        origin.append(index)
        index += 1
    return "".join(out), origin


def _occurrences(haystack: str, needle: str) -> list[int]:
    found: list[int] = []
    start = haystack.find(needle)
    while start != -1:
        found.append(start)
        start = haystack.find(needle, start + 1)
    return found


def _bbox(page: Page, start: int, end: int) -> dict[str, float] | None:
    boxes = [
        b.bbox
        for b in page.blocks
        if b.bbox is not None and b.char_start < end and b.char_end > start
    ]
    if not boxes:
        return None
    return {
        "x0": min(b.x0 for b in boxes),
        "y0": min(b.y0 for b in boxes),
        "x1": max(b.x1 for b in boxes),
        "y1": max(b.y1 for b in boxes),
    }


@dataclass(frozen=True)
class PageIndex:
    """Each page's normalised text, built once per file: a 10,000-row sheet
    has as many claims, and each would otherwise renormalise the page."""

    pages: list[tuple[Page, str, list[int]]]


def index(pages: list[Page]) -> PageIndex:
    return PageIndex([(page, *_normalise(page.text)) for page in pages])


def ground(
    quote: str, value: str, page_hint: int | None, pages: list[Page] | PageIndex
) -> Grounding:
    needle, _ = _normalise(quote.strip())
    wanted, _ = _normalise(value.strip())
    if not needle or not wanted or wanted not in needle:
        return Grounding(GroundingStatus.UNGROUNDED)
    if not re.search(r"[A-Za-z]", needle):
        return Grounding(GroundingStatus.AMBIGUOUS, page_hint)

    prepared = pages if isinstance(pages, PageIndex) else index(pages)
    searched = [
        (page, text, origin, _occurrences(text, needle)) for page, text, origin in prepared.pages
    ]
    hinted = [s for s in searched if s[0].index == page_hint]
    for candidates in (hinted, searched):
        matches = [(page, origin, at) for page, _, origin, hits in candidates for at in hits]
        if len(matches) > 1:
            return Grounding(GroundingStatus.AMBIGUOUS, page_hint)
        if len(matches) == 1:
            page, origin, at = matches[0]
            start, end = origin[at], origin[at + len(needle) - 1] + 1
            return Grounding(
                GroundingStatus.GROUNDED, page.index, start, end, _bbox(page, start, end)
            )
    return Grounding(GroundingStatus.UNGROUNDED)
