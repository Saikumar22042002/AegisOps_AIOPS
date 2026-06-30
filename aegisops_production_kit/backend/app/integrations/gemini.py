"""Real Google Gemini client (google-genai SDK).

Used as the reasoning engine inside the LangGraph agents (streaming + tool-calling) and
as the embedding model for RAG. The model id comes from `GEMINI_MODEL` (default
`gemini-3.5-flash`); `_resolve` falls back to a current flash model the key can list.
No fabricated output — if the API is unreachable the call raises and is surfaced.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import structlog
from google import genai
from google.genai import types
from tenacity import retry, stop_after_attempt, wait_exponential

from ..settings import Settings

log = structlog.get_logger(__name__)


class GeminiError(Exception):
    pass


class GeminiLLM:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._enabled = bool(settings.gemini_api_key)
        # Client construction does not call the network; calls do.
        self.client = genai.Client(api_key=settings.gemini_api_key or "missing-key")
        self.model = self._resolve(settings.gemini_model)
        self.embed_model = settings.gemini_embedding_model
        self.embed_dim = settings.gemini_embed_dim

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _resolve(self, wanted: str) -> str:
        if not self._enabled:
            return wanted
        try:
            ids = {m.name.split("/")[-1] for m in self.client.models.list()}
            if wanted in ids:
                return wanted
            for cand in ("gemini-3.5-flash", "gemini-flash-latest", "gemini-2.5-flash"):
                if cand in ids:
                    log.warning("gemini.model_fallback", wanted=wanted, using=cand)
                    return cand
            return wanted
        except Exception as e:  # noqa: BLE001 - resolution is best-effort; log and keep wanted
            log.warning("gemini.model_list_failed", error=str(e))
            return wanted

    def _config(self, system: str | None, tools: list | None) -> types.GenerateContentConfig:
        return types.GenerateContentConfig(
            system_instruction=system or None,
            tools=tools or None,
        )

    async def astream(
        self, system: str | None, contents: Any, tools: list | None = None
    ) -> AsyncIterator[types.GenerateContentResponse]:
        """Stream raw response chunks (caller reads .text and tool-call parts)."""
        if not self._enabled:
            raise GeminiError("GEMINI_API_KEY is not configured")
        stream = await self.client.aio.models.generate_content_stream(
            model=self.model, contents=contents, config=self._config(system, tools)
        )
        async for chunk in stream:
            yield chunk

    async def astream_text(
        self, system: str | None, contents: Any, tools: list | None = None
    ) -> AsyncIterator[str]:
        async for chunk in self.astream(system, contents, tools):
            if getattr(chunk, "text", None):
                yield chunk.text

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=0.5, max=6), reraise=True)
    async def agenerate(
        self, system: str | None, contents: Any, tools: list | None = None
    ) -> types.GenerateContentResponse:
        if not self._enabled:
            raise GeminiError("GEMINI_API_KEY is not configured")
        return await self.client.aio.models.generate_content(
            model=self.model, contents=contents, config=self._config(system, tools)
        )

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=0.5, max=6), reraise=True)
    async def aembed(self, texts: list[str]) -> list[list[float]]:
        """Return one embedding vector per input text (dimensionality = embed_dim)."""
        if not self._enabled:
            raise GeminiError("GEMINI_API_KEY is not configured")
        resp = await self.client.aio.models.embed_content(
            model=self.embed_model,
            contents=texts,
            config=types.EmbedContentConfig(output_dimensionality=self.embed_dim),
        )
        return [list(e.values) for e in resp.embeddings]


_llm: GeminiLLM | None = None


def get_gemini(settings: Settings) -> GeminiLLM:
    global _llm
    if _llm is None:
        _llm = GeminiLLM(settings)
    return _llm
