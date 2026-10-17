import hashlib
from typing import Any
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AuditEvent, EvidenceFile, OutboxEvent, ProcessingJob
from app.enums import CaseStatus, EvidenceStatus, Role
from app.storage import file_types
from app.storage.backend import LocalStorage
from tests import samples
from tests.factories import Factory, Person, key_headers

PDF_MIME = "application/pdf"


def _path(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.path}?{parts.query}"


def _request_upload(
    client: TestClient,
    headers: dict[str, str],
    case_id: str,
    name: str,
    data: bytes,
    content_type: str,
    **overrides: Any,
) -> Any:
    body = {
        "file_name": name,
        "size_bytes": len(data),
        "content_type": content_type,
        "evidence_types": ["documents"],
        **overrides,
    }
    return client.post(f"/v1/cases/{case_id}/evidence/upload-url", headers=headers, json=body)


def upload(
    client: TestClient,
    person: Person,
    case_id: str,
    name: str = "purchase_order.pdf",
    data: bytes | None = None,
    content_type: str = PDF_MIME,
) -> Any:
    """The whole client flow: ask for a URL, PUT the bytes, then complete."""
    data = samples.pdf(2) if data is None else data
    res = _request_upload(client, person.headers, case_id, name, data, content_type)
    assert res.status_code == 201, res.text
    slot = res.json()
    put = client.put(_path(slot["upload_url"]), content=data, headers=slot["headers"])
    assert put.status_code == 200, put.text
    return client.post(
        f"/v1/cases/{case_id}/evidence/{slot['evidence_id']}/complete", headers=person.headers
    )


def test_upload_flow_verifies_and_queues(client: TestClient, make: Factory, db: Session) -> None:
    alice = make.person(Role.INVESTIGATOR)
    case = make.case(alice.tenant, alice)
    data = samples.pdf(2)

    res = upload(client, alice, case.id, data=data)
    assert res.status_code == 200, res.text
    out = res.json()
    assert out["status"] == "queued"
    assert out["page_count"] == 2
    assert out["size_bytes"] == len(data)
    assert out["sha256"] == hashlib.sha256(data).hexdigest()  # computed on the server
    assert out["content_type"] == PDF_MIME

    evidence = db.get(EvidenceFile, out["id"])
    assert evidence is not None
    assert evidence.object_key.startswith(f"tenants/{alice.tenant.id}/cases/{case.id}/evidence/")
    job = db.scalar(select(ProcessingJob).where(ProcessingJob.evidence_id == evidence.id))
    assert job is not None and job.idempotency_key == f"{evidence.id}:process:1"
    event = db.scalar(select(OutboxEvent).where(OutboxEvent.type == "evidence.process"))
    assert event is not None and event.payload == {"evidence_id": evidence.id, "job_id": job.id}
    assert db.scalar(select(AuditEvent).where(AuditEvent.action == "evidence.uploaded"))

    # Completing twice is harmless: no second job. (The worker has already run
    # by now; this blank PDF has no text and there's no OCR, so it needs review.)
    again = client.post(
        f"/v1/cases/{case.id}/evidence/{evidence.id}/complete", headers=alice.headers
    )
    assert again.status_code == 200 and again.json()["status"] == "needs_review"
    jobs = db.scalars(select(ProcessingJob).where(ProcessingJob.evidence_id == evidence.id))
    assert len(jobs.all()) == 1

    listed = client.get(f"/v1/cases/{case.id}/evidence", headers=alice.headers).json()
    assert [e["id"] for e in listed["items"]] == [evidence.id]

    view = client.get(f"/v1/cases/{case.id}/evidence/{evidence.id}/view-url", headers=alice.headers)
    assert view.status_code == 200
    assert client.get(_path(view.json()["url"])).content == data


