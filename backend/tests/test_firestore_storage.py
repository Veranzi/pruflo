"""Files in Firestore (the free Firebase plan): chunked writes, signed links,
and the whole upload path through the API.

``FakeFirestore`` keeps documents in memory with the same calls the real
client offers (collection/document/get/set/delete/stream/batch), and
enforces Firestore's 1 MiB document limit so oversized chunks would fail
here as they would in production.
"""

import json
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.enums import Role
from app.storage.backend import FirestoreStorage
from tests import samples
from tests.factories import Factory
from tests.test_evidence import upload

MIB = 1024 * 1024


class _Snapshot:
    def __init__(self, ref: "_Doc", data: dict[str, Any] | None) -> None:
        self.reference = ref
        self.exists = data is not None
        self._data = data

    def to_dict(self) -> dict[str, Any] | None:
        return dict(self._data) if self._data is not None else None


class _Doc:
    def __init__(self, store: "FakeFirestore", path: str) -> None:
        self.store, self.path = store, path

    def collection(self, name: str) -> "_Collection":
        return _Collection(self.store, f"{self.path}/{name}")

    def get(self) -> _Snapshot:
        self.store.reads += 1
        return _Snapshot(self, self.store.docs.get(self.path))

    def set(self, data: dict[str, Any]) -> None:
        size = sum(
            len(v)
            if isinstance(v, bytes)
            else len(json.dumps(v, default=str, ensure_ascii=False).encode())
            for v in data.values()
        )
        if size > MIB:
            raise ValueError("Firestore documents are limited to 1 MiB")
        self.store.writes += 1
        self.store.docs[self.path] = dict(data)

    def delete(self) -> None:
        self.store.docs.pop(self.path, None)


class _Collection:
    def __init__(self, store: "FakeFirestore", path: str) -> None:
        self.store, self.path = store, path

    def document(self, doc_id: str) -> _Doc:
        return _Doc(self.store, f"{self.path}/{doc_id}")

    def stream(self) -> Iterator[_Snapshot]:
        prefix = self.path + "/"
        for path in sorted(self.store.docs):
            if path.startswith(prefix) and "/" not in path[len(prefix) :]:
                yield _Snapshot(_Doc(self.store, path), self.store.docs[path])


class _Batch:
    def __init__(self, store: "FakeFirestore") -> None:
        self.ops: list[tuple[str, _Doc, dict[str, Any] | None]] = []
        self.store = store

    def set(self, ref: _Doc, data: dict[str, Any]) -> None:
        self.ops.append(("set", ref, data))

    def delete(self, ref: _Doc) -> None:
        self.ops.append(("delete", ref, None))

    def commit(self) -> None:
        assert len(self.ops) <= 500  # Firestore's batch limit
        for op, ref, data in self.ops:
            if op == "set":
                assert data is not None
                ref.set(data)
            else:
                ref.delete()


class FakeFirestore:
    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {}
        self.reads = self.writes = 0

    def collection(self, name: str) -> _Collection:
        return _Collection(self, name)

    def batch(self) -> _Batch:
        return _Batch(self)


@pytest.fixture
def firestore() -> FakeFirestore:
    return FakeFirestore()


@pytest.fixture
def fstorage(firestore: FakeFirestore) -> FirestoreStorage:
    return FirestoreStorage(firestore, "pruflo_objects", "test-signing-key", "http://testserver")


def test_large_files_are_split_under_the_document_limit(
    fstorage: FirestoreStorage, firestore: FakeFirestore
) -> None:
    data = bytes(range(256)) * (3 * MIB // 256 + 7)  # just over 3 MiB
    fstorage.write("tenants/t/a.pdf", data, "application/pdf")
    chunks = [p for p in firestore.docs if "/chunks/" in p]
    assert len(chunks) == -(-len(data) // FirestoreStorage.CHUNK)
    info = fstorage.stat("tenants/t/a.pdf")
    assert info is not None and info.size == len(data) and info.content_type == "application/pdf"
    assert fstorage.read("tenants/t/a.pdf") == data
    # Rewriting with a smaller file leaves no stale pieces behind.
    fstorage.write("tenants/t/a.pdf", b"small", "application/pdf")
    assert len([p for p in firestore.docs if "/chunks/" in p]) == 1
    assert fstorage.read("tenants/t/a.pdf") == b"small"
    fstorage.delete("tenants/t/a.pdf")
    assert fstorage.stat("tenants/t/a.pdf") is None and firestore.docs == {}


def test_a_damaged_file_is_never_returned(
    fstorage: FirestoreStorage, firestore: FakeFirestore
) -> None:
    fstorage.write("k.pdf", b"x" * (FirestoreStorage.CHUNK + 10), "application/pdf")
    piece = next(p for p in firestore.docs if p.endswith("/chunks/00001"))
    firestore.docs[piece] = {"data": b"tampered!!"}
    with pytest.raises(OSError):
        fstorage.read("k.pdf")


def test_upload_and_view_through_the_api(
    client: TestClient,
    make: Factory,
    db: Session,
    fstorage: FirestoreStorage,
    firestore: FakeFirestore,
) -> None:
    from app.storage.backend import get_storage

    client.app.dependency_overrides[get_storage] = lambda: fstorage  # type: ignore[attr-defined]
    alice = make.person(Role.INVESTIGATOR)
    case = make.case(alice.tenant, alice)
    data = samples.pdf(2)
    res = upload(client, alice, case.id, data=data)
    assert res.status_code == 200, res.text
    assert res.json()["status"] in ("queued", "processed", "needs_review")
    assert any("/chunks/" in p for p in firestore.docs)
    view = client.get(
        f"/v1/cases/{case.id}/evidence/{res.json()['id']}/view-url", headers=alice.headers
    ).json()
    url = urlsplit(view["url"])
    assert url.path.startswith("/v1/objects/")
    got = client.get(f"{url.path}?{url.query}")
    assert got.status_code == 200 and got.content == data
    assert got.headers["content-type"] == "application/pdf"
    assert got.headers["cache-control"] == "private, no-store"
    # The link is bound to its key and signature.
    tampered = client.get(f"{url.path}?{url.query.replace('sig=', 'sig=x')}")
    assert tampered.status_code == 403


def test_settings_pick_firestore_and_require_a_signing_key(
    monkeypatch: pytest.MonkeyPatch, firestore: FakeFirestore
) -> None:
    from app.config import get_settings
    from app.storage import backend

    settings = get_settings()
    monkeypatch.setattr(settings, "storage_backend", "firestore")
    monkeypatch.setattr(settings, "env", "prod")
    monkeypatch.setattr(backend, "firestore_client", lambda: firestore)
    backend._default_storage.cache_clear()
    try:
        monkeypatch.setattr(settings, "storage_signing_key", "")
        with pytest.raises(RuntimeError, match="PRUFLO_STORAGE_SIGNING_KEY"):
            backend._default_storage()
        monkeypatch.setattr(settings, "storage_signing_key", "k" * 32)
        assert isinstance(backend._default_storage(), FirestoreStorage)
    finally:
        backend._default_storage.cache_clear()
