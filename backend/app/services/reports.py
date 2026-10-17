"""Reports (17-19) and the findings export (50).

Both are built only from stored claims, findings and review actions, never
from fresh model output (Docs/backend_plan.md, Step 14).
"""

import csv
import io
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.principal import Principal
from app.db.models import (
    AuditEvent,
    Case,
    EvidenceFile,
    ExtractedClaim,
    Finding,
    Report,
    ReviewAction,
    User,
)
from app.enums import ReportFormat, ReportStatus
from app.ids import new_id
from app.services import audit, outbox

EXPORT_SCHEMA_VERSION = "1.0"
REPORT_TEMPLATE_VERSION = "outline-1"
ReportType = Literal["summary", "detailed", "executive"]
DocumentFormat = Literal["pdf", "docx"]
# File extension and media type of each report format.
REPORT_FILES = {
    ReportFormat.PDF: ("pdf", "application/pdf"),
    ReportFormat.DOCX: (
        "docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ),
}
SECTIONS = ("evidence", "timeline", "findings", "recommendations")
GENERATE_REPORT = "report.generate"


def request_report(
    db: Session,
    principal: Principal,
    case: Case,
    report_type: ReportType,
    sections: list[str],
    report_format: DocumentFormat = "pdf",
) -> Report:
    report = Report(
        id=new_id("rpt"),
        tenant_id=case.tenant_id,
        case_id=case.id,
        format=ReportFormat(report_format),
        status=ReportStatus.QUEUED,
        title=f"{case.reference} {report_type.capitalize()} report",
        template_version=REPORT_TEMPLATE_VERSION,
        options={"type": report_type, "sections": [s for s in SECTIONS if s in sections]},
        requested_by=principal.user_id,
    )
    db.add(report)
    db.flush()
    outbox.emit(db, case.tenant_id, "processing", GENERATE_REPORT, {"report_id": report.id})
    audit.record(
        db,
        principal,
        "report.requested",
        "report",
        report.id,
        case_id=case.id,
        details={
            "title": f"Requested {report.title} ({report_format.upper()})",
            "type": report_type,
            "format": report_format,
        },
    )
    return report


def _review_lookup(db: Session, finding_ids: list[str]) -> dict[str, tuple[ReviewAction, User]]:
    latest: dict[str, tuple[ReviewAction, User]] = {}
    for action, user in db.execute(
        select(ReviewAction, User)
        .join(User, User.id == ReviewAction.actor_user_id)
        .where(ReviewAction.finding_id.in_(finding_ids))
        .order_by(ReviewAction.created_at.desc(), ReviewAction.id.desc())
    ).all():
        latest.setdefault(action.finding_id, (action, user))
    return latest


def export_data(db: Session, case: Case) -> dict[str, Any]:
    """The JSON export (Docs/Integrations_API_Contract.md §6)."""
    findings = db.scalars(
        select(Finding).where(Finding.case_id == case.id).order_by(Finding.created_at, Finding.id)
    ).all()
    claim_ids = {cid for f in findings for cid in f.input_claim_ids}
    claims = {
        c.id: (c, name)
        for c, name in db.execute(
            select(ExtractedClaim, EvidenceFile.original_filename)
            .join(EvidenceFile, EvidenceFile.id == ExtractedClaim.evidence_id)
            .where(ExtractedClaim.id.in_(claim_ids))
        ).all()
    }
    reviews = _review_lookup(db, [f.id for f in findings])
    out_findings = []
    for f in findings:
        action, user = reviews.get(f.id, (None, None))
        review: dict[str, Any] = {
            "status": f.review_status,
            "by": user.name if user else None,
            "at": action.created_at.isoformat() if action else None,
            "note": action.note if action else "",
        }
        if action and action.correction:
            review["correction"] = action.correction
        evidence = []
        for cid in f.input_claim_ids:
            if cid not in claims:
                continue
            claim, file_name = claims[cid]
            evidence.append(
                {
                    "claim_id": claim.id,
                    "field": claim.field_name,
                    "value": claim.normalized_value
                    if claim.normalized_value is not None
                    else claim.raw_value,
                    "evidence_id": claim.evidence_id,
                    "file_name": file_name,
                    "page": claim.page_index,
                    "quote": claim.source_quote,
                    "grounding": claim.grounding,
                    # A relative API path, never a signed URL: those expire and leak.
                    "source_url": f"/v1/cases/{case.id}/evidence/{claim.evidence_id}/view-url"
                    + (f"?page={claim.page_index}" if claim.page_index else ""),
                }
            )
        out_findings.append(
            {
                "id": f.id,
                "rule_id": f.rule_id,
                "rule_version": f.rule_version,
                "type": f.finding_type,
                "severity": f.severity,
                "expected": f.expected,
                "observed": f.observed,
                "difference": f.difference,
                "review": review,
                "explanation": f.explanation,
                "explanation_source": f.explanation_source,
                "evidence": evidence,
            }
        )
    return {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "case": {
            "id": case.id,
            "reference": case.reference,
            "title": case.title,
            "status": case.status,
        },
        "generated_at": datetime.now(UTC).isoformat(),
        "findings": out_findings,
    }


CSV_HEADER = [
    "case_id",
    "case_reference",
    "finding_id",
    "rule_id",
    "rule_version",
    "severity",
    "expected",
    "observed",
    "difference",
    "unit",
    "review_status",
    "reviewed_by",
    "reviewed_at",
    "evidence",
]


def _safe_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    # Spreadsheet apps treat cells starting with these as formulas (CSV injection).
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def export_csv(data: dict[str, Any]) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")  # RFC 4180
    writer.writerow(CSV_HEADER)
    case = data["case"]
    for f in data["findings"]:
        diff = f.get("difference") or {}
        writer.writerow(
            [
                _safe_cell(v)
                for v in (
                    case["id"],
                    case["reference"],
                    f["id"],
                    f["rule_id"],
                    f["rule_version"],
                    f["severity"],
                    (f.get("expected") or {}).get("value"),
                    (f.get("observed") or {}).get("value"),
                    diff.get("value"),
                    diff.get("unit"),
                    f["review"]["status"],
                    f["review"]["by"],
                    f["review"]["at"],
                    "; ".join(f"{e['file_name']} p{e['page']}" for e in f["evidence"]),
                )
            ]
        )
    return out.getvalue()


def timeline(db: Session, case: Case, limit: int = 200) -> list[tuple[AuditEvent, User | None]]:
    rows = db.execute(
        select(AuditEvent, User)
        .outerjoin(User, User.id == AuditEvent.actor_user_id)
        .where(AuditEvent.case_id == case.id)
        .order_by(AuditEvent.created_at, AuditEvent.id)
        .limit(limit)
    ).all()
    return [(e, u) for e, u in rows]
