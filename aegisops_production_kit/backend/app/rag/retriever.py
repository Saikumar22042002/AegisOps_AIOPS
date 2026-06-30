"""RAG retriever — embeds the query and returns cited references for the Analysis/References UI.

Falls back to trigram keyword search when no embedding model is configured, so retrieval
still returns grounded citations before a Gemini key is added.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog

from ..integrations.gemini import get_gemini
from ..settings import Settings
from . import store

log = structlog.get_logger(__name__)


async def retrieve(session, *, org_id: uuid.UUID, query: str, settings: Settings, k: int = 5) -> list[dict[str, Any]]:
    gemini = get_gemini(settings)
    if gemini.enabled:
        try:
            vectors = await gemini.aembed([query])
            results = await store.semantic_search(session, org_id=org_id, query_vector=vectors[0], k=k)
            if results:
                return results
        except Exception as e:  # noqa: BLE001 - degrade to keyword search, never fabricate
            log.warning("rag.semantic_failed", error=str(e))
    return await store.keyword_search(session, org_id=org_id, query=query, k=k)
