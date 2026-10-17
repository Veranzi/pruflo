"""Database roles (Docs/backend_plan.md, Step 2).

- The migration role owns the schema and is the only one that runs
  migrations (Alembic, with ``PRUFLO_MIGRATION_DATABASE_URL``).
- The runtime role is what the API and worker log in as. It can read and
  write data but can't change the schema, and it can only add to the
  append-only tables (``review_actions``, ``audit_events``): the database
  refuses its UPDATE and DELETE there, on top of the triggers.

``apply_roles`` is idempotent: run it once after creating the database, and
again after migrations that add append-only tables. On Cloud SQL, create the
two users with gcloud first and pass ``create=False``.
"""

from sqlalchemy import Connection, text

APPEND_ONLY_TABLES = ("review_actions", "audit_events")


def _ident(name: str) -> str:
    if not name.replace("_", "").isalnum():
        raise ValueError(f"Unsafe role name: {name!r}")
    return f'"{name}"'


def apply_roles(
    conn: Connection,
    *,
    migrator: str = "pruflo_migrator",
    runtime: str = "pruflo_app",
    migrator_password: str | None = None,
    runtime_password: str | None = None,
    create: bool = True,
) -> None:
    """Create (optionally) and configure both roles. Run as a database admin."""
    m, r = _ident(migrator), _ident(runtime)
    if create:
        for role, password in ((migrator, migrator_password), (runtime, runtime_password)):
            exists = conn.execute(
                text("SELECT 1 FROM pg_roles WHERE rolname = :n"), {"n": role}
            ).scalar()
            if not exists:
                if not password:
                    raise ValueError(f"A password is needed to create {role}.")
                # Role names and passwords can't be bound parameters in DDL.
                quoted = password.replace("'", "''")
                conn.execute(text(f"CREATE ROLE {_ident(role)} LOGIN PASSWORD '{quoted}'"))
    database = conn.execute(text("SELECT current_database()")).scalar_one()
    db = _ident(database)
    # On Cloud SQL the admin isn't a true superuser: it can only hand tables to
    # a role it belongs to.
    if conn.execute(text("SELECT current_user")).scalar_one() != migrator:
        conn.execute(text(f"GRANT {m} TO CURRENT_USER"))

    # Nobody else gets anything by default.
    conn.execute(text(f"REVOKE ALL ON DATABASE {db} FROM PUBLIC"))
    conn.execute(text("REVOKE CREATE ON SCHEMA public FROM PUBLIC"))
    conn.execute(text(f"GRANT CONNECT, TEMPORARY ON DATABASE {db} TO {m}, {r}"))

    # The migration role owns the schema and everything in it.
    conn.execute(text(f"GRANT USAGE, CREATE ON SCHEMA public TO {m}"))
    owned = conn.execute(
        text(
            "SELECT c.relname, c.relkind FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'S', 'v', 'p')"
        )
    ).all()
    for name, kind in owned:
        if kind == "S" and _owned_by_column(conn, name):
            continue  # serial sequences follow their table's owner
        conn.execute(
            text(
                f"ALTER {'SEQUENCE' if kind == 'S' else 'TABLE'} public.{_ident(name)} OWNER TO {m}"
            )
        )
    for (function,) in conn.execute(
        text(
            "SELECT p.oid::regprocedure::text FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'public' AND p.prokind = 'f'"
        )
    ).all():
        conn.execute(text(f"ALTER FUNCTION {function} OWNER TO {m}"))

    # The runtime role: data only, no schema changes.
    conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {r}"))
    conn.execute(
        text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {r}")
    )
    conn.execute(text(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {r}"))
    conn.execute(
        text(f"REVOKE TRUNCATE, REFERENCES, TRIGGER ON ALL TABLES IN SCHEMA public FROM {r}")
    )
    # Tables future migrations create get the same grants automatically.
    conn.execute(
        text(
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {m} IN SCHEMA public "
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {r}"
        )
    )
    conn.execute(
        text(
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {m} IN SCHEMA public "
            f"GRANT USAGE, SELECT ON SEQUENCES TO {r}"
        )
    )
    for table in APPEND_ONLY_TABLES:
        if any(name == table for name, _ in owned):
            conn.execute(text(f"REVOKE UPDATE, DELETE ON public.{_ident(table)} FROM {r}"))
    # alembic_version belongs to migrations only.
    if any(name == "alembic_version" for name, _ in owned):
        conn.execute(text(f"REVOKE ALL ON public.alembic_version FROM {r}"))
        conn.execute(text(f"GRANT SELECT ON public.alembic_version TO {r}"))


def _owned_by_column(conn: Connection, sequence: str) -> bool:
    return bool(
        conn.execute(
            text(
                "SELECT 1 FROM pg_depend d JOIN pg_class s ON s.oid = d.objid "
                "WHERE s.relname = :s AND d.deptype IN ('a', 'i')"
            ),
            {"s": sequence},
        ).scalar()
    )
