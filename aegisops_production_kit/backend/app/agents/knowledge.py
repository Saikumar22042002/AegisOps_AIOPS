"""Knowledge / RAG agent — grounded answers with citations over pgvector. No side effects."""

from __future__ import annotations

import uuid

import structlog

from ..db.session import get_sessionmaker
from ..integrations.gemini import GeminiError
from ..metrics import RAG_LATENCY
from ..rag import retriever
from ..security.confidentiality import classify
from ..settings import get_settings
from . import llm, memory
from .runtime import emitter_of
from .state import AgentState

log = structlog.get_logger(__name__)

_SYSTEM = (
    "You are AegisOps Knowledge. Answer using ONLY the provided context passages and cite them. "
    "If the context is insufficient, say what's missing. Be concise and accurate."
)


async def knowledge(state: AgentState, config) -> dict:
    emitter = emitter_of(config)
    settings = get_settings()
    await emitter.step(3, "Searched knowledge base")

    org_id = uuid.UUID(state["org_id"])
    refs: list = []
    async with get_sessionmaker()() as session:
        with RAG_LATENCY.labels(operation="retrieve").time():
            refs = await retriever.retrieve(session, org_id=org_id, query=state["message"], settings=settings, k=5)

    for r in refs:
        await emitter.reference({"title": r["title"], "source": r.get("source"),
                                 "url": r.get("url"), "relevance": r.get("relevance")})

    await emitter.step(5, "Composed response")
    context_block = "\n\n".join(f"[{i + 1}] {r['title']}\n{r['chunk']}" for i, r in enumerate(refs))
    # M1/M2: Context Engine slice for knowledge follow-ups ("expand on that", "the 2nd doc").
    transcript = await memory.build_context(state.get("session_id", ""), purpose="knowledge",
                                            org_id=state.get("org_id"),
                                            user_id=state.get("user", {}).get("user_id"),
                                            current_message=state["message"], settings=settings,
                                            run_id=state.get("run_id"))
    convo = f"Conversation so far:\n{transcript}\n\n" if transcript else ""
    prompt = f"{convo}Context passages:\n{context_block}\n\nQuestion: {state['message']}"

    try:
        answer = await llm.stream_answer(settings, _SYSTEM, prompt, emitter,
                                         purpose="knowledge")
    except GeminiError as e:
        # Without an LLM key we still return the retrieved citations.
        fallback = "Retrieved relevant documents (LLM answer unavailable until GEMINI_API_KEY is set):\n" + \
                   "\n".join(f"• {r['title']} ({r.get('relevance')})" for r in refs)
        await emitter.token(fallback)
        await emitter.error(str(e), code="llm_unavailable")
        return {"answer": fallback, "references": refs, "confidentiality": {"level": "Low", "score": 0.0}}

    c = classify(answer)
    await emitter.confidentiality(c.level, c.score)
    await emitter.analysis(
        summary=f"Grounded in {len(refs)} retrieved document(s) from the knowledge base.",
        cards=[{"title": "Sources", "conf": "", "body": ", ".join(r["title"] for r in refs) or "none"}],
    )
    return {"answer": answer, "references": refs, "confidentiality": {"level": c.level, "score": c.score}}
