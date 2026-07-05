"""General assistant — infra-aware Q&A with Gemini, no side-effecting tools."""

from __future__ import annotations

import structlog

from ..integrations.gemini import GeminiError
from ..security.confidentiality import classify
from ..settings import get_settings
from . import llm
from .runtime import emitter_of
from .state import AgentState

log = structlog.get_logger(__name__)

_SYSTEM = (
    "You are AegisOps, an AI-native CloudOps/DevOps/SRE assistant. Be precise and concise. "
    "You can explain infrastructure, cloud, deployments, and incidents. You never invent "
    "resource state; if you lack data, say so and suggest the action that would fetch it."
)


async def general(state: AgentState, config) -> dict:
    emitter = emitter_of(config)
    settings = get_settings()
    await emitter.step(5, "Composed response")

    if state.get("llm_unavailable"):
        msg = ("The reasoning engine isn't configured yet — set GEMINI_API_KEY in .env and "
               "restart the API to enable live answers.")
        await emitter.token(msg)
        await emitter.error("GEMINI_API_KEY is not configured", code="llm_unavailable", retriable=False)
        return {"answer": msg, "confidentiality": {"level": "Low", "score": 0.0}}

    if state.get("needs_clarification"):
        msg = state.get("clarification", "Could you clarify what you'd like to do?")
        await emitter.token(msg)
        c = classify(msg)
        await emitter.confidentiality(c.level, c.score)
        return {"answer": msg, "confidentiality": {"level": c.level, "score": c.score}}

    try:
        answer = await llm.stream_answer(settings, _SYSTEM, state["message"], emitter)
    except GeminiError as e:
        # Honest failure, clean run: the graph completes with a real message instead of
        # crashing and persisting an empty "completed" state (Phase 7 / BUG-03).
        msg = (f"The reasoning engine couldn't complete a response ({str(e)[:160]}). "
               "Nothing was changed — please send that again.")
        await emitter.token(msg)
        await emitter.error(str(e), code="llm_unavailable", retriable=True)
        return {"answer": msg, "confidentiality": {"level": "Low", "score": 0.0}}

    c = classify(answer)
    await emitter.confidentiality(c.level, c.score)
    await emitter.analysis(
        summary="Answered from the assistant's general knowledge; no infrastructure was modified.",
        cards=[{"title": "Interpreted intent", "conf": f"{int(state.get('intent_confidence', 0) * 100)}%",
                "body": state.get("routing_reason", "General question.")}],
    )
    return {"answer": answer, "confidentiality": {"level": c.level, "score": c.score}}
