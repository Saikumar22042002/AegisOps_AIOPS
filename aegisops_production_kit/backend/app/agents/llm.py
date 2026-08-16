"""Compatibility shim over the P1 provider substrate (07 P1.3 — byte-compatible).

The historical Gemini helpers, same signatures, same error semantics (`GeminiError`
raised for callers that catch it), same streaming resilience policy — now routed
through `app/llm` (canonical contracts → router → resilient executor → adapter).
TRANSITIONAL (Redesign/11 T-01): deleted end of P2 once every caller imports
`app.llm.service` directly. New code must NOT import this module.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog

from ..integrations.gemini import GeminiError, get_run_model
from ..metrics import LLM_LATENCY
from ..settings import Settings
from ..llm import service
from ..llm.errors import ModelError
from .events import Emitter

log = structlog.get_logger(__name__)

# The eval runner replays recorded model outputs through this exact parser (rule zero).
_extract_json = service.extract_json


async def classify_json(settings: Settings, system: str, prompt: str, *,
                        purpose: str = "extract",
                        response_schema: dict[str, Any] | None = None) -> dict[str, Any]:
    model_label = get_run_model() or settings.gemini_model  # U3: label = the run's model
    with LLM_LATENCY.labels(model=model_label, operation="classify").time():
        try:
            return await service.classify_json(
                settings, system, prompt, purpose=purpose,
                response_schema=response_schema, requested_model=get_run_model())
        except ModelError as e:
            raise GeminiError(str(e)) from e


# P0/D7: the caller-less `generate()` helper was deleted — every non-streaming call goes
# through `classify_json` or the service directly; dead code invites drift.


_TRUNCATION_NOTE = ("\n\n_(The upstream model stream ended early, so this answer may be "
                    "incomplete — ask me to continue if something is missing.)_")

# Error kinds that are configuration problems, not transient stream failures — parity
# with the historical "except GeminiError: raise" (never retried).
_CONFIG_KINDS = {"auth_permanent", "invalid_request", "content_filtered", "refusal"}


async def stream_answer(settings: Settings, system: str, prompt: str, emitter: Emitter,
                        max_attempts: int = 3, *, purpose: str = "general") -> str:
    """Stream tokens to the client and return the full text — resilient to upstream
    transport truncation (Phase 7 / BUG-03; policy unchanged in P1):

      • nothing emitted yet  → retry transparently (fresh stream, nothing duplicated);
      • tokens already shown → finish cleanly with the partial text + a visible
        truncation note, and emit a retriable `error` event;
      • retries exhausted with nothing to show → raise GeminiError, which every
        streaming agent (general/knowledge/sre) already handles.

    Provider failover BELOW this policy is the executor's job (turn-local, visible
    hops, pre-first-token only) — this loop only sees a stream that works or errors.
    """
    model_label = get_run_model() or settings.gemini_model
    full: list[str] = []
    attempt = 0
    with LLM_LATENCY.labels(model=model_label, operation="stream").time():
        while True:
            attempt += 1
            error_payload: dict | None = None
            try:
                async for ev in service.stream(settings, purpose=purpose, system=system,
                                               prompt=prompt,
                                               requested_model=get_run_model()):
                    if ev.kind == "text_delta":
                        full.append(ev.payload["text"])
                        await emitter.token(ev.payload["text"])
                    elif ev.kind == "served_by":
                        # Additive SSE event (P1.7): honest model badge, incl. fallback hops.
                        await emitter.served_by(ev.payload)
                    elif ev.kind == "error":
                        error_payload = ev.payload
            except ModelError as e:
                # Pre-dispatch refusal (no key, unknown model, budget) — config problem.
                raise GeminiError(str(e)) from e
            if error_payload is None:
                return "".join(full)
            kind = error_payload.get("kind", "unavailable")
            message = error_payload.get("message", kind)
            log.warning("llm.stream_interrupted", error=message, kind=kind,
                        attempt=attempt, emitted_chars=sum(len(c) for c in full))
            if kind in _CONFIG_KINDS:
                raise GeminiError(message)
            if not full and attempt < max_attempts:
                continue  # user saw nothing yet — retry with a fresh stream
            if full:
                await emitter.token(_TRUNCATION_NOTE)
                await emitter.error(f"upstream stream interrupted: {message}",
                                    code="stream_truncated", retriable=True)
                return "".join(full) + _TRUNCATION_NOTE
            raise GeminiError(
                f"model stream failed after {attempt} attempt(s): {message}")
