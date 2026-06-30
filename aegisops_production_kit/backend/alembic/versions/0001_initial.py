"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-06-28
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMBED_DIM = 768


def _uuid_pk() -> sa.Column:
    return sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()"))


def _ts(name: str = "created_at") -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.create_table(
        "organizations",
        _uuid_pk(),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("slug", sa.String(120), nullable=False, unique=True),
        sa.Column("plan", sa.String(40), server_default="enterprise"),
        sa.Column("member_count", sa.Integer(), server_default="0"),
        _ts(),
    )

    op.create_table(
        "roles",
        _uuid_pk(),
        sa.Column("name", sa.String(60), nullable=False, unique=True),
        sa.Column("display_name", sa.String(80), nullable=False),
        sa.Column("description", sa.Text(), server_default=""),
    )

    op.create_table(
        "users",
        _uuid_pk(),
        sa.Column("org_id", sa.Uuid(), sa.ForeignKey("organizations.id", ondelete="CASCADE"), index=True),
        sa.Column("keycloak_sub", sa.String(128), nullable=True),
        sa.Column("username", sa.String(160), nullable=False),
        sa.Column("email", sa.String(200), nullable=True),
        sa.Column("name", sa.String(200), nullable=True),
        sa.Column("roles", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb")),
        _ts(),
        sa.UniqueConstraint("keycloak_sub", name="uq_users_keycloak_sub"),
    )

    op.create_table(
        "sessions",
        _uuid_pk(),
        sa.Column("org_id", sa.Uuid(), sa.ForeignKey("organizations.id", ondelete="CASCADE"), index=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("title", sa.String(300), server_default="New conversation"),
        sa.Column("status", sa.String(20), server_default="active"),
        sa.Column("snow_id", sa.String(60), nullable=True),
        _ts(),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "runs",
        _uuid_pk(),
        sa.Column("org_id", sa.Uuid(), sa.ForeignKey("organizations.id", ondelete="CASCADE"), index=True),
        sa.Column("session_id", sa.Uuid(), sa.ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True),
        sa.Column("intent", sa.String(80), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("routing_reason", sa.Text(), nullable=True),
        sa.Column("domain", sa.String(40), nullable=True),
        sa.Column("workflow", sa.String(80), nullable=True),
        sa.Column("workflow_version", sa.String(40), nullable=True),
        sa.Column("mode", sa.String(20), server_default="plan"),
        sa.Column("status", sa.String(30), server_default="running"),
        sa.Column("plan_json", postgresql.JSONB(), nullable=True),
        sa.Column("input_json", postgresql.JSONB(), nullable=True),
        sa.Column("outcome", postgresql.JSONB(), nullable=True),
        sa.Column("trace_id", sa.String(80), nullable=True),
        sa.Column("context_id", sa.String(80), nullable=True),
        sa.Column("snow_id", sa.String(60), nullable=True),
        _ts(),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "messages",
        _uuid_pk(),
        sa.Column("org_id", sa.Uuid(), sa.ForeignKey("organizations.id", ondelete="CASCADE"), index=True),
        sa.Column("session_id", sa.Uuid(), sa.ForeignKey("sessions.id", ondelete="CASCADE"), index=True),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("content", sa.Text(), server_default=""),
        sa.Column("confidentiality_level", sa.String(20), nullable=True),
        sa.Column("confidentiality_score", sa.Float(), nullable=True),
        sa.Column("trace_id", sa.String(80), nullable=True),
        sa.Column("context_id", sa.String(80), nullable=True),
        sa.Column("snow_id", sa.String(60), nullable=True),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("runs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("analysis", postgresql.JSONB(), nullable=True),
        _ts(),
    )

    op.create_table(
        "feedback",
        _uuid_pk(),
        sa.Column("org_id", sa.Uuid(), sa.ForeignKey("organizations.id", ondelete="CASCADE"), index=True),
        sa.Column("message_id", sa.Uuid(), sa.ForeignKey("messages.id", ondelete="CASCADE"), index=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("value", sa.String(8), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("sensitive", sa.Boolean(), server_default=sa.text("false")),
        _ts(),
    )

    op.create_table(
        "run_steps",
        _uuid_pk(),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("runs.id", ondelete="CASCADE"), index=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("status", sa.String(30), server_default="pending"),
        sa.Column("tool", sa.String(60), nullable=True),
        sa.Column("human_vs_auto", sa.String(10), server_default="auto"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("retries", sa.Integer(), server_default="0"),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("order_index", sa.Integer(), server_default="0"),
    )

    op.create_table(
        "approvals",
        _uuid_pk(),
        sa.Column("org_id", sa.Uuid(), sa.ForeignKey("organizations.id", ondelete="CASCADE"), index=True),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("runs.id", ondelete="CASCADE"), index=True),
        sa.Column("decision", sa.String(20), nullable=False),
        sa.Column("actor_user", sa.String(160), nullable=False),
        sa.Column("actor_role", sa.String(60), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        _ts("ts"),
    )

    op.create_table(
        "documents",
        _uuid_pk(),
        sa.Column("org_id", sa.Uuid(), sa.ForeignKey("organizations.id", ondelete="CASCADE"), index=True),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("source", sa.String(120), nullable=True),
        sa.Column("kind", sa.String(40), server_default="runbook"),
        sa.Column("uri", sa.String(500), nullable=True),
        sa.Column("content", sa.Text(), server_default=""),
        sa.Column("metadata", postgresql.JSONB(), nullable=True),
        _ts(),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )

    op.create_table(
        "document_chunks",
        _uuid_pk(),
        sa.Column("org_id", sa.Uuid(), sa.ForeignKey("organizations.id", ondelete="CASCADE"), index=True),
        sa.Column("document_id", sa.Uuid(), sa.ForeignKey("documents.id", ondelete="CASCADE"), index=True),
        sa.Column("chunk_index", sa.Integer(), server_default="0"),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(EMBED_DIM), nullable=True),
        sa.Column("token_count", sa.Integer(), server_default="0"),
        _ts(),
    )
    # HNSW index for cosine similarity search over embeddings.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_document_chunks_embedding "
        "ON document_chunks USING hnsw (embedding vector_cosine_ops)"
    )

    op.create_table(
        "audit_log",
        _uuid_pk(),
        sa.Column("org_id", sa.Uuid(), sa.ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True),
        sa.Column("actor", sa.String(160), server_default="system"),
        sa.Column("action", sa.String(120), nullable=False),
        sa.Column("target", sa.String(200), nullable=True),
        sa.Column("detail", postgresql.JSONB(), nullable=True),
        sa.Column("correlation", postgresql.JSONB(), nullable=True),
        _ts("ts"),
    )

    op.create_table(
        "integrations",
        _uuid_pk(),
        sa.Column("org_id", sa.Uuid(), sa.ForeignKey("organizations.id", ondelete="CASCADE"), index=True),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("kind", sa.String(60), nullable=False),
        sa.Column("config_ref", sa.String(200), nullable=True),
        sa.Column("status", sa.String(40), server_default="unknown"),
        sa.Column("last_checked", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "notifications",
        _uuid_pk(),
        sa.Column("org_id", sa.Uuid(), sa.ForeignKey("organizations.id", ondelete="CASCADE"), index=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("level", sa.String(20), server_default="info"),
        sa.Column("color", sa.String(40), nullable=True),
        sa.Column("read", sa.Boolean(), server_default=sa.text("false")),
        _ts(),
    )


def downgrade() -> None:
    for table in [
        "notifications",
        "integrations",
        "audit_log",
        "document_chunks",
        "documents",
        "approvals",
        "run_steps",
        "feedback",
        "messages",
        "runs",
        "sessions",
        "users",
        "roles",
        "organizations",
    ]:
        op.drop_table(table)
