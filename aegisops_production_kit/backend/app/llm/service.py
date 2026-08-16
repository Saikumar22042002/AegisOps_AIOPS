"""LLM service facade (P1.3 — Redesign/04 §4.1, 07 P1.3).

The ONE place application code talks to models. Owns, uniformly for every wire family:
- RoutePlan resolution (router) + resilient dispatch (executor);
- observability: one Langfuse generation per call, named `llm.<purpose>` (provider
  identity lives in metadata/model fields, not the span name);
- accounting truth: one `llm_usage` ledger row per call, success AND error paths,
  with the honest served model vs requested model (P0 ledger contract preserved).

`generate()` and `stream()` are separate entry points — there is no `stream=` flag
(04 §4.1). No reasoning, no tool loops: substrate only.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import structlog

from ..integrations.langfuse_client import get_tracer
from ..integrations.usage_ledger import record_usage
from ..settings import Settings
from . import catalog as catalog_mod
from . import executor, router
from .adapters import for_provider
from .errors import ModelError
from .types import CanonicalMessage, ModelRequest, ModelResponse, StreamEvent, Usage

log = structlog.get_logger(__name__)


def configured(settings: Settings, purpose: str) -> bool:
    """Can this purpose be served right now? (The provider-neutral replacement for the
    historical `get_gemini(settings).enabled` guard.) Checks the purpose's default
    binding; org-level binding overrides are validated at write time (P1.7)."""
    try:
        cat = catalog_mod.load()
        spec = cat.purposes[purpose]
        return cat.provider_configured(cat.models[spec.model].provider, settings)
    except Exception:  # noqa: BLE001 — a guard must never raise
        return False


def _messages(system: str | None, prompt: str) -> list[CanonicalMessage]:
    out: list[CanonicalMessage] = []
    if system:
        out.append(CanonicalMessage(role="system", content=system))
    out.append(CanonicalMessage(role="user", content=prompt))
    return out


def _record(settings: Settings, *, purpose: str, plan, served_model: str | None,
            provider: str, system: str | None, prompt: str, output: str | None,
            usage: Usage | None, t0: datetime, error: str | None = None,
            agent_kind: str = "main") -> None:
    """One Langfuse generation + one authoritative ledger row. Best-effort tracing;
    the ledger call never raises (P0 durability chain)."""
    latency_ms = int((datetime.now(UTC) - t0).total_seconds() * 1000)
    model = served_model or plan.model
    try:
        get_tracer(settings).generation(
            name=f"llm.{purpose}", model=model,
            input={"system": system, "prompt": prompt},
            output=output, usage=usage.as_ledger() if usage else None,
            start_time=t0, error=error)
    except Exception:  # noqa: BLE001 — observability must never break the call
        pass
    record_usage(
        settings, purpose=purpose, model=model, requested_model=plan.model,
        provider=provider, usage=usage.as_ledger() if usage else None,
        latency_ms=latency_ms, agent_kind=agent_kind,
        outcome="ok" if error is None else f"error:{error[:40]}")


async def generate(settings: Settings, *, purpose: str, system: str | None, prompt: str,
                   response_schema: dict[str, Any] | None = None,
                   params: dict[str, Any] | None = None,
                   requested_model: str | None = None,
                   org_id: str | None = None,
                   agent_kind: str = "main") -> ModelResponse:
    plan = await router.resolve(settings, purpose, requested_model=requested_model,
                                org_id=org_id)
    req = ModelRequest(purpose=purpose, messages=_messages(system, prompt),
                       params=params or {}, response_schema=response_schema,
                       metadata={"org_id": org_id} if org_id else {})
    t0 = datetime.now(UTC)
    try:
        resp = await executor.execute(req, plan, settings)
    except ModelError as e:
        _record(settings, purpose=purpose, plan=plan, served_model=None,
                provider=e.provider or plan.provider, system=system, prompt=prompt,
                output=None, usage=None, t0=t0, error=str(e), agent_kind=agent_kind)
        raise
    _record(settings, purpose=purpose, plan=plan, served_model=resp.served_by.model,
            provider=resp.served_by.provider, system=system, prompt=prompt,
            output=resp.content, usage=resp.usage, t0=t0, agent_kind=agent_kind)
    return resp


async def stream(settings: Settings, *, purpose: str, system: str | None, prompt: str,
                 params: dict[str, Any] | None = None,
                 requested_model: str | None = None,
                 org_id: str | None = None,
                 agent_kind: str = "main") -> AsyncIterator[StreamEvent]:
    """Streaming twin of generate(). Emits the adapter/executor StreamEvents unchanged
    and records exactly one ledger row + Langfuse generation at the terminal event."""
    plan = await router.resolve(settings, purpose, requested_model=requested_model,
                                org_id=org_id)
    req = ModelRequest(purpose=purpose, messages=_messages(system, prompt),
                       params=params or {},
                       metadata={"org_id": org_id} if org_id else {})
    t0 = datetime.now(UTC)
    chunks: list[str] = []
    usage: Usage | None = None
    served_model: str | None = None
    served_provider: str | None = None
    try:
        async for ev in executor.execute_stream(req, plan, settings):
            if ev.kind == "text_delta":
                chunks.append(ev.payload["text"])
            elif ev.kind == "usage":
                usage = Usage.model_validate(ev.payload)
            elif ev.kind == "served_by":
                served_model = ev.payload.get("model")
                served_provider = ev.payload.get("provider")
            elif ev.kind == "error":
                _record(settings, purpose=purpose, plan=plan, served_model=served_model,
                        provider=served_provider or plan.provider, system=system,
                        prompt=prompt, output="".join(chunks) or None, usage=usage,
                        t0=t0, error=ev.payload.get("message", ev.payload.get("kind")),
                        agent_kind=agent_kind)
            elif ev.kind == "done":
                _record(settings, purpose=purpose, plan=plan, served_model=served_model,
                        provider=served_provider or plan.provider, system=system,
                        prompt=prompt, output="".join(chunks), usage=usage, t0=t0,
                        agent_kind=agent_kind)
            yield ev
    except GeneratorExit:
        raise
    except ModelError as e:  # pre-dispatch failures (budget gate, no binding)
        _record(settings, purpose=purpose, plan=plan, served_model=None,
                provider=e.provider or plan.provider, system=system, prompt=prompt,
                output=None, usage=None, t0=t0, error=str(e), agent_kind=agent_kind)
        raise


def extract_json(text: str) -> dict[str, Any]:
    """Pull the first JSON object out of a model response (handles ```json fences).
    Byte-identical semantics to the historical `agents/llm._extract_json` — the eval
    runner replays recorded outputs through this exact function (rule zero)."""
    import re
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else None
    if not candidate:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        candidate = m.group(0) if m else None
    if not candidate:
        raise ValueError("No JSON object in model response")
    return json.loads(candidate)


async def classify_json(settings: Settings, system: str, prompt: str, *,
                        purpose: str = "extract",
                        response_schema: dict[str, Any] | None = None,
                        requested_model: str | None = None,
                        org_id: str | None = None) -> dict[str, Any]:
    """JSON-answer call. With a schema, the provider enforces it natively (P1.8);
    the fenced-JSON parser stays as the resilience net either way."""
    resp = await generate(settings, purpose=purpose, system=system, prompt=prompt,
                          response_schema=response_schema,
                          requested_model=requested_model, org_id=org_id)
    text = resp.content or ""
    if response_schema is not None:
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except (TypeError, ValueError):
            pass  # fall through to the tolerant parser
    return extract_json(text)


async def embed(settings: Settings, texts: list[str]) -> list[list[float]]:
    """Embedding call on the `embeddings` purpose. Dimensionality is the catalog's
    pinned value (ADR-02: rebinding this model/dim is a re-embedding migration)."""
    cat = catalog_mod.load()
    plan = await router.resolve(settings, "embeddings")
    info = cat.model(plan.model)
    dim = info.embedding_dim or settings.gemini_embed_dim
    adapter = for_provider(plan.provider, cat, settings)
    t0 = datetime.now(UTC)
    try:
        vectors = await adapter.embed(texts, plan.model, dim)
    except ModelError as e:
        # P0/D3 contract: embedding calls are NEVER invisible — error rows included.
        record_usage(settings, purpose="embedding", model=plan.model,
                     provider=plan.provider,
                     latency_ms=int((datetime.now(UTC) - t0).total_seconds() * 1000),
                     agent_kind="embedding", outcome=f"error:{e.kind}")
        raise
    record_usage(settings, purpose="embedding", model=plan.model, provider=plan.provider,
                 latency_ms=int((datetime.now(UTC) - t0).total_seconds() * 1000),
                 agent_kind="embedding", outcome=f"ok:{len(texts)}_texts")
    return vectors
