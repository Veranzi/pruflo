"""Extracted page text and blocks in the document store (Firestore in
production): round trips, Firestore's 1 MiB document limit, replacement and
deletion. Uses the in-memory ``FakeFirestore`` from the storage tests."""

from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.enums import Role
from app.storage.content import (
    FirestoreContent,
    LocalContent,
    StoredBlock,
    StoredPage,
    set_content_store,
)
from tests.factories import Factory
from tests.test_firestore_storage import FakeFirestore
from tests.test_worker import _uploaded


def _page(index: int, text: str, blocks: int = 2) -> StoredPage:
    return StoredPage(
        index=index,
        label=f"Sheet {index}",
        width=Decimal("595.28"),
        height=Decimal("841.89"),
        parser="pdf_text",
        parser_version="pdfplumber 0.11",
        block_count=blocks,
        text=text,
        blocks=[
            StoredBlock(
                id=f"blk_{index}_{n}",
                index=n,
                text=f"line {n}",
                char_start=n * 7,
                char_end=n * 7 + 6,
                bbox={"x0": 0.1, "y0": 0.2, "x1": 0.3, "y1": 0.4},
                cell_ref=f"A{n + 1}",
                confidence=Decimal("0.9876"),
            )
            for n in range(blocks)
        ],
    )


@pytest.fixture(params=["firestore", "local"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> FirestoreContent | LocalContent:
    if request.param == "firestore":
        return FirestoreContent(FakeFirestore(), "pruflo_documents")
    return LocalContent(str(tmp_path / "content"))


def test_pages_round_trip(store: FirestoreContent | LocalContent) -> None:
    store.replace("ev_1", [_page(1, "first"), _page(2, "second", blocks=3)])

    summaries = store.summaries("ev_1")
    assert [(p.index, p.block_count, p.text) for p in summaries] == [(1, 2, ""), (2, 3, "")]
    assert summaries[0].width == Decimal("595.28")

    page = store.page("ev_1", 2)
    assert page is not None and page.text == "second" and page.label == "Sheet 2"
    assert [b.index for b in page.blocks] == [0, 1, 2]
    assert page.blocks[1].confidence == Decimal("0.9876")
    assert page.blocks[1].bbox == {"x0": 0.1, "y0": 0.2, "x1": 0.3, "y1": 0.4}
    assert store.page("ev_1", 9) is None
    assert store.summaries("ev_other") == []


def test_replacing_drops_old_pages_and_delete_removes_everything(
    store: FirestoreContent | LocalContent,
) -> None:
    store.replace("ev_1", [_page(1, "a"), _page(2, "b"), _page(3, "c")])
    store.replace("ev_1", [_page(1, "only")])
    assert [p.index for p in store.summaries("ev_1")] == [1]
    assert store.page("ev_1", 3) is None

    store.delete("ev_1")
    assert store.summaries("ev_1") == [] and store.page("ev_1", 1) is None


def test_large_pages_stay_under_firestores_document_limit() -> None:
    fake = FakeFirestore()
    store = FirestoreContent(fake, "pruflo_documents")
    text = "é" * 900_000  # 1.8 MB of UTF-8 on one page
    big = _page(1, text, blocks=6000)
    store.replace("ev_big", [big])  # FakeFirestore rejects any document over 1 MiB

    page = store.page("ev_big", 1)
    assert page is not None and page.text == text and len(page.blocks) == 6000

    store.delete("ev_big")
    assert not any(path.startswith("pruflo_documents/ev_big") for path in fake.docs)


def test_anchors_endpoint_reads_pages_from_firestore(client: TestClient, make: Factory) -> None:
    set_content_store(FirestoreContent(FakeFirestore(), "pruflo_documents"))
    alice = make.person(Role.INVESTIGATOR)
    case = make.case(alice.tenant, alice)
    evidence_id = _uploaded(client, alice, case.id)
    url = f"/v1/cases/{case.id}/evidence/{evidence_id}/anchors"

    body = client.get(url, headers=alice.headers).json()
    assert [p["index"] for p in body["pages"]] == [1, 2]
    detail = client.get(f"{url}?page=1", headers=alice.headers).json()["page"]
    assert "Purchase Order PO-1001" in detail["text"] and detail["blocks"]
