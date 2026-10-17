"""Separate migration and runtime database roles (Docs/backend_plan.md, Step 2).

Uses its own database and test-only roles, so the shared test database and
the local roles are left alone.
"""

from collections.abc import Iterator

import pytest
from alembic import command
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ProgrammingError

from app.db.roles import apply_roles
from tests.conftest import ADMIN_URL, alembic_config

DB = "pruflo_roles_test"
MIGRATOR, RUNTIME = "pruflo_test_migrator", "pruflo_test_app"
PASSWORD = "roles-test-password"


def _url(user: str | None = None) -> str:
    url = make_url(ADMIN_URL).set(database=DB)
    if user:
        url = url.set(username=user, password=PASSWORD)
    return url.render_as_string(hide_password=False)


def _drop(admin: Engine) -> None:
    with admin.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)"))
        for role in (MIGRATOR, RUNTIME):
            conn.execute(text(f"DROP ROLE IF EXISTS {role}"))


@pytest.fixture(scope="module")
def roles_db() -> Iterator[None]:
    admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    _drop(admin)
    with admin.connect() as conn:
        conn.execute(text(f"CREATE DATABASE {DB}"))
    command.upgrade(alembic_config(_url()), "head")
    setup = create_engine(_url())
    with setup.begin() as conn:
        apply_roles(
            conn,
            migrator=MIGRATOR,
            runtime=RUNTIME,
            migrator_password=PASSWORD,
            runtime_password=PASSWORD,
        )
        # Running it again changes nothing.
        apply_roles(conn, migrator=MIGRATOR, runtime=RUNTIME, create=False)
    setup.dispose()
    yield
    _drop(admin)
    admin.dispose()


def _as(user: str) -> Engine:
    return create_engine(_url(user))


def _denied(engine: Engine, sql: str) -> bool:
    with engine.connect() as conn:
        try:
            conn.execute(text(sql))
        except ProgrammingError as exc:
            conn.rollback()
            return "permission denied" in str(exc) or "must be owner" in str(exc)
        conn.rollback()
        return False


def test_runtime_role_reads_and_writes_data(roles_db: None) -> None:
    app = _as(RUNTIME)
    with app.connect() as conn:
        conn.execute(text("INSERT INTO tenants (id, name) VALUES ('ten_r', 'Roles')"))
        conn.execute(text("UPDATE tenants SET name = 'Renamed' WHERE id = 'ten_r'"))
        conn.execute(
            text(
                "INSERT INTO audit_events (id, tenant_id, action, target_type, target_id, "
                "request_id) VALUES ('aud_r', 'ten_r', 'x', 'case', 'c', 'req')"
            )
        )
        assert conn.execute(text("SELECT name FROM tenants WHERE id = 'ten_r'")).scalar() == (
            "Renamed"
        )
        conn.rollback()
    app.dispose()


def test_runtime_role_cant_change_append_only_tables(roles_db: None) -> None:
    app = _as(RUNTIME)
    for table in ("audit_events", "review_actions"):
        assert _denied(app, f"UPDATE {table} SET id = id")  # noqa: S608
        assert _denied(app, f"DELETE FROM {table}")  # noqa: S608
        assert _denied(app, f"TRUNCATE {table}")
        # Owners could switch the trigger off; the runtime role isn't one.
        assert _denied(app, f"ALTER TABLE {table} DISABLE TRIGGER ALL")
    app.dispose()


def test_runtime_role_cant_change_the_schema(roles_db: None) -> None:
    app = _as(RUNTIME)
    assert _denied(app, "CREATE TABLE sneaky (id int)")
    assert _denied(app, "ALTER TABLE cases ADD COLUMN sneaky int")
    assert _denied(app, "DROP TABLE notifications")
    assert _denied(app, "UPDATE alembic_version SET version_num = 'x'")
    app.dispose()


def test_migration_role_owns_the_schema(roles_db: None) -> None:
    migrator = _as(MIGRATOR)
    with migrator.connect() as conn:
        conn.execute(text("CREATE TABLE later_table (id int)"))
        conn.execute(text("ALTER TABLE cases ADD COLUMN later int"))
        conn.commit()
    # Tables migrations add later are usable by the app straight away.
    app = _as(RUNTIME)
    with app.connect() as conn:
        conn.execute(text("INSERT INTO later_table VALUES (1)"))
        conn.rollback()
    with migrator.connect() as conn:
        conn.execute(text("DROP TABLE later_table"))
        conn.execute(text("ALTER TABLE cases DROP COLUMN later"))
        conn.commit()
    app.dispose()
    migrator.dispose()
