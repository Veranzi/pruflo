"""Process one evidence file (Docs/backend_plan.md, Steps 7 and 8).

Idempotent: a job is claimed with a lease, and its results replace any
earlier results for the same file, so a redelivered message never
duplicates pages, audit events or webhooks.
"""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from app.db.models import EvidenceFile, ProcessingJob
from app.enums import EvidenceStatus, JobStatus, WebhookEvent
from app.pipeline.errors import PermanentError, PipelineError, TransientError
from app.pipeline.extract import Extraction, extract
from app.pipeline.ocr import OcrProvider
from app.services import audit, outbox
from app.storage.backend import Storage
from app.storage.content import from_extraction, get_content_store

logger = logging.getLogger("pruflo.worker")

MAX_ATTEMPTS = 5
LEASE = timedelta(minutes=10)


class RetryLater(Exception):
    """Tell Pub/Sub to redeliver (nack). Its retry policy adds the backoff."""


def _claim(db: Session, job_id: str) -> int | None:
    """Take the job if nobody else holds it. Returns the attempt number, or None."""
    now = datetime.now(UTC)
    attempts = db.execute(
        update(ProcessingJob)
        .where(
            ProcessingJob.id == job_id,
            or_(
                ProcessingJob.status.in_([JobStatus.QUEUED, JobStatus.FAILED_RETRYABLE]),
                and_(ProcessingJob.status == JobStatus.RUNNING, ProcessingJob.lease_until < now),
            ),
        )
        .values(
            status=JobStatus.RUNNING,
            attempts=ProcessingJob.attempts + 1,
            lease_until=now + LEASE,
        )
        .returning(ProcessingJob.attempts)
    ).scalar_one_or_none()
    db.commit()
    return attempts


def run_processing_job(
    db: Session, storage: Storage, ocr: OcrProvider | None, evidence_id: str, job_id: str
) -> str:
    """Returns "done", "failed" or "skipped". Raises RetryLater to be redelivered."""
    attempt = _claim(db, job_id)
    if attempt is None:
        job = db.get(ProcessingJob, job_id)
        if job is not None and job.status == JobStatus.RUNNING:
            raise RetryLater(f"job {job_id} is held by another worker")
        return "skipped"  # unknown, finished or failed for good: nothing to do

    evidence = db.get(EvidenceFile, evidence_id)
    if evidence is None or evidence.status == EvidenceStatus.UPLOADING:
        _finish_job(db, job_id, JobStatus.FAILED_TERMINAL, "evidence_missing")
        return "skipped"
    case_id = evidence.case_id
    log = {"evidence_id": evidence.id, "case_id": case_id, "job_id": job_id}
    evidence.status = EvidenceStatus.EXTRACTING
    db.commit()

    try:
        try:
            data = storage.read(evidence.object_key)
        except Exception as exc:
            raise TransientError("storage_unavailable", str(exc)) from exc
        result = extract(data, evidence.detected_mime or evidence.declared_mime, ocr)
        _save(db, evidence, result)
        _finish_job(db, job_id, JobStatus.SUCCEEDED)
        db.commit()
        logger.info("evidence processed", extra=log)
        _reconcile(db, case_id)
        return "done"
    except PermanentError as exc:
        db.rollback()
        _fail(db, evidence_id, job_id, exc.code)
        logger.warning("evidence failed: %s", exc.code, extra=log)
        _reconcile(db, case_id)
        return "failed"
    except Exception as exc:
        db.rollback()
        code = exc.code if isinstance(exc, PipelineError) else "processing_error"
        if not isinstance(exc, PipelineError):
            logger.exception("unexpected processing error", extra=log)
        if attempt >= MAX_ATTEMPTS:
            _fail(db, evidence_id, job_id, code)
            _reconcile(db, case_id)
            return "failed"
        _retryable(db, evidence_id, job_id, code)
        logger.warning("evidence will be retried (attempt %d): %s", attempt, code, extra=log)
        raise RetryLater(code) from exc


def _reconcile(db: Session, case_id: str) -> None:
    """Run the rules once the case's last file is done. A failure here never
    fails the file: ``python -m app.worker reconcile`` can run it again."""
    from app.services.reconcile import reconcile_case

    try:
        reconcile_case(db, case_id)
    except Exception:
        db.rollback()
        logger.exception("reconciliation failed", extra={"case_id": case_id})


def _save(db: Session, evidence: EvidenceFile, result: Extraction) -> None:
    # The page text and blocks go to the document store (Firestore in
    # production), replacing earlier results so a rerun leaves one copy.
    get_content_store().replace(evidence.id, from_extraction(result.pages))

    parsers = sorted({f"{p.parser}:{p.parser_version}" for p in result.pages})
    evidence.parser_version = ", ".join(parsers)[:80]
    # Claims (Steps 9 and 10). A model
    # outage (TransientError) retries the whole job; a refusal keeps the text
    # and leaves the file for a manual check.
    from app.services.claims import extract_claims

    claims_error: str | None = None
    try:
        claims = extract_claims(db, evidence, result.pages)
    except PermanentError as exc:
        claims, claims_error = [], exc.code
    if not result.has_text:
        evidence.status = EvidenceStatus.NEEDS_REVIEW
        evidence.error_class = "ocr_not_configured" if result.unread_pages else "no_text_found"
    elif result.unread_pages:
        evidence.status = EvidenceStatus.NEEDS_REVIEW
        evidence.error_class = "ocr_not_configured"
    elif claims_error:
        evidence.status = EvidenceStatus.NEEDS_REVIEW
        evidence.error_class = claims_error
    else:
        evidence.status = EvidenceStatus.PROCESSED
        evidence.error_class = None
    details = {
        "title": f"Processed {evidence.original_filename}",
        "pages": len(result.pages),
        "claims": len(claims),
        "status": evidence.status,
        "unread_pages": result.unread_pages[:50],
    }
    audit.record_system(
        db,
        evidence.tenant_id,
        "evidence.processed",
        "evidence",
        evidence.id,
        case_id=evidence.case_id,
        details=details,
    )
    outbox.emit(
        db,
        evidence.tenant_id,
        "webhook",
        WebhookEvent.EVIDENCE_PROCESSED.value,
        {"case_id": evidence.case_id, "evidence_id": evidence.id, "status": evidence.status},
    )
    _notify_uploader(db, evidence)


