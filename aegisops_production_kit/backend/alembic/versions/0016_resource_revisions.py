"""Immutable resource revision history (forensic-audit remediation, 2026-08-16).

Additive only. The `resources` table stays the upsert-by-name CURRENT-state inventory; this
table is the append-only change journal that makes "what changed / when / what was the
previous configuration / who changed it" answerable from deterministic data. One row per
lifecycle event (created | modified | destroyed | failed | partial | orphaned | no_change |
unknown), written in the same transaction as the inventory mutation. Rows are never updated
or deleted by application code.

Revision ID: 0016_resource_revisions
Revises: 0015_durable_execution
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0016_resource_revisions"
down_revision = "0015_durable_execution"
branch_labels = None
depends_on = None

_ACTIONS = ("created", "modified", "destroyed", "failed", "partial",
            "orphaned", "no_change", "unknown")


def upgrade() -> None:
    op.create_table(
        "resource_revisions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("org_id", UUID(as_uuid=True),
                  sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("resource_id", UUID(as_uuid=True),
                  sa.ForeignKey("resources.id", ondelete="SET NULL"), nullable=True),
        sa.Column("run_id", UUID(as_uuid=True),
                  sa.ForeignKey("runs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("session_id", UUID(as_uuid=True),
                  sa.ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True),
        sa.Column("actor_user", sa.String(160), nullable=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("cloud", sa.String(20), nullable=False),
        sa.Column("region", sa.String(64), nullable=True),
        sa.Column("resource_type", sa.String(60), nullable=False),
        sa.Column("action", sa.String(20), nullable=False),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column("before_state", JSONB, nullable=True),
        sa.Column("after_state", JSONB, nullable=True),
        sa.Column("inputs", JSONB, nullable=True),
        sa.Column("execution_result", sa.String(40), nullable=True),
        sa.Column("verification", JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.CheckConstraint(
            "action IN (" + ", ".join(f"'{a}'" for a in _ACTIONS) + ")",
            name="ck_resource_revisions_action"),
    )
    op.create_index("ix_resource_revisions_org_id", "resource_revisions", ["org_id"])
    op.create_index("ix_resource_revisions_resource_id", "resource_revisions", ["resource_id"])
    op.create_index("ix_resource_revisions_run_id", "resource_revisions", ["run_id"])
    op.create_index("ix_resource_revisions_name", "resource_revisions", ["name"])
    op.create_index("ix_resource_revisions_created_at", "resource_revisions", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_resource_revisions_created_at", table_name="resource_revisions")
    op.drop_index("ix_resource_revisions_name", table_name="resource_revisions")
    op.drop_index("ix_resource_revisions_run_id", table_name="resource_revisions")
    op.drop_index("ix_resource_revisions_resource_id", table_name="resource_revisions")
    op.drop_index("ix_resource_revisions_org_id", table_name="resource_revisions")
    op.drop_table("resource_revisions")
