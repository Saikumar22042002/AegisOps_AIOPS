"""GW-1: messaging-gateway channel identities + one-time link codes + source tagging

Revision ID: 0009_channel_identities
Revises: 0008_user_memory
Create Date: 2026-07-29

Identity for a messaging channel (Telegram first) is a BINDING between a channel account and a
platform user — never a chat-id allowlist. Two tables:

* `channel_identities` — the binding itself, org-scoped. Unique both ways: one channel account
  maps to at most one platform user, and one platform user holds at most one account per
  channel. `active_session_id` gives the channel "one chat = one session per user".
* `channel_link_codes` — one-time, expiring codes. Only the SHA-256 hash is stored, because the
  code is a bearer secret the user types into a third-party chat app.

Also adds `source` to `sessions` and `runs` so channel provenance is a first-class governance
fact ("was this Production change started from a phone?"). Existing rows backfill to 'web',
which is what they were.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0009_channel_identities"
down_revision: str | None = "0008_user_memory"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "channel_identities",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("org_id", UUID(as_uuid=True),
                  sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False,
                  index=True),
        sa.Column("user_id", UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("channel", sa.String(24), nullable=False),
        sa.Column("channel_user_id", sa.String(64), nullable=False),
        sa.Column("channel_chat_id", sa.String(64), nullable=False),
        sa.Column("channel_username", sa.String(160), nullable=True),
        sa.Column("active_session_id", UUID(as_uuid=True),
                  sa.ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True),
        sa.Column("linked_by", sa.String(160), nullable=True),
        sa.Column("linked_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("channel", "channel_user_id", name="uq_channel_identity_account"),
        sa.UniqueConstraint("channel", "user_id", name="uq_channel_identity_user"),
    )
    op.create_table(
        "channel_link_codes",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("org_id", UUID(as_uuid=True),
                  sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False,
                  index=True),
        sa.Column("user_id", UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("channel", sa.String(24), nullable=False),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("used_by_channel_user_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("code_hash", name="uq_channel_link_code_hash"),
    )
    # The hot lookup on inbound "/link <code>": hash → live (unused, unexpired) row.
    op.create_index("ix_channel_link_codes_live", "channel_link_codes",
                    ["channel", "code_hash"])

    # Channel provenance. server_default backfills existing rows to 'web' (what they were);
    # the default stays so a plain INSERT without `source` is still honest.
    op.add_column("sessions", sa.Column("source", sa.String(20), nullable=False,
                                        server_default="web"))
    op.add_column("runs", sa.Column("source", sa.String(20), nullable=False,
                                    server_default="web"))


def downgrade() -> None:
    op.drop_column("runs", "source")
    op.drop_column("sessions", "source")
    op.drop_index("ix_channel_link_codes_live", table_name="channel_link_codes")
    op.drop_table("channel_link_codes")
    op.drop_table("channel_identities")
