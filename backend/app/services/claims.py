"""Extract, validate and ground claims for one evidence file (Steps 9 and 10).

Runs in the processing job, in the same transaction as the page text, so a
file's pages and claims always belong together. Claims are what the
extractor said, kept unchanged; reviewers' corrections are review actions.
"""

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import EvidenceFile, ExtractedClaim
from app.enums import GroundingStatus
from app.ids import new_id
from app.pipeline.claims import extractor_for
from app.pipeline.claims.schema import SCHEMA_VERSION, validate
from app.pipeline.document import Page
from app.pipeline.grounding import ground, index

TEXT_PARSERS = {"pdf_text", "docx", "xlsx", "csv", "txt", "image"}


def extract_claims(db: Session, evidence: EvidenceFile, pages: list[Page]) -> list[ExtractedClaim]:
    """Raises the extractor's TransientError or PermanentError unchanged."""
    if not any(p.blocks for p in pages):
        return []
    extractor, region = extractor_for(db, evidence.tenant_id)
    proposed = extractor.extract(pages, evidence.original_filename)
    day_first = get_settings().dates_day_first
    ocr_version = next((p.parser_version for p in pages if p.parser not in TEXT_PARSERS), None)

    # A rerun replaces this file's claims; nothing else refers to claims of a
    # file that hasn't finished processing.
    db.execute(delete(ExtractedClaim).where(ExtractedClaim.evidence_id == evidence.id))
    pages_index = index(pages)
    seen: set[tuple[str, str | None, str, str]] = set()
    rows: list[ExtractedClaim] = []
    for raw in proposed:
        claim = validate(raw, day_first)
        if claim is None:
            continue
        key = (claim.field, claim.item_key, claim.raw_value, claim.quote)
        if key in seen:
            continue
        seen.add(key)
        # A document type is a classification, not text: its quote (the title
        # line) must be on the page, but needn't contain "purchase_order".
        supports = claim.quote if claim.field == "document_type" else claim.raw_value
        found = ground(claim.quote, supports, claim.page, pages_index)
        status = found.status
        if claim.status != "found" and status == GroundingStatus.GROUNDED:
            # Traced to the page, but the value couldn't be read as its type.
            status = GroundingStatus.NEEDS_REVIEW
        row = ExtractedClaim(
            id=new_id("clm"),
            tenant_id=evidence.tenant_id,
            case_id=evidence.case_id,
            evidence_id=evidence.id,
            field_name=claim.field,
            item_key=claim.item_key,
            context=claim.context,
            raw_value=claim.raw_value,
            normalized_value=claim.normalized_value,
            data_type=claim.data_type.value,
            unit=claim.unit,
            source_quote=claim.quote,
            page_index=found.page or claim.page,
            span_start=found.span_start,
            span_end=found.span_end,
            bbox=found.bbox,
            grounding=status,
            extraction_status=claim.status,
            confidence=claim.confidence,
            provider=extractor.name,
            model=extractor.model,
            prompt_version=extractor.prompt_version,
            schema_version=SCHEMA_VERSION,
            ocr_version=ocr_version,
            region=region,
        )
        db.add(row)
        rows.append(row)
    db.flush()
    return rows
