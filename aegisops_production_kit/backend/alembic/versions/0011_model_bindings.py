"""P1.7: org-level model bindings (Redesign/04 §4.4, 06 §8.2, 07 P1.7).

`models.yaml` = what CAN run; this table = who runs what. PK(org_id, purpose) —
one binding per purpose per org. `eval_state` is the promotion control
(pending | passed | failed | waived); a `failed` binding never routes.

Revision ID: 0011_model_bindings
Revises: 0010_llm_usage
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0011_model_bindings"
down_revision = "0010_llm_usage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "model_bindings",
        sa.Column("org_id", UUID(as_uuid=True),
                  sa.ForeignKey("organizations.id", ondelete="CASCADE"),
                  primary_key=True, nullable=False),
        sa.Column("purpose", sa.String(32), primary_key=True, nullable=False),
        sa.Column("model", sa.String(120), nullable=False),
        sa.Column("eval_state", sa.String(12), nullable=False,
                  server_default="pending"),
        sa.Column("updated_by", sa.String(120), nullable=True),
        sa.Column("reason", sa.String(500), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.CheckConstraint(
            "eval_state IN ('pending', 'passed', 'failed', 'waived')",
            name="ck_model_bindings_eval_state"),
    )


def downgrade() -> None:
    op.drop_table("model_bindings")
