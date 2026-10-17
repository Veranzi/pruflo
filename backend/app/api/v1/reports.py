"""Reports and the findings export: endpoints 17, 18, 19 and 50.

Investigators, reviewers and admins only; viewers get 403.
"""

import json
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.v1.cases import PersonRef
from app.auth.permissions import Permission
from app.auth.principal import Principal, load_case, require
from app.db.models import Report, User
from app.db.session import get_db
from app.enums import ReportFormat, ReportStatus
from app.errors import ApiError, not_found
from app.pagination import Page, after_cursor, clamp_limit, next_cursor_for
from app.services import audit
from app.services import reports as report_service
from app.storage.backend import Storage, get_storage
from app.worker.queue import Queue, get_queue

router = APIRouter(prefix="/cases/{case_id}", tags=["reports"])
EXPORT = Depends(require(Permission.EXPORT))

Section = Literal["evidence", "timeline", "findings", "recommendations"]
ALL_SECTIONS: tuple[Section, ...] = ("evidence", "timeline", "findings", "recommendations")


class ReportIn(BaseModel):
    type: report_service.ReportType = "detailed"
    format: report_service.DocumentFormat = "pdf"
    sections: list[Section] = Field(default_factory=lambda: list(ALL_SECTIONS), max_length=4)


class ReportOut(BaseModel):
    id: str
    case_id: str
    title: str
    type: str
    format: ReportFormat
    status: ReportStatus
    requested_by: PersonRef | None
    created_at: datetime
    completed_at: datetime | None


class DownloadOut(BaseModel):
    url: str
    expires_at: datetime


def _out(report: Report, user: User | None) -> ReportOut:
    return ReportOut(
        id=report.id,
        case_id=report.case_id,
        title=report.title,
        type=str(report.options.get("type", "detailed")),
        format=ReportFormat(report.format),
        status=ReportStatus(report.status),
        requested_by=PersonRef(uid=user.id, name=user.name) if user else None,
        created_at=report.created_at,
        completed_at=report.completed_at,
    )


@router.post("/reports", response_model=ReportOut, status_code=202)
def create_report(
    case_id: str,
    body: ReportIn,
    background: BackgroundTasks,
    principal: Principal = EXPORT,
    db: Session = Depends(get_db),
    queue: Queue = Depends(get_queue),
) -> ReportOut:
    """17: queue a PDF or Word report. Poll the list (18) until it's ready."""
    case = load_case(db, principal, case_id)
    report = report_service.request_report(
        db, principal, case, body.type, list(body.sections), body.format
    )
    db.commit()
    db.refresh(report)
    background.add_task(queue.kick)
    return _out(report, principal.user)


@router.get("/reports", response_model=Page[ReportOut])
def list_reports(
    case_id: str,
    cursor: str | None = None,
    limit: int | None = None,
    principal: Principal = EXPORT,
    db: Session = Depends(get_db),
) -> Page[ReportOut]:
    """18"""
    case = load_case(db, principal, case_id)
    size = clamp_limit(limit)
    rows = db.execute(
        after_cursor(
            select(Report, User)
            .outerjoin(User, User.id == Report.requested_by)
            .where(Report.case_id == case.id)
            .order_by(Report.created_at.desc(), Report.id.desc())
            .limit(size + 1),
            Report.created_at,
            Report.id,
            cursor,
        )
    ).all()
    return Page[ReportOut](
        items=[_out(r, u) for r, u in rows[:size]],
        next_cursor=next_cursor_for(rows, size, lambda r: (r[0].created_at, r[0].id)),
    )


@router.get("/reports/{report_id}/download-url", response_model=DownloadOut)
def download_url(
    case_id: str,
    report_id: str,
    disposition: Literal["attachment", "inline"] = Query(default="attachment"),
    principal: Principal = EXPORT,
    db: Session = Depends(get_db),
    storage: Storage = Depends(get_storage),
) -> DownloadOut:
    """19: a short-lived link to the file. ``inline`` opens it in the browser
    (a preview, for PDFs); ``attachment`` downloads it."""
    case = load_case(db, principal, case_id)
    report = db.get(Report, report_id)
    if report is None or report.case_id != case.id:
        raise not_found("Report")
    if report.status != ReportStatus.READY or not report.object_key:
        raise ApiError(409, "report_not_ready", "This report isn't ready yet.")
    extension, media_type = report_service.REPORT_FILES[ReportFormat(report.format)]
    signed = storage.view_url(
        report.object_key,
        f"{report.title}.{extension}",
        media_type,
        inline=disposition == "inline",
    )
    action = "report.previewed" if disposition == "inline" else "report.downloaded"
    audit.record(db, principal, action, "report", report.id, case_id=case.id)
    db.commit()
    return DownloadOut(url=signed.url, expires_at=signed.expires_at)


@router.get("/export")
def export(
    case_id: str,
    format: Literal["json", "csv"] = Query(default="json"),
    principal: Principal = EXPORT,
    db: Session = Depends(get_db),
) -> Response:
    """50: findings as JSON or CSV for other systems (API key scope findings:read)."""
    case = load_case(db, principal, case_id)
    data = report_service.export_data(db, case)
    audit.record(
        db,
        principal,
        "case.exported",
        "case",
        case.id,
        case_id=case.id,
        details={"format": format, "findings": len(data["findings"])},
    )
    db.commit()
    filename = f"case-{case.reference}-findings.{format}"
    if format == "csv":
        body, media = report_service.export_csv(data), "text/csv; charset=utf-8"
    else:
        body, media = json.dumps(data, indent=2, ensure_ascii=False), "application/json"
    return Response(
        content=body.encode("utf-8"),
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
