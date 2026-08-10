"""Real Google Gemini client (google-genai SDK).

Used as the reasoning engine inside the LangGraph agents (streaming + tool-calling) and
as the embedding model for RAG. The model id comes from `GEMINI_MODEL` (default
`gemini-3.5-flash`); `_resolve` falls back to a current flash model the key can list.
No fabricated output — if the API is unreachable the call raises and is surfaced.
"""

from __future__ import annotations

import contextvars
from collections.abc import AsyncIterator
from datetime import UTC, datetime
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
        # P0/D2: the lazy models.list() resolution was DEAD — prepare_run always resolves a
        # concrete model per run (U3), so `_effective_model` never fell through to a resolved
        # default; the network call still fired and its result was discarded. Deleted. A key
        # that cannot serve the requested model now fails at the call with the provider's own
        # error (and /chat already 400s unknown ids at admission).
        self.model = settings.gemini_model
        self.embed_model = settings.gemini_embedding_model
        self.embed_dim = settings.gemini_embed_dim

    @property
    def enabled(self) -> bool:
        return self._enabled

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
        use = self._effective_model(model)
        stream = await self.client.aio.models.generate_content_stream(
            model=use, contents=contents, config=self._config(system, tools)
        )
        async for chunk in stream:
            yield chunk

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=0.5, max=6), reraise=True)
    async def agenerate(
        self, system: str | None, contents: Any, tools: list | None = None,
        model: str | None = None, op: str = "generate",
    ) -> types.GenerateContentResponse:
        if not self._enabled:
            raise GeminiError("GEMINI_API_KEY is not configured")
        use = self._effective_model(model)
        # Each attempt is recorded as one Langfuse generation (observability) AND one
        # authoritative ledger row (accounting truth — P0; `op` is a coarse call-site
        # label, NOT purpose routing, which is P1).
        from . import usage_ledger
        from .langfuse_client import get_tracer

        t0 = datetime.now(UTC)
        try:
            resp = await self.client.aio.models.generate_content(
                model=use, contents=contents, config=self._config(system, tools)
            )
        except Exception as e:
            get_tracer(self.settings).generation(
                name="gemini.generate", model=use,
                input={"system": system, "prompt": contents if isinstance(contents, str) else str(contents)},
                start_time=t0, error=str(e))
            usage_ledger.record_usage(
                self.settings, purpose=op, model=use,
                latency_ms=int((datetime.now(UTC) - t0).total_seconds() * 1000),
                outcome=f"error:{type(e).__name__}")
            raise
        get_tracer(self.settings).generation(
            name="gemini.generate", model=use,
            input={"system": system, "prompt": contents if isinstance(contents, str) else str(contents)},
            output=resp.text or "", usage=usage_of(resp), start_time=t0)
        usage_ledger.record_usage(
            self.settings, purpose=op, model=use, usage=usage_of(resp),
            latency_ms=int((datetime.now(UTC) - t0).total_seconds() * 1000))
        return resp

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=0.5, max=6), reraise=True)
    async def aembed(self, texts: list[str]) -> list[list[float]]:
        """Return one embedding vector per input text (dimensionality = embed_dim)."""
        if not self._enabled:
            raise GeminiError("GEMINI_API_KEY is not configured")
        from . import usage_ledger

        t0 = datetime.now(UTC)
        resp = await self.client.aio.models.embed_content(
            model=self.embed_model,
            contents=texts,
            config=types.EmbedContentConfig(output_dimensionality=self.embed_dim),
        )
        # P0/D3: embedding calls were invisible in EVERY sink. The embed API does not
        # report token usage reliably, so tokens may honestly be 0 — the call itself
        # (count, model, latency) is now on the ledger.
        usage_ledger.record_usage(
            self.settings, purpose="embedding", model=self.embed_model,
            usage=getattr(resp, "usage_metadata", None) and {
                "total": getattr(resp.usage_metadata, "total_token_count", 0)} or None,
            latency_ms=int((datetime.now(UTC) - t0).total_seconds() * 1000),
            agent_kind="embedding", outcome=f"ok:{len(texts)}_texts")
        return [list(e.values) for e in resp.embeddings]


_llm: GeminiLLM | None = None


def get_gemini(settings: Settings) -> GeminiLLM:
    global _llm
    if _llm is None:
        _llm = GeminiLLM(settings)
    return _llm
