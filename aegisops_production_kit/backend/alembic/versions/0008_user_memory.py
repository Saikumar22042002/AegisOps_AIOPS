"""per-user/org persistent memory (M4)

Revision ID: 0008_user_memory
Revises: 0007_module_proposals
Create Date: 2026-07-12

User-editable standing context ("my usual region is ap-south-1") that survives sessions and is
threaded into every LLM call via build_context. Org-scoped; user_id NULL = org-wide standing
context. One row per (org, user, key) — setting a key again overwrites it.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0008_user_memory"
down_revision: str | None = "0007_module_proposals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_memories",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("org_id", UUID(as_uuid=True),
                  sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False,
                  index=True),
        sa.Column("user_id", UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True),
        sa.Column("key", sa.String(80), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    # Partial unique indexes: (org,user,key) for user rows; (org,key) for org-wide rows —
    # a single nullable-column UNIQUE would let duplicate org-wide keys through.
    op.execute("CREATE UNIQUE INDEX uq_user_memories_user_key ON user_memories (org_id, user_id, key) "
               "WHERE user_id IS NOT NULL")
    op.execute("CREATE UNIQUE INDEX uq_user_memories_org_key ON user_memories (org_id, key) "
               "WHERE user_id IS NULL")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_user_memories_org_key")
    op.execute("DROP INDEX IF EXISTS uq_user_memories_user_key")
    op.drop_table("user_memories")
