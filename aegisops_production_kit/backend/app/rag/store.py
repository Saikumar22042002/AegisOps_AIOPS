"""pgvector-backed document store."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Document, DocumentChunk
from ..settings import Settings
from .embeddings import chunk_text, embed_texts


async def add_document(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    title: str,
    content: str,
    source: str | None = None,
    kind: str = "runbook",
    uri: str | None = None,
    metadata: dict | None = None,
    settings: Settings,
) -> Document:
    """Insert a Document + its embedded chunks. Embeddings are null if no model is configured."""
    doc = Document(org_id=org_id, title=title, source=source, kind=kind, uri=uri,
                   content=content, doc_metadata=metadata)
    session.add(doc)
    await session.flush()

    chunks = chunk_text(content) or [content]
    vectors = await embed_texts(settings, chunks)
    for i, ch in enumerate(chunks):
        session.add(
            DocumentChunk(
                org_id=org_id,
                document_id=doc.id,
                chunk_index=i,
                content=ch,
                embedding=vectors[i] if vectors else None,
                token_count=len(ch.split()),
            )
        )
    await session.flush()
    return doc


async def semantic_search(
    session: AsyncSession, *, org_id: uuid.UUID, query_vector: list[float], k: int = 5
) -> list[dict[str, Any]]:
    distance = DocumentChunk.embedding.cosine_distance(query_vector).label("distance")
    stmt = (
        select(DocumentChunk, Document, distance)
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(DocumentChunk.org_id == org_id, DocumentChunk.embedding.isnot(None))
        .order_by(distance)
        .limit(k)
    )
    rows = (await session.execute(stmt)).all()
    return [
        {
            "chunk": chunk.content,
            "title": doc.title,
            "source": doc.source,
            "kind": doc.kind,
            "url": doc.uri,
            "relevance": round(max(0.0, 1.0 - float(dist)), 4),
        }
        for chunk, doc, dist in rows
    ]


async def keyword_search(
    session: AsyncSession, *, org_id: uuid.UUID, query: str, k: int = 5
) -> list[dict[str, Any]]:
    """Fallback search (trigram similarity) when embeddings are unavailable."""
    from sqlalchemy import func

    sim = func.similarity(DocumentChunk.content, query).label("sim")
    stmt = (
        select(DocumentChunk, Document, sim)
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(DocumentChunk.org_id == org_id)
        .order_by(sim.desc())
        .limit(k)
    )
    rows = (await session.execute(stmt)).all()
    return [
        {
            "chunk": chunk.content,
            "title": doc.title,
            "source": doc.source,
            "kind": doc.kind,
            "url": doc.uri,
            "relevance": round(float(s), 4),
        }
        for chunk, doc, s in rows
        if float(s) > 0
    ]
