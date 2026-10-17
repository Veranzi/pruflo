"""Firebase ID token and App Check verification.

The user's identity always comes from a verified token, never from the
request body.
"""

import json
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import firebase_admin
import jwt
from firebase_admin import app_check, auth

from app.config import get_settings
from app.errors import ApiError, unauthorized

logger = logging.getLogger("pruflo.auth")

DEV_TOKEN_PREFIX = "dev:"  # noqa: S105 (a prefix, not a secret)


@dataclass(frozen=True)
class FirebaseIdentity:
    uid: str
    email: str
    email_verified: bool
    name: str


def service_account_info() -> dict[str, Any] | None:
    """The Firebase service-account key, from PRUFLO_FIREBASE_CREDENTIALS (the
    JSON itself, as hosts store secrets) or PRUFLO_FIREBASE_CREDENTIALS_FILE.
    None on Google Cloud, where the runtime identity is used instead."""
    settings = get_settings()
    raw = settings.firebase_credentials
    if not raw and settings.firebase_credentials_file:
        raw = Path(settings.firebase_credentials_file).read_text()
    if not raw:
        return None
    info: dict[str, Any] = json.loads(raw)
    return info


@lru_cache
def _firebase_app() -> firebase_admin.App:
    settings = get_settings()
    project = settings.firebase_project or settings.gcp_project
    info = service_account_info()
    if info is None:
        # On Google Cloud this uses the runtime service account; no key file.
        return firebase_admin.initialize_app(options={"projectId": project})
    from firebase_admin import credentials

    return firebase_admin.initialize_app(
        credentials.Certificate(info), options={"projectId": project or info["project_id"]}
    )


def dev_mode_enabled() -> bool:
    settings = get_settings()
    # Never honoured outside local and test, whatever the flag says.
    return settings.auth_dev_mode and settings.env in ("local", "test")


def _verify_dev_token(token: str) -> FirebaseIdentity:
    # "dev:<uid>:<email>" — lets local tools and tests act as a user.
    parts = token.removeprefix(DEV_TOKEN_PREFIX).split(":", 1)
    if len(parts) != 2 or not parts[0] or "@" not in parts[1]:
        raise unauthorized("Malformed development token.")
    uid, email = parts
    return FirebaseIdentity(uid=uid, email=email.lower(), email_verified=True, name="")


def verify_id_token(token: str) -> FirebaseIdentity:
    if token.startswith(DEV_TOKEN_PREFIX):
        if not dev_mode_enabled():
            raise unauthorized("Invalid token.")
        return _verify_dev_token(token)
    import google.auth.exceptions
    from firebase_admin import exceptions as firebase_exceptions

    try:
        claims: dict[str, Any] = auth.verify_id_token(
            token, app=_firebase_app(), check_revoked=get_settings().firebase_check_revoked
        )
    except (auth.RevokedIdTokenError, auth.UserDisabledError) as exc:
        raise unauthorized("Your session has ended. Sign in again.") from exc
    except (ValueError, auth.InvalidIdTokenError) as exc:
        raise unauthorized("Invalid token.") from exc
    except (
        auth.CertificateFetchError,
        firebase_exceptions.FirebaseError,
        google.auth.exceptions.GoogleAuthError,
    ) as exc:
        # Our problem, not the caller's: Google's keys or the revocation check
        # (which needs admin access to the Firebase project) couldn't be reached.
        logger.warning("Firebase token check unavailable: %s", type(exc).__name__)
        raise ApiError(
            503, "auth_unavailable", "Sign-in can't be checked right now. Try again shortly."
        ) from exc
    return FirebaseIdentity(
        uid=str(claims["uid"]),
        email=str(claims.get("email", "")).lower(),
        email_verified=bool(claims.get("email_verified", False)),
        name=str(claims.get("name", "")),
    )


def verify_app_check(token: str | None) -> None:
    """Defence in depth only: App Check is never a substitute for authorization."""
    settings = get_settings()
    if not settings.app_check_required or settings.env in ("local", "test"):
        return
    if not token:
        raise unauthorized("Missing App Check token.")
    try:
        app_check.verify_token(token, app=_firebase_app())
    except (ValueError, jwt.PyJWTError) as exc:
        raise unauthorized("Invalid App Check token.") from exc