@pytest.mark.parametrize(
    ("name", "data", "content_type"),
    [
        ("contract.docx", samples.docx(), file_types.FILE_TYPES[".docx"].mime),
        ("stock.xlsx", samples.xlsx(), "application/octet-stream"),
        ("stock_count.csv", samples.CSV, "text/csv"),
        ("scan.png", samples.PNG, "image/png"),
    ],
)
def test_other_accepted_types(
    client: TestClient, make: Factory, name: str, data: bytes, content_type: str
) -> None:
    alice = make.person(Role.INVESTIGATOR)
    case = make.case(alice.tenant, alice)
    res = upload(client, alice, case.id, name=name, data=data, content_type=content_type)
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "queued"


def test_request_validation(client: TestClient, make: Factory) -> None:
    alice = make.person(Role.INVESTIGATOR)
    case = make.case(alice.tenant, alice)
    pdf = samples.pdf()
    for name, ctype, extra, code in (
        ("virus.exe", "application/octet-stream", {}, "unsupported_file"),
        ("old.doc", "application/msword", {}, "unsupported_file"),
        ("clip.mp4", "video/mp4", {}, "unsupported_file"),
        ("po.pdf", "image/png", {}, "unsupported_file"),
        ("po.pdf", PDF_MIME, {"size_bytes": 25 * 1024 * 1024 + 1}, "file_too_large"),
        ("po.pdf", PDF_MIME, {"evidence_types": ["videos"]}, "validation_failed"),
    ):
        res = _request_upload(client, alice.headers, case.id, name, pdf, ctype, **extra)
        assert res.status_code == 400, (name, ctype, extra)
        assert res.json()["error"]["code"] == code, (name, ctype, extra)


def test_content_that_does_not_match_is_rejected_and_kept(
    client: TestClient, make: Factory, db: Session
) -> None:
    alice = make.person(Role.INVESTIGATOR)
    case = make.case(alice.tenant, alice)
    res = upload(client, alice, case.id, name="invoice.pdf", data=b"not really a pdf")
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "unsupported_file"

    evidence = db.scalar(select(EvidenceFile).where(EvidenceFile.case_id == case.id))
    assert evidence is not None
    assert evidence.status == EvidenceStatus.FAILED_TERMINAL
    assert evidence.error_class == "unsupported_file"
    assert db.scalar(select(ProcessingJob).where(ProcessingJob.evidence_id == evidence.id)) is None

    retry = client.post(f"/v1/cases/{case.id}/evidence/{evidence.id}/retry", headers=alice.headers)
    assert retry.status_code == 409


def test_macro_files_and_long_pdfs_are_rejected(client: TestClient, make: Factory) -> None:
    alice = make.person(Role.INVESTIGATOR)
    case = make.case(alice.tenant, alice)
    docx_mime = file_types.FILE_TYPES[".docx"].mime
    macro = upload(
        client, alice, case.id, name="a.docx", data=samples.docm(), content_type=docx_mime
    )
    assert macro.json()["error"]["code"] == "unsupported_file"
    long_pdf = upload(client, alice, case.id, name="long.pdf", data=samples.pdf(201))
    assert long_pdf.json()["error"]["code"] == "too_many_pages"


def test_complete_before_upload_is_409(client: TestClient, make: Factory) -> None:
    alice = make.person(Role.INVESTIGATOR)
    case = make.case(alice.tenant, alice)
    slot = _request_upload(client, alice.headers, case.id, "po.pdf", samples.pdf(), PDF_MIME).json()
    res = client.post(
        f"/v1/cases/{case.id}/evidence/{slot['evidence_id']}/complete", headers=alice.headers
    )
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "upload_incomplete"


def test_local_upload_links_are_signed(
    client: TestClient, make: Factory, storage: LocalStorage
) -> None:
    alice = make.person(Role.INVESTIGATOR)
    case = make.case(alice.tenant, alice)
    slot = _request_upload(client, alice.headers, case.id, "po.pdf", samples.pdf(), PDF_MIME).json()
    path = _path(slot["upload_url"])

    tampered = path.replace("sig=", "sig=x")
    assert client.put(tampered, content=b"%PDF-", headers=slot["headers"]).status_code == 403
    other_key = path.replace(slot["evidence_id"], "evd_other")
    assert client.put(other_key, content=b"%PDF-", headers=slot["headers"]).status_code == 403
    wrong_type = client.put(path, content=b"%PDF-", headers={"Content-Type": "text/plain"})
    assert wrong_type.status_code == 403

    small = storage.upload_request("tenants/t/x.pdf", PDF_MIME, max_bytes=10)
    too_big = client.put(_path(small.url), content=b"x" * 11, headers=small.headers)
    assert too_big.status_code == 413


