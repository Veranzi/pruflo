"""Evidence: endpoints 7, 8, 9, 10 and 11."""

from datetime import datetime
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.permissions import Permission
from app.auth.principal import Principal, load_case, require
from app.db.models import Case, DocumentPage, EvidenceFile, TextBlock
from app.db.session import get_db
from app.enums import EvidenceStatus
from app.errors import ApiError, not_found
from app.pagination import Page, after_cursor, clamp_limit, next_cursor_for
from app.services import evidence as evidence_service
from app.storage.backend import Storage, get_storage
from app.worker.queue import Queue, get_queue

router = APIRouter(prefix="/cases/{case_id}/evidence", tags=["evidence"])

EvidenceKind = Literal["documents", "images", "other"]


class UploadUrlIn(BaseModel):
    file_name: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(ge=1)
    content_type: str = Field(min_length=1, max_length=120)
    evidence_types: list[EvidenceKind] = Field(min_length=1)


class UploadUrlOut(BaseModel):
    evidence_id: str
    upload_url: str
    method: str
    headers: dict[str, str]  # send these with the PUT, or storage rejects it
    expires_at: datetime


class EvidenceOut(BaseModel):
    id: str
    case_id: str
    file_name: str
    content_type: str
    size_bytes: int
    page_count: int | None
    evidence_types: list[str]
    status: EvidenceStatus
    error_class: str | None
    sha256: str | None
    uploaded_at: datetime | None
    created_at: datetime


class ViewUrlOut(BaseModel):
    url: str
    expires_at: datetime


def _out(evidence: EvidenceFile) -> EvidenceOut:
    return EvidenceOut(
        id=evidence.id,
        case_id=evidence.case_id,
        file_name=evidence.original_filename,
        content_type=evidence.detected_mime or evidence.declared_mime,
        size_bytes=evidence.byte_size,
        page_count=evidence.page_count,
        evidence_types=evidence.evidence_types,
        status=EvidenceStatus(evidence.status),
        error_class=evidence.error_class,
        sha256=evidence.sha256,
        uploaded_at=evidence.uploaded_at,
        created_at=evidence.created_at,
    )


def _load_evidence(db: Session, case: Case, evidence_id: str) -> EvidenceFile:
    evidence = db.get(EvidenceFile, evidence_id)
    if evidence is None or evidence.case_id != case.id:
        raise not_found("Evidence")
    return evidence


@router.post("/upload-url", response_model=UploadUrlOut, status_code=201)
def create_upload_url(
    case_id: str,
    body: UploadUrlIn,
    principal: Principal = Depends(require(Permission.UPLOAD_EVIDENCE)),
    db: Session = Depends(get_db),
    storage: Storage = Depends(get_storage),
) -> UploadUrlOut:
    """7: returns a short-lived signed URL. Then PUT the file, then call ``complete``."""
    case = load_case(db, principal, case_id)
    evidence, signed = evidence_service.create_upload(
        db,
        principal,
        case,
        file_name=body.file_name.strip(),
        size_bytes=body.size_bytes,
        content_type=body.content_type,
        evidence_types=list(body.evidence_types),
        storage=storage,
    )
    db.commit()
    return UploadUrlOut(
        evidence_id=evidence.id,
        upload_url=signed.url,
        method=signed.method,
        headers=signed.headers,
        expires_at=signed.expires_at,
    )


@router.post("/{evidence_id}/complete", response_model=EvidenceOut)
def complete_upload(
    case_id: str,
    evidence_id: str,
    background: BackgroundTasks,
    principal: Principal = Depends(require(Permission.UPLOAD_EVIDENCE)),
    db: Session = Depends(get_db),
    storage: Storage = Depends(get_storage),
    queue: Queue = Depends(get_queue),
) -> EvidenceOut:
    """8: verify the uploaded file on the server and queue it for processing."""
    case = load_case(db, principal, case_id)
    evidence = _load_evidence(db, case, evidence_id)
    out = _out(evidence_service.complete_upload(db, principal, case, evidence, storage))
    background.add_task(queue.kick)  # after the response; the outbox keeps it safe
    return out


@router.get("", response_model=Page[EvidenceOut])
def list_evidence(
    case_id: str,
    cursor: str | None = None,
    limit: int | None = None,
    principal: Principal = Depends(require(Permission.VIEW_CASES)),
    db: Session = Depends(get_db),
) -> Page[EvidenceOut]:
    """9: the case's files and their processing status, newest first."""
    case = load_case(db, principal, case_id)
    size = clamp_limit(limit)
    query = after_cursor(
        select(EvidenceFile)
        .where(EvidenceFile.case_id == case.id)
        .order_by(EvidenceFile.created_at.desc(), EvidenceFile.id.desc())
        .limit(size + 1),
        EvidenceFile.created_at,
        EvidenceFile.id,
        cursor,
    )
    rows = db.scalars(query).all()
    return Page[EvidenceOut](
        items=[_out(e) for e in rows[:size]],
        next_cursor=next_cursor_for(rows, size, lambda e: (e.created_at, e.id)),
    )


