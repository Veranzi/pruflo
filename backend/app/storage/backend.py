"""Object storage behind one small interface.

- ``FirestoreStorage``: files in Cloud Firestore (Firebase, free Spark plan),
  split into chunks under one document per file. Upload and view links point
  at this API and are HMAC-signed, expiring and size-limited.
- ``LocalStorage``: files on disk with the same signed links (local and test).
- ``GcsStorage``: a private Cloud Storage bucket with V4 signed URLs, for a
  deployment with Google Cloud billing.

Signed URLs are never logged.
"""

import base64
import contextlib
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

from app.config import get_settings


@dataclass(frozen=True)
class SignedRequest:
    url: str
    method: str
    expires_at: datetime
    headers: dict[str, str] = field(default_factory=dict)  # the client must send these


@dataclass(frozen=True)
class ObjectInfo:
    size: int
    content_type: str | None


class Storage(Protocol):
    def upload_request(self, key: str, content_type: str, max_bytes: int) -> SignedRequest: ...
    def view_url(
        self, key: str, file_name: str, content_type: str, inline: bool = True
    ) -> SignedRequest: ...
    def stat(self, key: str) -> ObjectInfo | None: ...
    def read(self, key: str) -> bytes: ...
    def write(self, key: str, data: bytes, content_type: str) -> None: ...
    def delete(self, key: str) -> None: ...


def _expiry() -> tuple[timedelta, datetime]:
    ttl = timedelta(minutes=get_settings().signed_url_minutes)
    return ttl, datetime.now(UTC) + ttl


def attachment_extra(file_name: str) -> str:
    return f"attachment\n{file_name}"


def _disposition(file_name: str, inline: bool = True) -> str:
    kind = "inline" if inline else "attachment"
    return f"{kind}; filename*=UTF-8''{quote(file_name)}"


class GcsStorage:
    def __init__(self, bucket: str) -> None:
        from google.cloud.storage import Client

        self._client = Client(project=get_settings().gcp_project)
        self._bucket = self._client.bucket(bucket)

    def _signing_kwargs(self) -> dict[str, object]:
        # On Cloud Run there's no key file: sign through the IAM signBlob API
        # using the service account's own access token.
        import google.auth
        import google.auth.transport.requests

        credentials, _ = google.auth.default()
        credentials.refresh(google.auth.transport.requests.Request())  # type: ignore[no-untyped-call]
        email = get_settings().signing_service_account or getattr(
            credentials, "service_account_email", None
        )
        if email:
            return {"service_account_email": email, "access_token": credentials.token}
        return {"credentials": credentials}

    def upload_request(self, key: str, content_type: str, max_bytes: int) -> SignedRequest:
        ttl, expires_at = _expiry()
        headers = {"Content-Type": content_type, "x-goog-content-length-range": f"0,{max_bytes}"}
        url = self._bucket.blob(key).generate_signed_url(
            version="v4", expiration=ttl, method="PUT", headers=headers, **self._signing_kwargs()
        )
        return SignedRequest(url=url, method="PUT", expires_at=expires_at, headers=headers)

    def view_url(
        self, key: str, file_name: str, content_type: str, inline: bool = True
    ) -> SignedRequest:
        ttl, expires_at = _expiry()
        url = self._bucket.blob(key).generate_signed_url(
            version="v4",
            expiration=ttl,
            method="GET",
            response_disposition=_disposition(file_name, inline),
            response_type=content_type,
            **self._signing_kwargs(),
        )
        return SignedRequest(url=url, method="GET", expires_at=expires_at)

    def stat(self, key: str) -> ObjectInfo | None:
        blob = self._bucket.get_blob(key)
        if blob is None:
            return None
        return ObjectInfo(size=int(blob.size or 0), content_type=blob.content_type)

    def read(self, key: str) -> bytes:
        return bytes(self._bucket.blob(key).download_as_bytes())

    def write(self, key: str, data: bytes, content_type: str) -> None:
        self._bucket.blob(key).upload_from_string(data, content_type=content_type)

    def delete(self, key: str) -> None:
        from google.api_core.exceptions import NotFound

        with contextlib.suppress(NotFound):
            self._bucket.blob(key).delete()


