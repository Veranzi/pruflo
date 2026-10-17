"""Extracted document content: each page's text and its text blocks.

This is the unstructured part of an evidence file, so it lives in a document
store rather than in Postgres. Postgres keeps the structured records (cases,
evidence metadata, claims, findings, review actions, audit) that reports and
the review trail join on.

- ``FirestoreContent``: Cloud Firestore. One document per evidence file holds
  the page summaries; each page is a document in its ``pages`` subcollection,
  with its blocks split over a ``blocks`` subcollection so no document passes
  Firestore's 1 MiB limit. The evidence document is written last, so a
  half-written replacement is never visible.
- ``LocalContent``: one JSON file per evidence file (local and test only).

Content is replaced as a whole on every processing run, which keeps worker
retries idempotent.
"""

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

from app.config import get_settings
from app.ids import new_id
from app.pipeline.document import Page


@dataclass
class StoredBlock:
    id: str
    index: int
    text: str
    char_start: int
    char_end: int
    bbox: dict[str, float] | None = None
    cell_ref: str | None = None
    confidence: Decimal | None = None


@dataclass
class StoredPage:
    index: int
    label: str
    width: Decimal | None
    height: Decimal | None
    parser: str
    parser_version: str
    block_count: int
    text: str = ""
    blocks: list[StoredBlock] = field(default_factory=list)


def from_extraction(pages: list[Page]) -> list[StoredPage]:
    return [
        StoredPage(
            index=page.index,
            label=page.label,
            width=page.width,
            height=page.height,
            parser=page.parser,
            parser_version=page.parser_version,
            block_count=len(page.blocks),
            text=page.text,
            blocks=[
                StoredBlock(
                    id=new_id("blk"),
                    index=number,
                    text=block.text,
                    char_start=block.char_start,
                    char_end=block.char_end,
                    bbox=block.bbox.as_dict() if block.bbox else None,
                    cell_ref=block.cell_ref,
                    confidence=block.confidence,
                )
                for number, block in enumerate(page.blocks)
            ],
        )
        for page in pages
    ]


class ContentStore(Protocol):
    def replace(self, evidence_id: str, pages: list[StoredPage]) -> None: ...
    def summaries(self, evidence_id: str) -> list[StoredPage]: ...  # no text or blocks
    def page(self, evidence_id: str, index: int) -> StoredPage | None: ...
    def delete(self, evidence_id: str) -> None: ...


