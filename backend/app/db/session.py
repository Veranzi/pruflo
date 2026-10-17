from collections.abc import Iterator
from functools import lru_cache

from fastapi.responses import JSONResponse
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings


@lru_cache
def get_engine() -> Engine:
    # Small pool per instance: total connections = pool size x Cloud Run max instances.
    settings = get_settings()
    return create_engine(
        settings.sqlalchemy_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,
        pool_recycle=1800,  # Cloud SQL drops idle connections
        connect_args={"connect_timeout": 10},
    )


@lru_cache
def _session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """FastAPI dependency. Routes commit explicitly; anything uncommitted rolls back."""
    with _session_factory()() as session:
        yield session


def database_ready(db: Session) -> dict[str, str] | JSONResponse:
    """For /readyz on the API and worker: can we run a query right now?"""
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        return JSONResponse({"status": "database_unavailable"}, status_code=503)
    return {"status": "ready", "release": get_settings().release}
