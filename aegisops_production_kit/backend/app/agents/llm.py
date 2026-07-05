"""Gemini helpers for the agents — JSON classification, generation, and token streaming."""

from __future__ import annotations

import json
import re
from typing import Any

import structlog

from ..integrations.gemini import GeminiError, get_gemini
from ..metrics import LLM_LATENCY
from ..settings import Settings
from .events import Emitter

log = structlog.get_logger(__name__)


def _extract_json(text: str) -> dict[str, Any]:
    """Pull the first JSON object out of a model response (handles ```json fences)."""
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else None
    if not candidate:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        candidate = m.group(0) if m else None
    if not candidate:
        raise ValueError("No JSON object in model response")
    return json.loads(candidate)


async def classify_json(settings: Settings, system: str, prompt: str) -> dict[str, Any]:
    gemini = get_gemini(settings)
    if not gemini.enabled:
        raise GeminiError("GEMINI_API_KEY is not configured")
    with LLM_LATENCY.labels(model=gemini.model, operation="classify").time():
        resp = await gemini.agenerate(system, prompt)
    return _extract_json(resp.text or "")


async def generate(settings: Settings, system: str, prompt: str) -> str:
    gemini = get_gemini(settings)
    if not gemini.enabled:
        raise GeminiError("GEMINI_API_KEY is not configured")
    with LLM_LATENCY.labels(model=gemini.model, operation="generate").time():
        resp = await gemini.agenerate(system, prompt)
    return resp.text or ""


_TRUNCATION_NOTE = ("\n\n_(The upstream model stream ended early, so this answer may be "
                    "incomplete — ask me to continue if something is missing.)_")


async def stream_answer(settings: Settings, system: str, prompt: str, emitter: Emitter,
                        max_attempts: int = 3) -> str:
    """Stream Gemini tokens to the client and return the full text — resilient to upstream
    transport truncation (Phase 7 / BUG-03).

    The google-genai async stream can die mid-response with a transport error (observed live:
    aiohttp `TransferEncodingError: 400, 'Not enough data to satisfy transfer length header'`,
    screenshot 15). Previously that exception escaped the agent, crashed the whole graph run,
    and the run was persisted as an EMPTY "completed" state. Policy now:

      • nothing emitted yet  → retry transparently (fresh stream, nothing duplicated);
      • tokens already shown → finish cleanly with the partial text + a visible truncation
        note, and emit a retriable `error` event (the client can resume via Last-Event-ID);
      • retries exhausted with nothing to show → raise GeminiError, which every streaming
        agent (general/knowledge/sre) already handles without crashing the graph.
    """
    gemini = get_gemini(settings)
    if not gemini.enabled:
        raise GeminiError("GEMINI_API_KEY is not configured")
    full: list[str] = []
    attempt = 0
    with LLM_LATENCY.labels(model=gemini.model, operation="stream").time():
        while True:
            attempt += 1
            try:
                async for chunk in gemini.astream_text(system, prompt):
                    full.append(chunk)
                    await emitter.token(chunk)
                return "".join(full)
            except GeminiError:
                raise  # configuration problem — not transient
            except Exception as e:  # noqa: BLE001 - transport/stream failure mid-response
                log.warning("llm.stream_interrupted", error=str(e), attempt=attempt,
                            emitted_chars=sum(len(c) for c in full))
                if not full and attempt < max_attempts:
                    continue  # user saw nothing yet — retry with a fresh stream
                if full:
                    await emitter.token(_TRUNCATION_NOTE)
                    await emitter.error(f"upstream stream interrupted: {e}",
                                        code="stream_truncated", retriable=True)
                    return "".join(full) + _TRUNCATION_NOTE
                raise GeminiError(f"model stream failed after {attempt} attempt(s): {e}") from e
