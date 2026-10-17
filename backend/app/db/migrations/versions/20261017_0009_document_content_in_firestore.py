"""document content in firestore

Page text and text blocks are unstructured, so they moved to the document
store (Firestore in production, app/storage/content.py). Files processed
before this change need processing again to get their pages back.

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-17 23:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index(op.f("ix_text_blocks_evidence_id"), table_name="text_blocks")
    op.drop_table("text_blocks")
    op.drop_table("document_pages")


def downgrade() -> None:
    op.create_table(
        "document_pages",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("evidence_id", sa.String(length=40), nullable=False),
        sa.Column("page_index", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(length=120), server_default="", nullable=False),
        sa.Column("text", sa.Text(), server_default="", nullable=False),
        sa.Column("width", sa.Numeric(), nullable=True),
        sa.Column("height", sa.Numeric(), nullable=True),
        sa.Column(
            "coordinate_unit", sa.String(length=20), server_default="normalized", nullable=False
        ),
        sa.Column("parser", sa.String(length=60), nullable=False),
        sa.Column("parser_version", sa.String(length=80), nullable=False),
        sa.ForeignKeyConstraint(
            ["evidence_id"],
            ["evidence_files.id"],
            name=op.f("fk_document_pages_evidence_id_evidence_files"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_pages")),
        sa.UniqueConstraint(
            "evidence_id", "page_index", name=op.f("uq_document_pages_evidence_id")
        ),
    )
    op.create_table(
        "text_blocks",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("page_id", sa.String(length=40), nullable=False),
        sa.Column("evidence_id", sa.String(length=40), nullable=False),
        sa.Column("block_index", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("char_start", sa.Integer(), nullable=False),
        sa.Column("char_end", sa.Integer(), nullable=False),
        sa.Column("bbox", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("cell_ref", sa.String(length=20), nullable=True),
        sa.Column("confidence", sa.Numeric(precision=5, scale=4), nullable=True),
        sa.ForeignKeyConstraint(
            ["evidence_id"],
            ["evidence_files.id"],
            name=op.f("fk_text_blocks_evidence_id_evidence_files"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["page_id"],
            ["document_pages.id"],
            name=op.f("fk_text_blocks_page_id_document_pages"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_text_blocks")),
        sa.UniqueConstraint("page_id", "block_index", name=op.f("uq_text_blocks_page_id")),
    )
    op.create_index(
        op.f("ix_text_blocks_evidence_id"), "text_blocks", ["evidence_id"], unique=False
    )
