"""Files through this API's signed links (FirestoreStorage and LocalStorage).

Every request must carry a valid, unexpired HMAC signature from the storage
backend. PUT checks the signed content type and size limit while the body
streams in, then hands the bytes to the store; GET returns the file. With
Cloud Storage (``gcs``) the browser talks to Google directly and these
routes answer 404.
"""

from urllib.parse import quote

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import FileResponse

from app.errors import ApiError, forbidden, not_found
from app.storage.backend import (
    LocalStorage,
    SignedLinks,
    Storage,
    attachment_extra,
    get_storage,
)

router = APIRouter(prefix="/objects", include_in_schema=False)


def _signed(storage: Storage) -> SignedLinks:
    if not isinstance(storage, SignedLinks):
        raise not_found()
    return storage


@router.put("/{key:path}", status_code=200)
async def put_object(
    key: str,
    request: Request,
    expires: int,
    sig: str,
    max: int,
    ct: str,
    storage: Storage = Depends(get_storage),
) -> Response:
    links = _signed(storage)
    if not links.verify("PUT", key, expires, sig, f"{ct}\n{max}"):
        raise forbidden("Invalid or expired upload link.")
    if request.headers.get("content-type", "").split(";")[0] != ct:
        raise forbidden("Content-Type must match the signed upload.")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > max:
            raise ApiError(413, "file_too_large", "The file is larger than allowed.")
    try:
        links.write(key, bytes(body), ct)
    except Exception as exc:
        raise ApiError(
            503, "storage_unavailable", "The file couldn't be stored. Try again."
        ) from exc
    return Response(status_code=200)


@router.get("/{key:path}", response_model=None)
def get_object(
    key: str,
    expires: int,
    sig: str,
    dl: str | None = None,
    storage: Storage = Depends(get_storage),
) -> Response:
    links = _signed(storage)
    extra = attachment_extra(dl) if dl is not None else ""
    if not links.verify("GET", key, expires, sig, extra):
        raise forbidden("Invalid or expired link.")
    info = links.stat(key)
    if info is None:
        raise not_found()
    name = dl if dl is not None else key.rsplit("/", 1)[-1]
    disposition = f"{'attachment' if dl is not None else 'inline'}; filename*=UTF-8''{quote(name)}"
    if isinstance(links, LocalStorage):
        return FileResponse(
            links.path(key),
            media_type=info.content_type,
            headers={"Content-Disposition": disposition},
        )
    return Response(
        links.read(key),
        media_type=info.content_type or "application/octet-stream",
        headers={
            "Content-Disposition": disposition,
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