class SignedLinks:
    """Links to this API's /v1/objects route, HMAC-signed over method, key,
    expiry and (for uploads) the content type and size limit."""

    def __init__(self, signing_key: str, base_url: str) -> None:
        self._key = signing_key.encode()
        self._base = base_url.rstrip("/")

    def sign(self, method: str, key: str, expires: int, extra: str = "") -> str:
        message = f"{method}\n{key}\n{expires}\n{extra}".encode()
        digest = hmac.new(self._key, message, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).decode().rstrip("=")

    def verify(self, method: str, key: str, expires: int, signature: str, extra: str = "") -> bool:
        if expires < time.time():
            return False
        return hmac.compare_digest(self.sign(method, key, expires, extra), signature)

    def _url(self, method: str, key: str, extra: str = "") -> tuple[str, datetime]:
        _, expires_at = _expiry()
        expires = int(expires_at.timestamp())
        sig = self.sign(method, key, expires, extra)
        return (
            f"{self._base}/v1/objects/{quote(key)}?expires={expires}&sig={sig}",
            expires_at,
        )

    def upload_request(self, key: str, content_type: str, max_bytes: int) -> SignedRequest:
        url, expires_at = self._url("PUT", key, f"{content_type}\n{max_bytes}")
        url += f"&max={max_bytes}&ct={quote(content_type)}"
        return SignedRequest(url, "PUT", expires_at, headers={"Content-Type": content_type})

    def view_url(
        self, key: str, file_name: str, content_type: str, inline: bool = True
    ) -> SignedRequest:
        if inline:
            url, expires_at = self._url("GET", key)
        else:
            # The download name is signed too, so a link can't be re-labelled.
            url, expires_at = self._url("GET", key, attachment_extra(file_name))
            url += f"&dl={quote(file_name)}"
        return SignedRequest(url, "GET", expires_at)

    # Implemented by each store.
    def stat(self, key: str) -> ObjectInfo | None:
        raise NotImplementedError

    def read(self, key: str) -> bytes:
        raise NotImplementedError

    def write(self, key: str, data: bytes, content_type: str) -> None:
        raise NotImplementedError


class LocalStorage(SignedLinks):
    def __init__(self, root: str, signing_key: str, base_url: str) -> None:
        super().__init__(signing_key, base_url)
        self.root = Path(root).resolve()

    def path(self, key: str) -> Path:
        target = (self.root / key).resolve()
        if not target.is_relative_to(self.root):
            raise ValueError("object key escapes the storage root")
        return target

    def stat(self, key: str) -> ObjectInfo | None:
        target = self.path(key)
        if not target.is_file():
            return None
        meta = target.with_name(target.name + ".type")
        content_type = meta.read_text() if meta.is_file() else None
        return ObjectInfo(size=target.stat().st_size, content_type=content_type)

    def read(self, key: str) -> bytes:
        return self.path(key).read_bytes()

    def write(self, key: str, data: bytes, content_type: str) -> None:
        target = self.path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.with_name(target.name + ".type").write_text(content_type)

    def delete(self, key: str) -> None:
        target = self.path(key)
        target.unlink(missing_ok=True)
        target.with_name(target.name + ".type").unlink(missing_ok=True)


