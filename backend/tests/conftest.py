"""Shared fixtures. Database tests need Postgres from docker-compose.yml:

    docker compose up -d postgres

They run against a separate ``pruflo_test`` database, migrated once per run.
Each test runs inside a transaction that is rolled back afterwards.
"""

import os
from collections.abc import Iterator
from contextlib import nullcontext
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.config import get_settings
from app.storage.backend import LocalStorage
from app.storage.content import LocalContent, set_content_store
from app.worker.queue import InlinePublisher, Queue
from tests.factories import Factory

os.environ.setdefault("PRUFLO_ENV", "test")
os.environ.setdefault("PRUFLO_AUTH_DEV_MODE", "true")
os.environ.setdefault("PRUFLO_APP_CHECK_REQUIRED", "false")

# The same server as local development (credentials from backend/.env), unless overridden.
ADMIN_URL = os.environ.get("PRUFLO_TEST_ADMIN_URL") or get_settings().sqlalchemy_url
TEST_URL = make_url(ADMIN_URL).set(database="pruflo_test").render_as_string(hide_password=False)


def alembic_config(url: str) -> Config:
    cfg = Config(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option(
        "script_location", os.path.join(os.path.dirname(__file__), "..", "app/db/migrations")
    )
    cfg.attributes["database_url"] = url
    return cfg


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text("DROP DATABASE IF EXISTS pruflo_test WITH (FORCE)"))
        conn.execute(text("CREATE DATABASE pruflo_test"))
    admin.dispose()
    command.upgrade(alembic_config(TEST_URL), "head")
    eng = create_engine(TEST_URL)
    yield eng
    eng.dispose()


@pytest.fixture
def connection(engine: Engine) -> Iterator[Connection]:
    with engine.connect() as conn:
        outer = conn.begin()
        yield conn
        outer.rollback()


@pytest.fixture
def db(connection: Connection) -> Iterator[Session]:
    """A session whose commits become savepoints, so the test can still roll back."""
    with Session(
        bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
    ) as session:
        yield session


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(str(tmp_path / "objects"), "test-signing-key", "http://testserver")


@pytest.fixture(autouse=True)
def content(tmp_path: Path) -> Iterator[LocalContent]:
    """Extracted page text and blocks (Firestore in production), on disk here."""
    store = LocalContent(str(tmp_path / "content"))
    set_content_store(store)
    yield store
    set_content_store(None)


@pytest.fixture
def queue(db: Session, storage: LocalStorage) -> Queue:
    """Runs jobs inline on the test's session, without OCR."""
    return Queue(lambda: nullcontext(db), InlinePublisher(), storage, None, inline=True)


@pytest.fixture
def client(db: Session, storage: LocalStorage, queue: Queue) -> Iterator[TestClient]:
    """The API, using the test's session so everything rolls back afterwards."""
    from app.auth.api_keys import rate_limiter
    from app.db.session import get_db
    from app.main import create_app
    from app.storage.backend import get_storage
    from app.worker.queue import get_queue

    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_queue] = lambda: queue
    rate_limiter.reset()
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def make(db: Session) -> Factory:
    return Factory(db)
