"""P3: durable Task/Run/Step execution (Redesign/06 §8.1/§8.3, 07 P3.1/3.2/3.6).

Additive only:
- `tasks` — the user-visible objective container that may span runs (06 §8.1). `runs`
  gains a nullable `task_id` (no backfill needed; legacy runs simply have none).
- `run_steps` gains the durable-engine columns (06 §8.1 Step + wave/evidence/compensation):
  `wave`, `depends_on`, `idempotency_key`, `kind`, `compensation_of`, `evidence`. All
  nullable/defaulted — the existing exec_loop path ignores them, so old rows are valid.

No existing column is dropped (P3 boundary: never remove a field the new path stopped
reading). The full run-status machine (06 §8.3) is enforced in code (app/engine/status.py),
not a DB CHECK, so legacy literals remain writable during coexistence.

Revision ID: 0015_durable_execution
Revises: 0014_prompt_registry
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0015_durable_execution"
down_revision = "0014_prompt_registry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tasks",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", UUID(as_uuid=True),
                  sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("objective", sa.Text, nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="open"),
        sa.Column("created_by", UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index("ix_tasks_org", "tasks", ["org_id", "status"])

    op.add_column("runs", sa.Column("task_id", UUID(as_uuid=True),
                                    sa.ForeignKey("tasks.id", ondelete="SET NULL"),
                                    nullable=True))

    op.add_column("run_steps", sa.Column("wave", sa.Integer, nullable=True))
    op.add_column("run_steps", sa.Column("depends_on", sa.String(120), nullable=True))
    op.add_column("run_steps", sa.Column("idempotency_key", sa.String(200), nullable=True))
    op.add_column("run_steps", sa.Column("kind", sa.String(16), nullable=True))  # module|day2|k8s|read|gate
    op.add_column("run_steps", sa.Column("compensation_of", sa.String(120), nullable=True))
    op.add_column("run_steps", sa.Column("evidence", JSONB, nullable=True))


def downgrade() -> None:
    for col in ("evidence", "compensation_of", "kind", "idempotency_key",
                "depends_on", "wave"):
        op.drop_column("run_steps", col)
    op.drop_column("runs", "task_id")
    op.drop_index("ix_tasks_org", table_name="tasks")
    op.drop_table("tasks")
