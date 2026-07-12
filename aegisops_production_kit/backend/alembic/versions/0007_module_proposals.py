"""module proposals — the Module Promotion Pipeline's state (MPP)

Revision ID: 0007_module_proposals
Revises: 0006_message_embeddings
Create Date: 2026-07-12

A drafted Terraform module lives here through draft → checks (fmt/validate/scan) → proposed →
promoted|rejected. Only PROMOTED rows join the approved library (selectable by the agent);
generation and execution never happen in the same turn — a draft is data until a human
reviewer promotes it.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0007_module_proposals"
down_revision: str | None = "0006_message_embeddings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "module_proposals",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("org_id", UUID(as_uuid=True),
                  sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False,
                  index=True),
        sa.Column("key", sa.String(80), nullable=False),          # e.g. "aws.efs"
        sa.Column("cloud", sa.String(20), nullable=False),
        sa.Column("resource", sa.String(60), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("files", JSONB(), nullable=False),              # {filename: content}
        sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
        # draft | proposed | promoted | rejected
        sa.Column("fmt_ok", sa.Boolean(), nullable=True),
        sa.Column("validate_ok", sa.Boolean(), nullable=True),
        sa.Column("scan", JSONB(), nullable=True),                # {tool,status,findings}
        sa.Column("created_by", sa.String(120), nullable=True),
        sa.Column("reviewed_by", sa.String(120), nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_module_proposals_org_status", "module_proposals", ["org_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_module_proposals_org_status", table_name="module_proposals")
    op.drop_table("module_proposals")
