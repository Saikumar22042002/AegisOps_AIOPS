"""Embeddings + chunking for RAG (Gemini embedding model)."""

from __future__ import annotations

import re

from ..integrations.gemini import get_gemini
from ..settings import Settings


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
    """Return embeddings, or None if no embedding model is configured."""
    gemini = get_gemini(settings)
    if not gemini.enabled:
        return None
    return await gemini.aembed(texts)
