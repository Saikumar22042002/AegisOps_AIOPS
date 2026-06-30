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


async def stream_answer(settings: Settings, system: str, prompt: str, emitter: Emitter) -> str:
    """Stream Gemini tokens to the client and return the full text."""
    gemini = get_gemini(settings)
    if not gemini.enabled:
        raise GeminiError("GEMINI_API_KEY is not configured")
    full: list[str] = []
    with LLM_LATENCY.labels(model=gemini.model, operation="stream").time():
        async for chunk in gemini.astream_text(system, prompt):
            full.append(chunk)
            await emitter.token(chunk)
    return "".join(full)
