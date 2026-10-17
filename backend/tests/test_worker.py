"""Step 7: the worker, the outbox and idempotent processing; endpoint 12."""

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import (
    AuditEvent,
    Case,
    DocumentPage,
    EvidenceFile,
    OutboxEvent,
    ProcessingJob,
    TextBlock,
)
from app.enums import EvidenceStatus, JobStatus, Role
from app.storage.backend import LocalStorage
from app.worker import jobs
from app.worker.jobs import RetryLater, run_processing_job
from app.worker.queue import InlinePublisher, Queue, dispatch_outbox
from tests import samples
from tests.factories import Factory, Person
from tests.test_evidence import upload

PO = samples.text_pdf([["Purchase Order PO-1001", "Qty ordered: 100"], ["Received: 80 units"]])


@pytest.fixture
def manual_queue(queue: Queue) -> Queue:
    """Dispatch only: lets a test run the job itself, step by step."""
    queue.inline = False
    return queue


def _uploaded(client: TestClient, person: Person, case_id: str, data: bytes = PO) -> str:
    res = upload(client, person, case_id, name="purchase_order.pdf", data=data)
    assert res.status_code == 200, res.text
    return str(res.json()["id"])


def _job(db: Session, evidence_id: str) -> ProcessingJob:
    job = db.scalar(select(ProcessingJob).where(ProcessingJob.evidence_id == evidence_id))
    assert job is not None
    return job


def _count(db: Session, model: Any, **where: str) -> int:
    query = select(func.count()).select_from(model)
    for name, value in where.items():
        query = query.where(getattr(model, name) == value)
    return int(db.scalar(query) or 0)


def test_upload_is_processed_and_pages_are_stored(
    client: TestClient, make: Factory, db: Session
) -> None:
    alice = make.person(Role.INVESTIGATOR)
    case = make.case(alice.tenant, alice)
    evidence_id = _uploaded(client, alice, case.id)

    evidence = db.get(EvidenceFile, evidence_id)
    assert evidence is not None
    db.refresh(evidence)
    assert evidence.status == EvidenceStatus.PROCESSED
    assert evidence.parser_version and evidence.parser_version.startswith("pdf_text:pdfplumber")
    assert _job(db, evidence_id).status == JobStatus.SUCCEEDED
    assert _count(db, DocumentPage, evidence_id=evidence_id) == 2
    assert _count(db, TextBlock, evidence_id=evidence_id) == 3

    processing = db.scalar(select(OutboxEvent).where(OutboxEvent.type == "evidence.process"))
    assert processing is not None and processing.published_at is not None
    webhook = db.scalar(select(OutboxEvent).where(OutboxEvent.type == "evidence.processed"))
    assert webhook is not None and webhook.topic == "webhook"
    assert webhook.payload == {
        "case_id": case.id,
        "evidence_id": evidence_id,
        "status": "processed",
    }
    assert _count(db, AuditEvent, action="evidence.processed", target_id=evidence_id) == 1

    listed = client.get(f"/v1/cases/{case.id}/evidence", headers=alice.headers).json()
    assert listed["items"][0]["status"] == "processed"


def test_anchors_endpoint(client: TestClient, make: Factory) -> None:
    alice = make.person(Role.VIEWER)
    investigator = make.person(Role.INVESTIGATOR, alice.tenant)
    outsider = make.person(Role.INVESTIGATOR, alice.tenant)
    case = make.case(alice.tenant, alice, investigator)
    evidence_id = _uploaded(client, investigator, case.id)
    url = f"/v1/cases/{case.id}/evidence/{evidence_id}/anchors"

    overview = client.get(url, headers=alice.headers).json()
    assert overview["page"] is None
    assert [(p["index"], p["block_count"]) for p in overview["pages"]] == [(1, 2), (2, 1)]

    detail = client.get(f"{url}?page=1", headers=alice.headers).json()["page"]
    assert detail["text"] == "Purchase Order PO-1001\nQty ordered: 100"
    block = detail["blocks"][1]
    assert detail["text"][block["char_start"] : block["char_end"]] == "Qty ordered: 100"
    assert set(block["bbox"]) == {"x0", "y0", "x1", "y1"}

    assert client.get(f"{url}?page=9", headers=alice.headers).status_code == 404
    assert client.get(url, headers=outsider.headers).status_code == 404


