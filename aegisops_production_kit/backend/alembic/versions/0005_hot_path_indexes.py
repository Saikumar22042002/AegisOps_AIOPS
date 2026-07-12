"""hot-path indexes (D1) — transcript load, artifact _load, module/overview counts

Revision ID: 0005_hot_path_indexes
Revises: 0004_run_initiated_by
Create Date: 2026-07-12

These back the queries run on every chat turn / artifact fetch, which previously risked
sequential scans at scale:
  * messages(session_id, created_at) — transcript load + /sessions/{id}/messages ordering
  * messages(run_id)                 — artifact _load filters by run_id
  * runs(session_id)                 — per-session run lookups
  * runs(org_id, created_at)         — module/overview counts + recent-run lists
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005_hot_path_indexes"
down_revision: str | None = "0004_run_initiated_by"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEXES = [
    ("ix_messages_session_created", "messages", ["session_id", "created_at"]),
    ("ix_messages_run_id", "messages", ["run_id"]),
    ("ix_runs_session_id", "runs", ["session_id"]),
    ("ix_runs_org_created", "runs", ["org_id", "created_at"]),
]


def upgrade() -> None:
    for name, table, cols in _INDEXES:
        op.create_index(name, table, cols, if_not_exists=True)


def downgrade() -> None:
    for name, table, _cols in _INDEXES:
        op.drop_index(name, table_name=table, if_exists=True)
