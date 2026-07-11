"""runs.initiated_by + runs.env (A5 — initiator recorded; 4-eyes for Production)

Revision ID: 0004_run_initiated_by
Revises: 0003_state_workspace
Create Date: 2026-07-11
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_run_initiated_by"
down_revision: str | None = "0003_state_workspace"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # NULL = legacy run recorded before initiator tracking; 4-eyes cannot be enforced for
    # those and skips (new runs always carry their initiator under strict tenancy).
    op.add_column("runs", sa.Column("initiated_by", sa.Uuid(), nullable=True))
    op.create_foreign_key("fk_runs_initiated_by_users", "runs", "users",
                          ["initiated_by"], ["id"], ondelete="SET NULL")
    op.add_column("runs", sa.Column("env", sa.String(20), nullable=True))


def downgrade() -> None:
    op.drop_constraint("fk_runs_initiated_by_users", "runs", type_="foreignkey")
    op.drop_column("runs", "initiated_by")
    op.drop_column("runs", "env")
