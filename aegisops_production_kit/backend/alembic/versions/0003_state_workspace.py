"""per-resource Terraform state workspace (Phase 8 / N-08 state isolation)

Revision ID: 0003_state_workspace
Revises: 0002_resources
Create Date: 2026-07-05
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_state_workspace"
down_revision: str | None = "0002_resources"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # NULL = legacy resource living in the module's default Terraform workspace (pre-isolation);
    # new applies always record their per-resource workspace slug.
    op.add_column("resources", sa.Column("state_workspace", sa.String(80), nullable=True))


def downgrade() -> None:
    op.drop_column("resources", "state_workspace")