def test_redelivery_is_a_no_op(
    client: TestClient, make: Factory, db: Session, storage: LocalStorage, manual_queue: Queue
) -> None:
    alice = make.person(Role.INVESTIGATOR)
    case = make.case(alice.tenant, alice)
    evidence_id = _uploaded(client, alice, case.id)
    job = _job(db, evidence_id)

    assert run_processing_job(db, storage, None, evidence_id, job.id) == "done"
    pages, audits = _count(db, TextBlock, evidence_id=evidence_id), _count(db, AuditEvent)
    # Pub/Sub delivers at least once: the same message again changes nothing.
    assert run_processing_job(db, storage, None, evidence_id, job.id) == "skipped"
    assert _count(db, TextBlock, evidence_id=evidence_id) == pages
    assert _count(db, AuditEvent) == audits


def test_job_held_by_another_worker_is_retried_later(
    client: TestClient, make: Factory, db: Session, storage: LocalStorage, manual_queue: Queue
) -> None:
    alice = make.person(Role.INVESTIGATOR)
    case = make.case(alice.tenant, alice)
    evidence_id = _uploaded(client, alice, case.id)
    job = _job(db, evidence_id)
    job.status = JobStatus.RUNNING
    job.lease_until = datetime.now(UTC) + timedelta(minutes=5)
    db.commit()
    with pytest.raises(RetryLater):
        run_processing_job(db, storage, None, evidence_id, job.id)

    # A crashed worker's lease runs out, and the job can be taken over.
    job.lease_until = datetime.now(UTC) - timedelta(seconds=1)
    db.commit()
    assert run_processing_job(db, storage, None, evidence_id, job.id) == "done"
    assert _count(db, DocumentPage, evidence_id=evidence_id) == 2


class FlakyStorage:
    def __init__(self, real: LocalStorage) -> None:
        self.real = real

    def read(self, key: str) -> bytes:
        raise ConnectionError("storage down")

    def __getattr__(self, name: str) -> Any:
        return getattr(self.real, name)


def test_transient_errors_retry_then_fail_for_good(
    client: TestClient, make: Factory, db: Session, storage: LocalStorage, manual_queue: Queue
) -> None:
    alice = make.person(Role.INVESTIGATOR)
    case = make.case(alice.tenant, alice)
    evidence_id = _uploaded(client, alice, case.id)
    job = _job(db, evidence_id)
    flaky = FlakyStorage(storage)

    for attempt in range(1, jobs.MAX_ATTEMPTS):
        with pytest.raises(RetryLater):
            run_processing_job(db, flaky, None, evidence_id, job.id)  # type: ignore[arg-type]
        db.refresh(job)
        assert job.attempts == attempt and job.status == JobStatus.FAILED_RETRYABLE
    evidence = db.get(EvidenceFile, evidence_id)
    assert evidence is not None and evidence.status == EvidenceStatus.FAILED_RETRYABLE

    assert run_processing_job(db, flaky, None, evidence_id, job.id) == "failed"  # type: ignore[arg-type]
    db.refresh(evidence)
    assert evidence.status == EvidenceStatus.FAILED_TERMINAL
    assert evidence.error_class == "storage_unavailable"
    failed = db.scalar(select(OutboxEvent).where(OutboxEvent.type == "evidence.failed"))
    assert failed is not None and failed.payload["error_class"] == "storage_unavailable"

    # It can be retried by hand once the problem is fixed.
    res = client.post(f"/v1/cases/{case.id}/evidence/{evidence_id}/retry", headers=alice.headers)
    assert res.status_code == 200


