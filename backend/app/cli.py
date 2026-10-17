"""Operator commands.

Create the first organisation and its admin (they sign in with this email):

    python -m app.cli bootstrap --tenant "Acme Ltd" \
        --admin-email admin@acme.com --admin-name "Ada Admin"

Invite someone to an existing organisation, or change their role:

    python -m app.cli invite --tenant ten_... --email someone@acme.com --role admin

Remove someone's access everywhere. The user row stays, with its email
cleared, because the append-only audit trail refers to it:

    python -m app.cli remove-user --email someone@acme.com

Set up the separate migration and runtime database roles (run as a database
admin; passwords come from the environment, never the command line):

    PRUFLO_ADMIN_DATABASE_URL=postgresql+psycopg://postgres:...@host/pruflo \
    PRUFLO_MIGRATOR_PASSWORD=... PRUFLO_RUNTIME_PASSWORD=... \
        python -m app.cli db-roles

On Cloud SQL, create both users with gcloud first and add ``--no-create``.

Send a test push to someone's registered browsers, through the real Firebase
Cloud Messaging (needs credentials for PRUFLO_FIREBASE_PROJECT):

    python -m app.cli test-push --email someone@acme.com

Check the real evidence bucket: region, private access, signed URLs and the
upload size limit (see app/storage/check.py):

    python -m app.cli check-storage
"""

import argparse
import os

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import ProviderSettings, Tenant, User
from app.db.roles import apply_roles
from app.db.session import get_engine
from app.enums import Role
from app.ids import new_id
from app.services import users

DEFAULT_MODEL = "gemini-2.5-flash"


def bootstrap(db: Session, tenant_name: str, admin_email: str, admin_name: str) -> Tenant:
    email = users.normalise_email(admin_email)
    tenant = Tenant(id=new_id("ten"), name=tenant_name.strip())
    db.add(tenant)
    db.flush()
    users.invite(db, tenant.id, email, Role.ADMIN, admin_name)
    # With a Gemini API key configured, new organisations use it; otherwise Vertex.
    ai = "gemini_api" if get_settings().gemini_api_key else "vertex_gemini"
    db.add(ProviderSettings(tenant_id=tenant.id, model=DEFAULT_MODEL, ai=ai))
    db.commit()
    return tenant


def invite(db: Session, tenant_id: str, email: str, role: Role, name: str = "") -> User:
    """Give ``email`` the role in the organisation. They link their account on first sign-in."""
    user = users.invite(db, tenant_id, email, role, name)
    db.commit()
    return user


def remove_user(db: Session, email: str) -> User:
    """Take away all access. The row is kept (audit events point at it) but anonymised."""
    user = db.scalar(select(User).where(User.email == users.normalise_email(email)))
    if user is None:
        raise ValueError(f"No user with email {email}")
    users.anonymise(db, user)
    db.commit()
    return user


def test_push(db: Session, email: str) -> str:
    from app.services import push

    user = db.scalar(select(User).where(User.email == users.normalise_email(email)))
    if user is None:
        raise ValueError(f"No user with email {email}")
    tried, dead = push.send_test(db, user.id)
    if not tried:
        return f"{email} has no registered devices. Sign in to the web app and allow notifications."
    return f"Sent to {tried} device(s); {dead} no longer valid and removed."


def db_roles(admin_url: str, migrator: str, runtime: str, create: bool) -> None:
    engine = create_engine(admin_url)
    try:
        with engine.begin() as conn:
            apply_roles(
                conn,
                migrator=migrator,
                runtime=runtime,
                migrator_password=os.environ.get("PRUFLO_MIGRATOR_PASSWORD"),
                runtime_password=os.environ.get("PRUFLO_RUNTIME_PASSWORD"),
                create=create,
            )
    finally:
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    boot = sub.add_parser("bootstrap", help="create an organisation and its first admin")
    boot.add_argument("--tenant", required=True)
    boot.add_argument("--admin-email", required=True)
    boot.add_argument("--admin-name", default="")
    inv = sub.add_parser("invite", help="invite someone to an organisation or change their role")
    inv.add_argument("--tenant", required=True, help="organisation ID, e.g. ten_...")
    inv.add_argument("--email", required=True)
    inv.add_argument("--role", required=True, choices=[r.value for r in Role])
    inv.add_argument("--name", default="")
    rem = sub.add_parser("remove-user", help="remove someone's access everywhere")
    rem.add_argument("--email", required=True)
    tp = sub.add_parser("test-push", help="send a test push to someone's devices")
    tp.add_argument("--email", required=True)
    roles = sub.add_parser("db-roles", help="set up the migration and runtime database roles")
    roles.add_argument("--migrator", default="pruflo_migrator")
    roles.add_argument("--runtime", default="pruflo_app")
    roles.add_argument(
        "--no-create", action="store_true", help="the roles already exist (Cloud SQL)"
    )
    sub.add_parser("check-storage", help="check the Cloud Storage bucket and signed URLs")
    args = parser.parse_args()

    if args.command == "check-storage":
        from app.storage import check

        try:
            passed = check.run()
        except ValueError as exc:
            parser.error(str(exc))
        raise SystemExit(0 if passed else 1)
    if args.command == "db-roles":
        admin_url = os.environ.get("PRUFLO_ADMIN_DATABASE_URL")
        if not admin_url:
            parser.error("Set PRUFLO_ADMIN_DATABASE_URL to an admin connection URL.")
        try:
            db_roles(admin_url, args.migrator, args.runtime, create=not args.no_create)
        except ValueError as exc:
            parser.error(str(exc))
        print(f"Roles ready: migrations as {args.migrator}, the app as {args.runtime}.")  # noqa: T201
        return

    with Session(get_engine()) as db:
        try:
            if args.command == "bootstrap":
                tenant_id = bootstrap(db, args.tenant, args.admin_email, args.admin_name).id
                message = f"Created {tenant_id}; {args.admin_email} is its admin."
            elif args.command == "test-push":
                message = test_push(db, args.email)
            elif args.command == "invite":
                invite(db, args.tenant, args.email, Role(args.role), args.name)
                message = f"Invited {args.email.strip().lower()} to {args.tenant} as {args.role}."
            else:
                remove_user(db, args.email)
                message = f"Removed {args.email.strip().lower()}."
        except ValueError as exc:
            parser.error(str(exc))
    print(message)  # noqa: T201


if __name__ == "__main__":
    main()
