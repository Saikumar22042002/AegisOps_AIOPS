"""General assistant — infra-aware Q&A with Gemini, no side-effecting tools."""

from __future__ import annotations

import structlog

from ..integrations.gemini import GeminiError
from ..security.confidentiality import classify
from ..settings import get_settings
from . import llm, memory
from .runtime import emitter_of
from .state import AgentState

log = structlog.get_logger(__name__)

_SYSTEM = (
    "You are AegisOps, an AI-native CloudOps/DevOps/SRE assistant. Be precise and concise. "
    "You can explain infrastructure, cloud, deployments, and incidents. You never invent "
    "resource state; if you lack data, say so and suggest the action that would fetch it. "
    "When a conversation transcript is provided, it is the REAL history of this session — "
    "use it to answer questions like “what did I ask earlier?” accurately, and never claim "
    "the conversation has no history when a transcript is present."
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

    # M2: exact positional recall is answered DETERMINISTICALLY from the store — no LLM guess,
    # no truncation. "What was my 20th question?" returns turn 20 verbatim even if the LLM is
    # down, and can never hallucinate a different turn.
    rec = memory.detect_recall(state["message"])
    if rec:
        turn = await memory.get_turn(state.get("session_id", ""), rec[0], role=rec[1])
        if turn:
            answer = (f"Your {memory._ordinal_label(turn['ordinal'])} {rec[1]} in this "
                      f"conversation was:\n\n> {turn['content']}")
            await emitter.token(answer)
            c = classify(answer)
            await emitter.confidentiality(c.level, c.score)
            return {"answer": answer, "confidentiality": {"level": c.level, "score": c.score}}

    # M1/M2: the full Context Engine slice — transcript + a verbatim positional-recall slot
    # ("what was my 20th question?") + semantic/keyword retrieval of relevant earlier turns.
    transcript = await memory.build_context(state.get("session_id", ""), purpose="general",
                                            current_message=state["message"], settings=get_settings())
    prompt = (f"Conversation so far in this session:\n{transcript}\n\n"
              f"User's current message: {state['message']}") if transcript else state["message"]

    try:
        answer = await llm.stream_answer(settings, _SYSTEM, prompt, emitter)
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