@router.post("/{evidence_id}/retry", response_model=EvidenceOut)
def retry_evidence(
    case_id: str,
    evidence_id: str,
    background: BackgroundTasks,
    principal: Principal = Depends(require(Permission.UPLOAD_EVIDENCE)),
    db: Session = Depends(get_db),
    queue: Queue = Depends(get_queue),
) -> EvidenceOut:
    """10: queue a failed file again. Rejected files (wrong type, too big) can't be retried."""
    case = load_case(db, principal, case_id)
    evidence = _load_evidence(db, case, evidence_id)
    out = _out(evidence_service.retry(db, principal, case, evidence))
    background.add_task(queue.kick)
    return out


@router.get("/{evidence_id}/view-url", response_model=ViewUrlOut)
def view_url(
    case_id: str,
    evidence_id: str,
    principal: Principal = Depends(require(Permission.VIEW_CASES)),
    db: Session = Depends(get_db),
    storage: Storage = Depends(get_storage),
) -> ViewUrlOut:
    """11: a short-lived link to the original file. ``?page=`` is for the viewer only."""
    case = load_case(db, principal, case_id)
    evidence = _load_evidence(db, case, evidence_id)
    if evidence.status == EvidenceStatus.UPLOADING:
        raise not_found("Evidence")
    if evidence.purged_at is not None:
        raise ApiError(410, "evidence_purged", "This file was deleted under the retention policy.")
    signed = storage.view_url(
        evidence.object_key,
        evidence.original_filename,
        evidence.detected_mime or evidence.declared_mime,
    )
    return ViewUrlOut(url=signed.url, expires_at=signed.expires_at)


class BlockOut(BaseModel):
    id: str
    index: int
    text: str
    char_start: int
    char_end: int
    bbox: dict[str, float] | None  # normalized 0..1: x0, y0 top-left; x1, y1 bottom-right
    cell_ref: str | None
    confidence: Decimal | None


class PageSummary(BaseModel):
    index: int
    label: str
    width: Decimal | None
    height: Decimal | None
    parser: str
    block_count: int


class PageDetail(PageSummary):
    text: str
    blocks: list[BlockOut]


class AnchorsOut(BaseModel):
    evidence_id: str
    status: EvidenceStatus
    pages: list[PageSummary]
    page: PageDetail | None  # filled when ?page= is given


@router.get("/{evidence_id}/anchors", response_model=AnchorsOut)
def anchors(
    case_id: str,
    evidence_id: str,
    page: int | None = Query(default=None, ge=1),
    principal: Principal = Depends(require(Permission.VIEW_CASES)),
    db: Session = Depends(get_db),
) -> AnchorsOut:
    """12: extracted pages, and one page's text and blocks for source highlighting."""
    case = load_case(db, principal, case_id)
    evidence = _load_evidence(db, case, evidence_id)
    counts: dict[str, int] = {
        page_id: count
        for page_id, count in db.execute(
            select(TextBlock.page_id, func.count())
            .where(TextBlock.evidence_id == evidence.id)
            .group_by(TextBlock.page_id)
        ).all()
    }
    rows = db.scalars(
        select(DocumentPage)
        .where(DocumentPage.evidence_id == evidence.id)
        .order_by(DocumentPage.page_index)
    ).all()

    def summary(row: DocumentPage) -> dict[str, object]:
        return {
            "index": row.page_index,
            "label": row.label,
            "width": row.width,
            "height": row.height,
            "parser": row.parser,
            "block_count": counts.get(row.id, 0),
        }

    detail = None
    if page is not None:
        row = next((r for r in rows if r.page_index == page), None)
        if row is None:
            raise not_found("Page")
        blocks = db.scalars(
            select(TextBlock).where(TextBlock.page_id == row.id).order_by(TextBlock.block_index)
        ).all()
        detail = PageDetail(
            **summary(row),
            text=row.text,
            blocks=[
                BlockOut(
                    id=b.id,
                    index=b.block_index,
                    text=b.text,
                    char_start=b.char_start,
                    char_end=b.char_end,
                    bbox=b.bbox,
                    cell_ref=b.cell_ref,
                    confidence=b.confidence,
                )
                for b in blocks
            ],
        )
    return AnchorsOut(
        evidence_id=evidence.id,
        status=EvidenceStatus(evidence.status),
        pages=[PageSummary(**summary(r)) for r in rows],
        page=detail,
    )