def test_who_can_upload_and_see_evidence(client: TestClient, make: Factory) -> None:
    alice = make.person(Role.INVESTIGATOR)
    tenant = alice.tenant
    viewer = make.person(Role.VIEWER, tenant)
    reviewer = make.person(Role.REVIEWER, tenant)
    outsider = make.person(Role.INVESTIGATOR, tenant)  # not a member
    case = make.case(tenant, alice, viewer, reviewer)
    upload(client, alice, case.id)

    pdf = samples.pdf()
    for person in (viewer, reviewer):
        res = _request_upload(client, person.headers, case.id, "x.pdf", pdf, PDF_MIME)
        assert res.status_code == 403
        assert (
            client.get(f"/v1/cases/{case.id}/evidence", headers=person.headers).status_code == 200
        )
    assert (
        _request_upload(client, outsider.headers, case.id, "x.pdf", pdf, PDF_MIME).status_code
        == 404
    )
    assert client.get(f"/v1/cases/{case.id}/evidence", headers=outsider.headers).status_code == 404

    _, key = make.api_key(tenant, "evidence:write")
    res = _request_upload(client, key_headers(key), case.id, "x.pdf", pdf, PDF_MIME)
    assert res.status_code == 201


def test_new_evidence_reopens_a_reviewed_case(
    client: TestClient, make: Factory, db: Session
) -> None:
    alice = make.person(Role.INVESTIGATOR)
    case = make.case(alice.tenant, alice)
    case.status = CaseStatus.COMPLETED
    db.commit()
    upload(client, alice, case.id)
    # (Set, not list: in a test both events share one transaction timestamp.)
    moves = {
        (e.details["from"], e.details["to"])
        for e in db.scalars(
            select(AuditEvent).where(
                AuditEvent.case_id == case.id, AuditEvent.action == "case.status_changed"
            )
        )
    }
    # Reopened by the upload; once processed, the rules found nothing to
    # review in this blank PDF, so it's completed again.
    assert moves == {("completed", "awaiting_evidence"), ("awaiting_evidence", "completed")}


def test_retry_failed_processing_queues_a_new_job(
    client: TestClient, make: Factory, db: Session
) -> None:
    alice = make.person(Role.INVESTIGATOR)
    case = make.case(alice.tenant, alice)
    evidence_id = upload(client, alice, case.id).json()["id"]
    evidence = db.get(EvidenceFile, evidence_id)
    assert evidence is not None

    queued = client.post(f"/v1/cases/{case.id}/evidence/{evidence_id}/retry", headers=alice.headers)
    assert queued.status_code == 409  # only failed files

    evidence.status = EvidenceStatus.FAILED_RETRYABLE
    evidence.error_class = "ocr_timeout"
    db.commit()
    res = client.post(f"/v1/cases/{case.id}/evidence/{evidence_id}/retry", headers=alice.headers)
    assert res.status_code == 200 and res.json()["status"] == "queued"
    keys = set(
        db.scalars(
            select(ProcessingJob.idempotency_key).where(ProcessingJob.evidence_id == evidence_id)
        )
    )
    assert keys == {f"{evidence_id}:process:1", f"{evidence_id}:process:2"}


def test_detect_types() -> None:
    assert file_types.detect(samples.pdf()) == PDF_MIME
    assert file_types.detect(samples.docx()) == file_types.FILE_TYPES[".docx"].mime
    assert file_types.detect(samples.xlsx()) == file_types.FILE_TYPES[".xlsx"].mime
    assert file_types.detect(samples.docm()) is None
    assert file_types.detect(samples.CSV) == "text/plain"
    assert file_types.detect(b"\x00\x01binary") is None
    assert file_types.matches(".csv", "text/plain")
    assert not file_types.matches(".pdf", "text/plain")
