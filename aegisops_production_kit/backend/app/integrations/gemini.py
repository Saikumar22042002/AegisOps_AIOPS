"""Real Google Gemini client (google-genai SDK).

Used as the reasoning engine inside the LangGraph agents (streaming + tool-calling) and
as the embedding model for RAG. The model id comes from `GEMINI_MODEL` (default
`gemini-3.5-flash`); `_resolve` falls back to a current flash model the key can list.
No fabricated output — if the API is unreachable the call raises and is surfaced.
"""

from __future__ import annotations

import contextvars
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

import structlog
from google import genai
from google.genai import types
from tenacity import retry, stop_after_attempt, wait_exponential

from ..settings import Settings

log = structlog.get_logger(__name__)

# U3: the per-run model chosen at the API layer (already validated against the provider
# catalog). It is stored in a contextvar rather than mutated onto the shared GeminiLLM
# singleton so that concurrent runs — different asyncio tasks — never clobber each other's
# model. Every GeminiLLM call reads it, so router/cloudops/devops/sre all honor the choice
# without threading a `model` argument through the whole graph.
_run_model: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "aegisops_run_model", default=None)


def set_run_model(model: str | None) -> None:
    """Bind the model for the current run's asyncio context (call once per run driver)."""
    _run_model.set(model)


def get_run_model() -> str | None:
    return _run_model.get()


class GeminiError(Exception):
    pass


def usage_of(resp: Any) -> dict | None:
    """Token usage from a google-genai response/chunk (None-safe)."""
    md = getattr(resp, "usage_metadata", None)
    if md is None:
        return None
    return {"input": getattr(md, "prompt_token_count", None),
            "output": getattr(md, "candidates_token_count", None),
            "total": getattr(md, "total_token_count", None)}


class GeminiLLM:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._enabled = bool(settings.gemini_api_key)
        # Client construction does not call the network; calls do.
        self.client = genai.Client(api_key=settings.gemini_api_key or "missing-key")
        # P18/B6: DO NOT resolve the model here — `models.list()` is a blocking network call and
        # this constructor runs inside async handlers (via the get_gemini singleton). Start with
        # the configured model and resolve lazily, off-thread, on first use.
        self.model = settings.gemini_model
        self._model_resolved = False
        self.embed_model = settings.gemini_embedding_model
        self.embed_dim = settings.gemini_embed_dim

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def _ensure_model(self) -> None:
        """Resolve the model to one the key can actually list, once, without blocking the loop."""
        if self._model_resolved or not self._enabled:
            self._model_resolved = True
            return
        import anyio
        self.model = await anyio.to_thread.run_sync(self._resolve, self.model)
        self._model_resolved = True

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

    def _effective_model(self, model: str | None) -> str:
        """Resolve the model for one call: explicit arg > per-run choice > resolved default."""
        return model or _run_model.get() or self.model

    async def astream(
        self, system: str | None, contents: Any, tools: list | None = None,
        model: str | None = None,
    ) -> AsyncIterator[types.GenerateContentResponse]:
        """Stream raw response chunks (caller reads .text and tool-call parts)."""
        if not self._enabled:
            raise GeminiError("GEMINI_API_KEY is not configured")
        await self._ensure_model()
        use = self._effective_model(model)
        stream = await self.client.aio.models.generate_content_stream(
            model=use, contents=contents, config=self._config(system, tools)
        )
        async for chunk in stream:
            yield chunk

    async def astream_text(
        self, system: str | None, contents: Any, tools: list | None = None,
        model: str | None = None,
    ) -> AsyncIterator[str]:
        async for chunk in self.astream(system, contents, tools, model=model):
            if getattr(chunk, "text", None):
                yield chunk.text

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=0.5, max=6), reraise=True)
    async def agenerate(
        self, system: str | None, contents: Any, tools: list | None = None,
        model: str | None = None,
    ) -> types.GenerateContentResponse:
        if not self._enabled:
            raise GeminiError("GEMINI_API_KEY is not configured")
        await self._ensure_model()
        use = self._effective_model(model)
        # Each attempt is recorded as one Langfuse generation (tokens/cost/latency; failures
        # as ERROR generations) under whichever step span is currently open for the run.
        from .langfuse_client import get_tracer

        t0 = datetime.now(timezone.utc)
        try:
            resp = await self.client.aio.models.generate_content(
                model=use, contents=contents, config=self._config(system, tools)
            )
        except Exception as e:
            get_tracer(self.settings).generation(
                name="gemini.generate", model=use,
                input={"system": system, "prompt": contents if isinstance(contents, str) else str(contents)},
                start_time=t0, error=str(e))
            raise
        get_tracer(self.settings).generation(
            name="gemini.generate", model=use,
            input={"system": system, "prompt": contents if isinstance(contents, str) else str(contents)},
            output=resp.text or "", usage=usage_of(resp), start_time=t0)
        return resp

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
