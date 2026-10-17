"""Pruflo Cloud Run API (FastAPI).

/v1 routes verify the Firebase ID token (or an API key) and App Check token,
then apply case-level authorization. See Docs/backend_plan.md.
"""

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.api.v1 import objects
from app.api.v1 import router as v1_router
from app.config import get_settings
from app.db.session import database_ready, get_db
from app.errors import install_error_handlers
from app.logging import configure_logging
from app.middleware import install_request_middleware


def install_cors(app: FastAPI) -> None:
    settings = get_settings()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        # `flutter run -d chrome` picks a random localhost port.
        allow_origin_regex=(
            r"http://(localhost|127\.0\.0\.1)(:\d+)?" if settings.env in ("local", "test") else None
        ),
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "X-Firebase-AppCheck",
            "X-Pruflo-Tenant",
            "X-Request-ID",
        ],
        expose_headers=["X-Request-ID", "Retry-After"],
        max_age=600,
    )


def create_app() -> FastAPI:
    configure_logging()
    # Interactive docs only where developers work; the contracts are in Docs/.
    show_docs = get_settings().env in ("local", "test", "dev")
    app = FastAPI(
        title="Pruflo API",
        version="0.1.0",
        docs_url="/docs" if show_docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if show_docs else None,
    )
    install_error_handlers(app)
    install_request_middleware(app)
    install_cors(app)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        """Liveness: the process answers. Carries the release (commit) deployed."""
        return {"status": "ok", "release": get_settings().release}

    @app.get("/readyz", include_in_schema=False, response_model=None)
    def readyz(db: Session = Depends(get_db)) -> dict[str, str] | JSONResponse:
        """Readiness (Cloud Run startup probe): the database answers."""
        return database_ready(db)

    app.include_router(v1_router, prefix="/v1")
    # Signed upload and view links for Firestore and local storage.
    app.include_router(objects.router, prefix="/v1")
    return app


app = create_app()
