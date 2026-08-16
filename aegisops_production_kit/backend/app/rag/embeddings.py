"""Embeddings + chunking for RAG (Gemini embedding model)."""

from __future__ import annotations

import re

from ..llm import service as llm_service
from ..logging_conf import get_logger
from ..settings import Settings

log = get_logger(__name__)


def chunk_text(text: str, max_chars: int = 1200, overlap: int = 150) -> list[str]:
    """Split on paragraph boundaries, packing up to ~max_chars with a small overlap."""
    text = text.strip()
    if not text:
        return []
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    buf = ""
    for p in paras:
        if len(buf) + len(p) + 2 <= max_chars:
            buf = f"{buf}\n\n{p}" if buf else p
        else:
            if buf:
                chunks.append(buf)
            if len(p) <= max_chars:
                buf = p
            else:
                for i in range(0, len(p), max_chars - overlap):
                    chunks.append(p[i : i + max_chars])
                buf = ""
    if buf:
        chunks.append(buf)
    return chunks


async def embed_texts(settings: Settings, texts: list[str]) -> list[list[float]] | None:
    """Return embeddings, or None when the embedding model is unavailable.

    None means "no vectors" — chunks persist with NULL embeddings and retrieval degrades
    to pg_trgm keyword search (the same documented degrade as running with no key at all).
    An unusable key is reported loudly, never silently swallowed into fake vectors."""
    if not llm_service.configured(settings, "embeddings"):
        return None
    try:
        return await llm_service.embed(settings, texts)
    except Exception as exc:  # noqa: BLE001 — degrade to keyword recall, loudly
        log.warning("embeddings.unavailable_degrading_to_keyword", error=str(exc))
        return None
