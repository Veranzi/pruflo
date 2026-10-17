"""gemini api provider

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-17 18:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD = "ai IN ('vertex_gemini', 'vertex_claude', 'azure_openai', 'self_hosted')"
NEW = "ai IN ('vertex_gemini', 'gemini_api', 'vertex_claude', 'azure_openai', 'self_hosted')"


def upgrade() -> None:
    op.drop_constraint(op.f("ck_provider_settings_ai_valid"), "provider_settings", type_="check")
    op.create_check_constraint(op.f("ck_provider_settings_ai_valid"), "provider_settings", NEW)


def downgrade() -> None:
    op.execute("UPDATE provider_settings SET ai = 'vertex_gemini' WHERE ai = 'gemini_api'")
    op.drop_constraint(op.f("ck_provider_settings_ai_valid"), "provider_settings", type_="check")
    op.create_check_constraint(op.f("ck_provider_settings_ai_valid"), "provider_settings", OLD)