# Decimals don't survive Firestore or JSON as numbers without losing digits.
def _dec(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _undec(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _page_meta(page: StoredPage) -> dict[str, Any]:
    return {
        "index": page.index,
        "label": page.label,
        "width": _dec(page.width),
        "height": _dec(page.height),
        "parser": page.parser,
        "parser_version": page.parser_version,
        "block_count": page.block_count,
    }


def _block_doc(block: StoredBlock) -> dict[str, Any]:
    data = asdict(block)
    data["confidence"] = _dec(block.confidence)
    return data


def _page_from(meta: dict[str, Any]) -> StoredPage:
    return StoredPage(
        index=int(meta["index"]),
        label=meta.get("label", ""),
        width=_undec(meta.get("width")),
        height=_undec(meta.get("height")),
        parser=meta.get("parser", ""),
        parser_version=meta.get("parser_version", ""),
        block_count=int(meta.get("block_count", 0)),
    )


def _block_from(data: dict[str, Any]) -> StoredBlock:
    return StoredBlock(
        id=data["id"],
        index=int(data["index"]),
        text=data["text"],
        char_start=int(data["char_start"]),
        char_end=int(data["char_end"]),
        bbox=data.get("bbox"),
        cell_ref=data.get("cell_ref"),
        confidence=_undec(data.get("confidence")),
    )


class LocalContent:
    def __init__(self, root: str) -> None:
        self.root = Path(root)

    def _path(self, evidence_id: str) -> Path:
        if not evidence_id.replace("_", "").isalnum():
            raise ValueError(f"Unsafe evidence ID: {evidence_id!r}")
        return self.root / f"{evidence_id}.json"

    def replace(self, evidence_id: str, pages: list[StoredPage]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = [
            {**_page_meta(p), "text": p.text, "blocks": [_block_doc(b) for b in p.blocks]}
            for p in pages
        ]
        handle, temp = tempfile.mkstemp(dir=self.root, suffix=".tmp")
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump(payload, out)
        os.replace(temp, self._path(evidence_id))

    def _load(self, evidence_id: str) -> list[dict[str, Any]]:
        path = self._path(evidence_id)
        if not path.is_file():
            return []
        loaded: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))
        return loaded

    def summaries(self, evidence_id: str) -> list[StoredPage]:
        return [_page_from(p) for p in self._load(evidence_id)]

    def page(self, evidence_id: str, index: int) -> StoredPage | None:
        for data in self._load(evidence_id):
            if int(data["index"]) == index:
                page = _page_from(data)
                page.text = data.get("text", "")
                page.blocks = [_block_from(b) for b in data.get("blocks", [])]
                return page
        return None

    def delete(self, evidence_id: str) -> None:
        self._path(evidence_id).unlink(missing_ok=True)


class FirestoreContent:
    """Page text and blocks in Firestore; see the module docstring for the layout."""

    BUDGET = 700 * 1024  # bytes per document, well under Firestore's 1 MiB limit
    PER_BATCH = 400  # Firestore allows 500 writes per batch

    def __init__(self, client: Any, collection: str) -> None:
        self._client = client
        self._docs = client.collection(collection)

    def _evidence(self, evidence_id: str) -> Any:
        return self._docs.document(evidence_id)

    def replace(self, evidence_id: str, pages: list[StoredPage]) -> None:
        doc = self._evidence(evidence_id)
        doc.delete()  # readers stop seeing the old content first
        self._delete_pages(doc)
        writes: list[tuple[Any, dict[str, Any]]] = []
        for page in pages:
            page_doc = doc.collection("pages").document(f"{page.index:05d}")
            meta = _page_meta(page)
            parts = _split_text(page.text, self.BUDGET)
            if len(parts) == 1:
                meta["text"] = parts[0]
            else:
                meta["text_parts"] = len(parts)
                writes += [
                    (page_doc.collection("text").document(f"{n:05d}"), {"text": part})
                    for n, part in enumerate(parts)
                ]
            groups = _group_blocks([_block_doc(b) for b in page.blocks], self.BUDGET)
            meta["block_groups"] = len(groups)
            writes += [
                (page_doc.collection("blocks").document(f"{n:05d}"), {"blocks": group})
                for n, group in enumerate(groups)
            ]
            writes.append((page_doc, meta))
        self._commit(writes)
        doc.set(
            {
                "evidence_id": evidence_id,
                "page_count": len(pages),
                "pages": [_page_meta(p) for p in pages],
                "updated_at": datetime.now(UTC),
            }
        )

    def summaries(self, evidence_id: str) -> list[StoredPage]:
        snapshot = self._evidence(evidence_id).get()
        if not snapshot.exists:
            return []
        return [_page_from(p) for p in (snapshot.to_dict() or {}).get("pages", [])]

    def page(self, evidence_id: str, index: int) -> StoredPage | None:
        doc = self._evidence(evidence_id)
        if not doc.get().exists:
            return None
        page_doc = doc.collection("pages").document(f"{index:05d}")
        snapshot = page_doc.get()
        if not snapshot.exists:
            return None
        meta = snapshot.to_dict() or {}
        page = _page_from(meta)
        if "text_parts" in meta:
            page.text = "".join(
                (page_doc.collection("text").document(f"{n:05d}").get().to_dict() or {})["text"]
                for n in range(int(meta["text_parts"]))
            )
        else:
            page.text = meta.get("text", "")
        for n in range(int(meta.get("block_groups", 0))):
            group = page_doc.collection("blocks").document(f"{n:05d}").get().to_dict() or {}
            page.blocks += [_block_from(b) for b in group.get("blocks", [])]
        return page

    def delete(self, evidence_id: str) -> None:
        doc = self._evidence(evidence_id)
        doc.delete()
        self._delete_pages(doc)

    def _delete_pages(self, doc: Any) -> None:
        refs: list[Any] = []
        for page in doc.collection("pages").stream():
            for sub in ("blocks", "text"):
                refs += [snap.reference for snap in page.reference.collection(sub).stream()]
            refs.append(page.reference)
        for start in range(0, len(refs), self.PER_BATCH):
            batch = self._client.batch()
            for ref in refs[start : start + self.PER_BATCH]:
                batch.delete(ref)
            batch.commit()

    def _commit(self, writes: list[tuple[Any, dict[str, Any]]]) -> None:
        # Keep each commit small: Firestore also caps a request at about 10 MiB.
        batch, size, count = self._client.batch(), 0, 0
        for ref, data in writes:
            weight = _weight(data)
            if count and (count >= self.PER_BATCH or size + weight > 6 * self.BUDGET):
                batch.commit()
                batch, size, count = self._client.batch(), 0, 0
            batch.set(ref, data)
            size, count = size + weight, count + 1
        if count:
            batch.commit()


def _weight(data: Any) -> int:
    return len(json.dumps(data, default=str).encode())


def _split_text(text: str, budget: int) -> list[str]:
    """Split ``text`` so each part is at most ``budget`` bytes of UTF-8."""
    if len(text.encode()) <= budget:
        return [text]
    parts: list[str] = []
    current: list[str] = []
    size = 0
    for char in text:
        width = len(char.encode())
        if size + width > budget:
            parts.append("".join(current))
            current, size = [], 0
        current.append(char)
        size += width
    parts.append("".join(current))
    return parts


def _group_blocks(blocks: list[dict[str, Any]], budget: int) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    size = 0
    for block in blocks:
        weight = _weight(block)
        if current and size + weight > budget:
            groups.append(current)
            current, size = [], 0
        current.append(block)
        size += weight
    if current:
        groups.append(current)
    return groups


_override: ContentStore | None = None


def set_content_store(store: ContentStore | None) -> None:
    """Tests swap the store in; ``None`` goes back to the configured one."""
    global _override
    _override = store


@lru_cache
def _default_content() -> ContentStore:
    settings = get_settings()
    if settings.content_backend == "firestore":
        from app.storage.backend import firestore_client

        return FirestoreContent(firestore_client(), settings.firestore_content_collection)
    if settings.env not in ("local", "test"):
        raise RuntimeError("Local content is only allowed when PRUFLO_ENV is local or test.")
    return LocalContent(str(Path(settings.local_storage_dir) / "content"))


def get_content_store() -> ContentStore:
    return _override or _default_content()
