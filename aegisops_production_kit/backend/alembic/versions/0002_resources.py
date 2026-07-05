"""provisioned-resource inventory (day-2 operations)

Revision ID: 0002_resources
Revises: 0001_initial
Create Date: 2026-07-04
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_resources"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "resources",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("org_id", sa.Uuid(), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("session_id", sa.Uuid(), sa.ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("runs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("cloud", sa.String(20), nullable=False),
        sa.Column("region", sa.String(40), nullable=True),
        sa.Column("resource_type", sa.String(60), nullable=False),
        sa.Column("provider_id", sa.String(200), nullable=True),
        sa.Column("workspace", sa.String(120), nullable=True),
        sa.Column("status", sa.String(20), server_default="active", nullable=False),
        sa.Column("attributes", postgresql.JSONB(), nullable=True),
        sa.Column("inputs", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_resources_org_id", "resources", ["org_id"])
    op.create_index("ix_resources_session_id", "resources", ["session_id"])
    op.create_index("ix_resources_name", "resources", ["name"])


def downgrade() -> None:
    op.drop_index("ix_resources_name", table_name="resources")
    op.drop_index("ix_resources_session_id", table_name="resources")
    op.drop_index("ix_resources_org_id", table_name="resources")
    op.drop_table("resources")
