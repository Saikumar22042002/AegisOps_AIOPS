"""P2.8: prompt registry (Redesign/05 §9, 06 §8.2, 07 P2.8).

Prompts become versioned artifacts, not string literals: PK(name, version), content_hash,
eval_state. Every ledger row's `prompt_version` and every Langfuse generation reference a
PromptRef, so "which prompt caused this regression?" is answerable.

Revision ID: 0014_prompt_registry
Revises: 0013_memory_items
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014_prompt_registry"
down_revision = "0013_memory_items"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "prompt_registry",
        sa.Column("name", sa.String(80), primary_key=True, nullable=False),
        sa.Column("version", sa.Integer, primary_key=True, nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("owner", sa.String(120), nullable=True),
        sa.Column("changelog", sa.Text, nullable=True),
        sa.Column("eval_state", sa.String(12), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.CheckConstraint(
            "eval_state IN ('pending','passed','failed','waived')",
            name="ck_prompt_registry_eval_state"),
    )


def downgrade() -> None:
    op.drop_table("prompt_registry")
