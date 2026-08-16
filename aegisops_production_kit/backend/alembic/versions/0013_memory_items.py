"""P2.6: episodic/semantic memory tier (Redesign/06 §1, 07 P2.6).

`memory_items` is the tier above the transcript: consolidation writes PROPOSALS (never
direct agent writes — 06 §2 write-path security), a human accepts, and contradictions
SUPERSEDE rather than coexist (`supersedes` self-FK). 768-d pgvector, matching the pinned
embedding dimension (ADR-02, models.EMBED_DIM). DDL is 06 §1 verbatim.

Revision ID: 0013_memory_items
Revises: 0012_run_events
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import UUID

revision = "0013_memory_items"
down_revision = "0012_run_events"
branch_labels = None
depends_on = None

EMBED_DIM = 768


def upgrade() -> None:
    op.create_table(
        "memory_items",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("org_id", UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), nullable=True),
        sa.Column("kind", sa.String(16), nullable=False),          # fact | episode
        sa.Column("subject", sa.Text, nullable=True),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("embedding", Vector(EMBED_DIM), nullable=True),
        sa.Column("provenance", sa.String(24), nullable=False),    # human|consolidation_accepted|system
        sa.Column("origin_run_id", UUID(as_uuid=True), nullable=True),
        sa.Column("confidence", sa.Float, nullable=False, server_default="0.7"),
        sa.Column("importance", sa.Float, nullable=False, server_default="0.5"),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("supersedes", sa.BigInteger,
                  sa.ForeignKey("memory_items.id", ondelete="SET NULL"), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("created_by", sa.String(120), nullable=True),
        sa.CheckConstraint("kind IN ('fact','episode')", name="ck_memory_items_kind"),
        sa.CheckConstraint(
            "provenance IN ('human','consolidation_accepted','system')",
            name="ck_memory_items_provenance"),
        sa.CheckConstraint(
            "status IN ('active','superseded','expired','retracted')",
            name="ck_memory_items_status"),
    )
    op.create_index("ix_memory_items_org_kind", "memory_items", ["org_id", "kind", "status"])


def downgrade() -> None:
    op.drop_table("memory_items")
