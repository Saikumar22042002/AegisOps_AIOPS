"""P2.5: durable, event-sourced run log (Redesign/06 §8.2, ADR-16, 07 P2.5).

Two records, one owner each (ADR-16): the LangGraph checkpointer remains the resume
authority for the graph spine; `run_events` is the append-only record of what happened —
and the replay source for HARNESS loop resumption (P2). Redis streams stay the live
feed; UI tabs and Langfuse are projections.

`seq` is per-run monotonic and gapless (UNIQUE(run_id, seq) — behavioral invariant #1 of
Redesign/10 §0). Payloads are redacted BEFORE the write (invariant: redaction-clean).

Kind enum: the 17 kinds of 06 §8.2 plus `agent_gate` — the C-05 resolution recorded in
Redesign/11 §22 (doc 10 scenario Q asserts a retrieval-gate event; the enum gains it
here rather than scenario Q silently failing).

Revision ID: 0012_run_events
Revises: 0011_model_bindings
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0012_run_events"
down_revision = "0011_model_bindings"
branch_labels = None
depends_on = None

KINDS = (
    "iteration_started", "assistant_turn", "tool_call", "observation", "policy_verdict",
    "approval_requested", "approval_resolved", "step_started", "step_finished",
    "deviation", "verification", "compaction", "steering", "budget",
    "subagent_spawned", "subagent_result", "run_finished", "agent_gate",
)


def upgrade() -> None:
    op.create_table(
        "run_events",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("run_id", UUID(as_uuid=True), nullable=False),
        sa.Column("org_id", UUID(as_uuid=True), nullable=True),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("payload", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.UniqueConstraint("run_id", "seq", name="uq_run_events_run_seq"),
        sa.CheckConstraint("kind IN " + repr(KINDS), name="ck_run_events_kind"),
    )
    op.create_index("ix_run_events_run", "run_events", ["run_id"])
    op.create_index("ix_run_events_org_at", "run_events", ["org_id", "at"])


def downgrade() -> None:
    op.drop_table("run_events")
