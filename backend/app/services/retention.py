"""Retention and deletion (Docs/backend_plan.md, Step 17).

Runs daily from Cloud Scheduler (``/tasks/retention`` on the worker) or
``python -m app.worker retention``. Both periods default to 0, which keeps
everything until a retention period is agreed.

- Reports older than ``report_retention_days``: the PDF and the row go.
- Evidence of cases completed more than ``evidence_retention_days`` ago:
  the stored file and its page text go. The row stays, with its name and
  checksum, because the audit trail and the findings' claims refer to it.

Each deletion is audited.
"""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import Case, DocumentPage, EvidenceFile, Report
from app.enums import CaseStatus
from app.services import audit
from app.storage.backend import Storage

logger = logging.getLogger("pruflo.retention")
BATCH = 200


def purge_reports(db: Session, storage: Storage, now: datetime | None = None) -> int:
    days = get_settings().report_retention_days
    if days <= 0:
        return 0
    cutoff = (now or datetime.now(UTC)) - timedelta(days=days)
    reports = db.scalars(select(Report).where(Report.created_at < cutoff).limit(BATCH)).all()
    for report in reports:
        if report.object_key:
            storage.delete(report.object_key)
        audit.record_system(
            db,
            report.tenant_id,
            "report.purged",
            "report",
            report.id,
            case_id=report.case_id,
            details={"title": f"{report.title} deleted after {days} days"},
        )
        db.delete(report)
    db.commit()
    return len(reports)


def purge_evidence(db: Session, storage: Storage, now: datetime | None = None) -> int:
    days = get_settings().evidence_retention_days
    if days <= 0:
        return 0
    cutoff = (now or datetime.now(UTC)) - timedelta(days=days)
    rows = db.execute(
        select(EvidenceFile, Case.reference)
        .join(Case, Case.id == EvidenceFile.case_id)
        .where(
            Case.status == CaseStatus.COMPLETED,
            Case.updated_at < cutoff,
            EvidenceFile.purged_at.is_(None),
        )
        .limit(BATCH)
    ).all()
    when = now or datetime.now(UTC)
    for evidence, reference in rows:
        storage.delete(evidence.object_key)
        db.execute(delete(DocumentPage).where(DocumentPage.evidence_id == evidence.id))
        evidence.purged_at = when
        audit.record_system(
            db,
            evidence.tenant_id,
            "evidence.purged",
            "evidence",
            evidence.id,
            case_id=evidence.case_id,
            details={
                "title": f"{evidence.original_filename} deleted after {days} days",
                "sha256": evidence.sha256,
                "case": reference,
            },
        )
    db.commit()
    return len(rows)


def run(db: Session, storage: Storage) -> dict[str, int]:
    totals = {"reports": purge_reports(db, storage), "evidence": purge_evidence(db, storage)}
    if any(totals.values()):
        logger.info("retention: %s", totals)
    return totals
