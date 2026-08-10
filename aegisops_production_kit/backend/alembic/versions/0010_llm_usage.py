"""P0: authoritative LLM usage/cost ledger (Redesign/06 §8.2, 07 item 0.3, defect D3)

Revision ID: 0010_llm_usage
Revises: 0009_channel_identities
Create Date: 2026-08-09

Accounting truth for every model invocation — tokens are ground truth, `cost_usd` is a
write-time convenience snapshot. Langfuse remains observability; this table is the
billing/accounting record that survives trace-store key rotation, retention, and outages.

Design notes:
* `id` is CLIENT-generated (no server default) — the spill-journal replay path re-inserts
  records idempotently via INSERT .. ON CONFLICT (id) DO NOTHING, so a retry or replay can
  never double-count.
* `org_id`/`run_id` deliberately carry NO foreign keys: an accounting record must outlive
  the principal/run it describes, and spill replay must never fail an FK check.
* `task_id`/`prompt_version` are forward-compatible nullable columns (populated when the
  durable Task system and prompt registry land in P2/P3).
* Append-only by convention: nothing in the application updates or deletes rows.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision: str = "0010_llm_usage"
down_revision: str | None = "0009_channel_identities"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "llm_usage",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),  # client-generated
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("org_id", UUID(as_uuid=True), nullable=True),
        sa.Column("run_id", UUID(as_uuid=True), nullable=True),
        sa.Column("task_id", UUID(as_uuid=True), nullable=True),
        sa.Column("purpose", sa.String(40), nullable=False, server_default="legacy"),
        sa.Column("provider", sa.String(20), nullable=False, server_default="google"),
        sa.Column("model", sa.String(80), nullable=False),
        sa.Column("requested_model", sa.String(80), nullable=True),
        sa.Column("agent_kind", sa.String(20), nullable=False, server_default="main"),
        sa.Column("prompt_version", sa.String(40), nullable=True),
        sa.Column("input_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("total_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Float, nullable=True),
        sa.Column("latency_ms", sa.Integer, nullable=True),
        sa.Column("outcome", sa.String(60), nullable=False, server_default="ok"),
    )
    # Spend-by-org over time is the primary read (chargeback / budgets-to-come).
    op.create_index("ix_llm_usage_org_ts", "llm_usage", ["org_id", "ts"])
    op.create_index("ix_llm_usage_run", "llm_usage", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_llm_usage_run", table_name="llm_usage")
    op.drop_index("ix_llm_usage_org_ts", table_name="llm_usage")
    op.drop_table("llm_usage")
