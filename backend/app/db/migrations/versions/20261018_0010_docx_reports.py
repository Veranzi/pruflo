"""docx reports

Reports can be generated as Word documents as well as PDFs.

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-18 01:30:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD = "format IN ('pdf', 'json', 'csv')"
NEW = "format IN ('pdf', 'docx', 'json', 'csv')"


def upgrade() -> None:
    op.drop_constraint(op.f("ck_reports_format_valid"), "reports", type_="check")
    op.create_check_constraint(op.f("ck_reports_format_valid"), "reports", NEW)


def downgrade() -> None:
    op.execute("UPDATE reports SET format = 'pdf' WHERE format = 'docx'")
    op.drop_constraint(op.f("ck_reports_format_valid"), "reports", type_="check")
    op.create_check_constraint(op.f("ck_reports_format_valid"), "reports", OLD)