class FirestoreStorage(SignedLinks):
    """Files in Cloud Firestore, for the free Firebase plan (no Cloud Storage).

    One document per file in ``collection`` (ID: SHA-256 of the key) holds the
    key, type, size, checksum and chunk count; the bytes are in its ``chunks``
    subcollection, ``CHUNK`` bytes per document (Firestore allows 1 MiB). The
    file document is written last, so a half-written upload is never visible.
    Access is server-side only (the service account); security rules deny
    every browser read and write.
    """

    CHUNK = 700 * 1024
    PER_BATCH = 8  # about 5.6 MB per commit, under Firestore's request limit

    def __init__(self, client: Any, collection: str, signing_key: str, base_url: str) -> None:
        super().__init__(signing_key, base_url)
        self._files = client.collection(collection)
        self._client = client

    def _doc(self, key: str) -> Any:
        return self._files.document(hashlib.sha256(key.encode()).hexdigest())

    def stat(self, key: str) -> ObjectInfo | None:
        snapshot = self._doc(key).get()
        if not snapshot.exists:
            return None
        data = snapshot.to_dict() or {}
        return ObjectInfo(size=int(data.get("size", 0)), content_type=data.get("content_type"))

    def read(self, key: str) -> bytes:
        doc = self._doc(key)
        snapshot = doc.get()
        if not snapshot.exists:
            raise FileNotFoundError(key)
        meta = snapshot.to_dict() or {}
        parts = [
            bytes((doc.collection("chunks").document(f"{n:05d}").get().to_dict() or {})["data"])
            for n in range(int(meta["chunks"]))
        ]
        data = b"".join(parts)
        if len(data) != int(meta["size"]) or hashlib.sha256(data).hexdigest() != meta["sha256"]:
            raise OSError(f"stored file {key} is incomplete")
        return data

    def write(self, key: str, data: bytes, content_type: str) -> None:
        doc = self._doc(key)
        self._delete_chunks(doc)
        pieces = [data[i : i + self.CHUNK] for i in range(0, len(data), self.CHUNK)] or [b""]
        for start in range(0, len(pieces), self.PER_BATCH):
            batch = self._client.batch()
            for n, piece in enumerate(pieces[start : start + self.PER_BATCH], start=start):
                batch.set(doc.collection("chunks").document(f"{n:05d}"), {"data": piece})
            batch.commit()
        doc.set(
            {
                "key": key,
                "content_type": content_type,
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "chunks": len(pieces),
                "created_at": datetime.now(UTC),
            }
        )

    def _delete_chunks(self, doc: Any) -> None:
        refs = [snap.reference for snap in doc.collection("chunks").stream()]
        for start in range(0, len(refs), 400):
            batch = self._client.batch()
            for ref in refs[start : start + 400]:
                batch.delete(ref)
            batch.commit()

    def delete(self, key: str) -> None:
        doc = self._doc(key)
        doc.delete()  # the file disappears first, then its pieces
        self._delete_chunks(doc)


def firestore_client() -> Any:
    """Firestore with the Firebase service-account key. Replaced in tests."""
    from google.cloud import firestore
    from google.oauth2 import service_account

    from app.auth.firebase import service_account_info

    info = service_account_info()
    if info is None:
        raise RuntimeError("Set PRUFLO_FIREBASE_CREDENTIALS to use Firestore storage.")
    credentials = service_account.Credentials.from_service_account_info(info)  # type: ignore[no-untyped-call]
    project = get_settings().firebase_project or str(info["project_id"])
    return firestore.Client(project=project, credentials=credentials)


@lru_cache
def _default_storage() -> Storage:
    settings = get_settings()
    if settings.storage_backend == "gcs":
        return GcsStorage(settings.evidence_bucket)
    signing_key = settings.storage_signing_key
    if not signing_key:
        if settings.env not in ("local", "test"):
            raise RuntimeError("Set PRUFLO_STORAGE_SIGNING_KEY to sign upload and view links.")
        signing_key = _LOCAL_SIGNING_KEY
    if settings.storage_backend == "firestore":
        return FirestoreStorage(
            firestore_client(), settings.firestore_collection, signing_key, settings.public_base_url
        )
    if settings.env not in ("local", "test"):
        raise RuntimeError("Local storage is only allowed when PRUFLO_ENV is local or test.")
    return LocalStorage(settings.local_storage_dir, signing_key, settings.public_base_url)


# Local development only, when no signing key is set.
_LOCAL_SIGNING_KEY = secrets.token_urlsafe(32)


def get_storage() -> Storage:
    """FastAPI dependency; tests override it."""
    return _default_storage()
