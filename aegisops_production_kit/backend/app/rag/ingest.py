"""Ingestion pipeline — store + embed documents (runbooks/RCAs/design-docs/summaries)."""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from ..settings import Settings
from . import store

log = structlog.get_logger(__name__)


async def ingest_document(session: AsyncSession, *, org_id: uuid.UUID, settings: Settings, **doc: Any):
    document = await store.add_document(session, org_id=org_id, settings=settings, **doc)
    log.info("rag.ingested", title=doc.get("title"), doc_id=str(document.id))
    return document


async def ingest_many(session: AsyncSession, *, org_id: uuid.UUID, settings: Settings, docs: list[dict[str, Any]]):
    created = []
    for d in docs:
        created.append(await ingest_document(session, org_id=org_id, settings=settings, **d))
    return created