def _finish_job(db: Session, job_id: str, status: JobStatus, error: str | None = None) -> None:
    job = db.get(ProcessingJob, job_id)
    if job is not None:
        job.status = status
        job.error_class = error
        job.lease_until = None
    db.commit()


def _fail(db: Session, evidence_id: str, job_id: str, code: str) -> None:
    evidence = db.get(EvidenceFile, evidence_id)
    if evidence is not None:
        evidence.status = EvidenceStatus.FAILED_TERMINAL
        evidence.error_class = code
        audit.record_system(
            db,
            evidence.tenant_id,
            "evidence.failed",
            "evidence",
            evidence.id,
            case_id=evidence.case_id,
            details={"title": f"Couldn't process {evidence.original_filename}", "reason": code},
        )
        outbox.emit(
            db,
            evidence.tenant_id,
            "webhook",
            WebhookEvent.EVIDENCE_FAILED.value,
            {"case_id": evidence.case_id, "evidence_id": evidence.id, "error_class": code},
        )
        _notify_uploader(db, evidence)
    _finish_job(db, job_id, JobStatus.FAILED_TERMINAL, code)


def _retryable(db: Session, evidence_id: str, job_id: str, code: str) -> None:
    evidence = db.get(EvidenceFile, evidence_id)
    if evidence is not None:
        evidence.status = EvidenceStatus.FAILED_RETRYABLE
        evidence.error_class = code
    _finish_job(db, job_id, JobStatus.FAILED_RETRYABLE, code)


def run_report_job(db: Session, storage: Storage, report_id: str) -> str:
    """Render a queued report to PDF and store it. Idempotent: ready reports are skipped."""
    from hashlib import sha256

    from app.db.models import Case, Report
    from app.enums import ReportStatus
    from app.reports.pdf import render
    from app.services import reports as report_service

    report = db.scalar(
        select(Report).where(Report.id == report_id).with_for_update(skip_locked=True)
    )
    if report is None or report.status != ReportStatus.QUEUED:
        return "skipped"
    case = db.get(Case, report.case_id)
    if case is None:
        return "skipped"
    try:
        evidence = db.scalars(
            select(EvidenceFile)
            .where(EvidenceFile.case_id == case.id, EvidenceFile.status != EvidenceStatus.UPLOADING)
            .order_by(EvidenceFile.created_at)
        ).all()
        pdf = render(
            case,
            report.options,
            list(evidence),
            report_service.export_data(db, case),
            report_service.timeline(db, case),
        )
        key = f"tenants/{case.tenant_id}/cases/{case.id}/reports/{report.id}.pdf"
        try:
            storage.write(key, pdf, "application/pdf")
        except Exception as exc:
            raise TransientError("storage_unavailable", str(exc)) from exc
    except TransientError:
        db.rollback()
        raise RetryLater(f"report {report_id}") from None
    except Exception:
        db.rollback()
        logger.exception("report failed", extra={"case_id": case.id})
        report = db.get(Report, report_id)
        if report is not None:
            report.status = ReportStatus.FAILED
            report.completed_at = datetime.now(UTC)
            db.commit()
        return "failed"
    report.object_key = key
    report.sha256 = sha256(pdf).hexdigest()
    report.status = ReportStatus.READY
    report.completed_at = datetime.now(UTC)
    audit.record_system(
        db,
        case.tenant_id,
        "report.ready",
        "report",
        report.id,
        case_id=case.id,
        details={"title": f"{report.title} is ready"},
    )
    outbox.emit(
        db,
        case.tenant_id,
        "webhook",
        WebhookEvent.REPORT_READY.value,
        {"case_id": case.id, "report_id": report.id, "format": report.format},
    )
    from app.enums import NotificationKind
    from app.services import notifications

    notifications.notify(
        db,
        case.tenant_id,
        [report.requested_by],
        NotificationKind.REPORT_READY,
        f"{report.title} is ready",
        case.title,
        case=case,
        link=f"/reports?case={case.id}",
    )
    db.commit()
    return "done"


_EVIDENCE_TITLES = {
    EvidenceStatus.PROCESSED: "{name} is processed",
    EvidenceStatus.NEEDS_REVIEW: "{name} needs a manual check",
    EvidenceStatus.FAILED_TERMINAL: "{name} couldn't be processed",
}


def _notify_uploader(db: Session, evidence: EvidenceFile) -> None:
    from app.db.models import Case
    from app.enums import NotificationKind
    from app.services import notifications

    template = _EVIDENCE_TITLES.get(EvidenceStatus(evidence.status))
    case = db.get(Case, evidence.case_id)
    if template is None or case is None:
        return
    body = f"{case.reference} · {case.title}"
    if evidence.error_class:
        body += f" ({evidence.error_class.replace('_', ' ')})"
    notifications.notify(
        db,
        evidence.tenant_id,
        [evidence.uploaded_by],
        NotificationKind.EVIDENCE_PROCESSED,
        template.format(name=evidence.original_filename),
        body,
        case=case,
    )
