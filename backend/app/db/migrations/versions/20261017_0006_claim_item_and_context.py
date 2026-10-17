"""claim item and context

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-17 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("extracted_claims", sa.Column("item_key", sa.String(length=200), nullable=True))
    op.add_column(
        "extracted_claims",
        sa.Column("context", sa.String(length=300), server_default="", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("extracted_claims", "context")
    op.drop_column("extracted_claims", "item_key")