def test_unreadable_file_fails_at_once(client: TestClient, make: Factory, db: Session) -> None:
    alice = make.person(Role.INVESTIGATOR)
    case = make.case(alice.tenant, alice)
    # Passes the upload check (a zip with word/document.xml) but isn't a real Word file.
    res = upload(
        client,
        alice,
        case.id,
        name="contract.docx",
        data=samples.docx(),
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    evidence = db.get(EvidenceFile, res.json()["id"])
    assert evidence is not None
    db.refresh(evidence)
    assert evidence.status == EvidenceStatus.FAILED_TERMINAL
    assert evidence.error_class == "unreadable_file"
    assert _job(db, evidence.id).attempts == 1


def test_scans_without_ocr_need_review(client: TestClient, make: Factory, db: Session) -> None:
    alice = make.person(Role.INVESTIGATOR)
    case = make.case(alice.tenant, alice)
    res = upload(
        client, alice, case.id, name="scan.png", data=samples.PNG, content_type="image/png"
    )
    evidence = db.get(EvidenceFile, res.json()["id"])
    assert evidence is not None
    db.refresh(evidence)
    assert evidence.status == EvidenceStatus.NEEDS_REVIEW
    assert evidence.error_class == "ocr_not_configured"


def test_failed_publish_stays_in_the_outbox(make: Factory, db: Session) -> None:
    from app.services import outbox

    tenant = make.tenant()
    outbox.emit(
        db, tenant.id, "processing", "evidence.process", {"evidence_id": "e", "job_id": "j"}
    )
    db.commit()

    class Down:
        def publish(self, event: OutboxEvent) -> None:
            raise ConnectionError("pubsub down")

    assert dispatch_outbox(db, Down()) == []
    event = db.scalar(select(OutboxEvent).where(OutboxEvent.tenant_id == tenant.id))
    assert event is not None and event.published_at is None and event.attempts == 1
    assert len(dispatch_outbox(db, InlinePublisher())) == 1  # picked up next time


# The worker service ---------------------------------------------------------------


@pytest.fixture
def worker(db: Session, queue: Queue) -> TestClient:
    from app.db.session import get_db
    from app.worker.main import create_worker_app
    from app.worker.queue import get_queue

    app = create_worker_app()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_queue] = lambda: queue
    return TestClient(app, raise_server_exceptions=False)


def _push(payload: dict[str, str] | str, event_type: str = "evidence.process") -> dict[str, Any]:
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    return {
        "message": {
            "data": base64.b64encode(raw.encode()).decode(),
            "attributes": {"type": event_type},
            "messageId": "m1",
        },
        "subscription": "projects/p/subscriptions/evidence-jobs-push",
    }


def test_push_endpoint_acks_and_nacks(
    client: TestClient, worker: TestClient, make: Factory, db: Session, manual_queue: Queue
) -> None:
    alice = make.person(Role.INVESTIGATOR)
    case = make.case(alice.tenant, alice)
    evidence_id = _uploaded(client, alice, case.id)
    job = _job(db, evidence_id)
    message = _push({"evidence_id": evidence_id, "job_id": job.id})

    assert worker.post("/pubsub/push", json=message).status_code == 204
    assert _job(db, evidence_id).status == JobStatus.SUCCEEDED
    assert worker.post("/pubsub/push", json=message).status_code == 204  # redelivery

    job.status = JobStatus.RUNNING
    job.lease_until = datetime.now(UTC) + timedelta(minutes=5)
    db.commit()
    assert worker.post("/pubsub/push", json=message).status_code == 503  # nack: retry later

    assert worker.post("/pubsub/push", json=_push("{not json")).status_code == 204
    assert worker.post("/pubsub/push", json=_push({"evidence_id": "x"})).status_code == 204
    assert worker.post("/pubsub/push", json=_push({}, "something.else")).status_code == 204


def test_worker_refuses_unauthenticated_calls_in_the_cloud(worker: TestClient) -> None:
    settings = get_settings()
    original = settings.env
    settings.env = "prod"
    try:
        # Not configured: refuse everything (fail closed).
        assert worker.post("/pubsub/push", json=_push({})).status_code == 403
        settings.worker_push_audience = "https://worker.example.run.app"
        settings.worker_invoker_service_accounts = ["pubsub@p.iam.gserviceaccount.com"]
        assert worker.post("/pubsub/push", json=_push({})).status_code == 401
        res = worker.post(
            "/tasks/dispatch-outbox", headers={"Authorization": "Bearer not-a-real-token"}
        )
        assert res.status_code == 401
    finally:
        settings.env = original
        settings.worker_push_audience = ""
        settings.worker_invoker_service_accounts = []


