"""message embeddings for semantic conversational recall (M2)

Revision ID: 0006_message_embeddings
Revises: 0005_hot_path_indexes
Create Date: 2026-07-12

Adds a nullable pgvector column to messages so each message can be embedded on write and
retrieved by semantic similarity. Nullable: a no-Gemini setup leaves it NULL and recall
degrades to pg_trgm keyword search. An HNSW cosine index backs the top-k retrieval.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = "0006_message_embeddings"
down_revision: str | None = "0005_hot_path_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DIM = 768


def upgrade() -> None:
    op.add_column("messages", sa.Column("embedding", Vector(_DIM), nullable=True))
    # HNSW cosine index (matches document_chunks); created only where a vector exists.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_messages_embedding_hnsw "
        "ON messages USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_messages_embedding_hnsw")
    op.drop_column("messages", "embedding")