def test_drain_processes_waiting_files(
    client: TestClient,
    make: Factory,
    db: Session,
    storage: LocalStorage,
    manual_queue: Queue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.worker import __main__ as worker_cli

    monkeypatch.setattr(worker_cli, "get_storage", lambda: storage)
    monkeypatch.setattr(worker_cli, "get_ocr_provider", lambda: None)
    alice = make.person(Role.INVESTIGATOR)
    case = make.case(alice.tenant, alice)
    first, second = _uploaded(client, alice, case.id), _uploaded(client, alice, case.id)
    assert worker_cli.drain(db) == {"done": 2}
    for evidence_id in (first, second):
        evidence = db.get(EvidenceFile, evidence_id)
        assert evidence is not None and evidence.status == EvidenceStatus.PROCESSED
    assert worker_cli.drain(db) == {}


def test_retention_is_off_until_a_period_is_set(
    make: Factory, db: Session, storage: LocalStorage
) -> None:
    from app.services import retention

    assert retention.run(db, storage) == {"reports": 0, "evidence": 0}


def test_retention_deletes_old_files_but_keeps_the_record(
    make: Factory, db: Session, storage: LocalStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime, timedelta

    from app.db.models import AuditEvent, DocumentPage, Report
    from app.enums import CaseStatus
    from app.ids import new_id
    from app.services import retention

    settings = get_settings()
    monkeypatch.setattr(settings, "evidence_retention_days", 30)
    monkeypatch.setattr(settings, "report_retention_days", 30)
    person = make.person()
    old_case = make.case(person.tenant, person)
    new_case = make.case(person.tenant, person)
    old_case.status = new_case.status = CaseStatus.COMPLETED
    old = make.evidence(old_case)
    fresh = make.evidence(new_case)
    for evidence in (old, fresh):
        storage.write(evidence.object_key, b"%PDF-1.4", "application/pdf")
        db.add(
            DocumentPage(
                id=new_id("pg"),
                evidence_id=evidence.id,
                page_index=1,
                text="x",
                parser="pdf_text",
                parser_version="1",
            )
        )
    report = Report(
        id=new_id("rep"),
        tenant_id=person.tenant.id,
        case_id=old_case.id,
        format="pdf",
        status="ready",
        template_version="1",
        title="Summary",
        object_key="tenants/x/report.pdf",
    )
    db.add(report)
    db.commit()
    storage.write("tenants/x/report.pdf", b"%PDF", "application/pdf")
    later = datetime.now(UTC) + timedelta(days=31)
    db.execute(Case.__table__.update().where(Case.id == new_case.id).values(updated_at=later))
    db.commit()

    assert retention.purge_evidence(db, storage, now=later) == 1
    assert retention.purge_reports(db, storage, now=later) == 1
    db.refresh(old)
    db.refresh(fresh)
    assert old.purged_at is not None and storage.stat(old.object_key) is None
    assert db.scalar(select(DocumentPage).where(DocumentPage.evidence_id == old.id)) is None
    assert fresh.purged_at is None and storage.stat(fresh.object_key) is not None
    assert db.get(Report, report.id) is None and storage.stat("tenants/x/report.pdf") is None
    assert db.scalar(
        select(AuditEvent).where(
            AuditEvent.action == "evidence.purged", AuditEvent.target_id == old.id
        )
    )


def test_tick_runs_scheduled_work_with_its_token(
    client: TestClient, make: Factory, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = get_settings()
    assert client.post("/v1/internal/tick").status_code == 404  # off without a token
    monkeypatch.setattr(settings, "tick_token", "tick-secret-123")
    assert client.post("/v1/internal/tick").status_code == 401
    bad = client.post("/v1/internal/tick", headers={"X-Pruflo-Tick-Token": "wrong"})
    assert bad.status_code == 401
    alice = make.person(Role.INVESTIGATOR)
    case = make.case(alice.tenant, alice)
    evidence = make.evidence(case)
    evidence.status = EvidenceStatus.FAILED_RETRYABLE
    db.commit()
    res = client.post("/v1/internal/tick", headers={"X-Pruflo-Tick-Token": "tick-secret-123"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert {"processing", "connectors", "retention", "notifications_removed"} <= set(body)
    assert "failed" not in body.values()
