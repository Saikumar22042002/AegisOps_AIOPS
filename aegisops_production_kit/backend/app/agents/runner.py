"""Drive the compiled graph for a run, with per-run Langfuse trace + OTel span.

Reports whether the graph paused at an interrupt. One Langfuse trace per RUN ID — the
approval resume re-attaches to the same trace, so a run that waits on a human still reads
as one end-to-end trace. Node/sub-step spans are opened by `agents.timing`, LLM generations
by the Gemini client, and tool spans by the tool layers; this module owns the trace itself:
identity, tags (session/user/agent/cloud/env/intent), context-graph link, and the final
status/output. The trace is flushed before the async task ends so short-lived handlers
never drop it.
"""

from __future__ import annotations

import structlog
from langgraph.types import Command

from ..integrations.langfuse_client import get_tracer
from ..otel import get_tracer as get_otel_tracer
from ..settings import get_settings
from .events import Emitter, RunChannel
from .graph import get_graph

log = structlog.get_logger(__name__)


def _tags(state: dict) -> list[str]:
    tags = []
    user = state.get("user") or {}
    for key, value in (("agent", state.get("domain")), ("cloud", user.get("cloud")),
                       ("env", user.get("env")), ("intent", state.get("intent"))):
        if value:
            tags.append(f"{key}:{str(value).lower()}")
    return tags


async def run_graph(run_id: str, channel: RunChannel, *, initial: dict | None = None, resume=None) -> dict:
    graph = get_graph()
    settings = get_settings()
    emitter = Emitter(channel)
    config = {"configurable": {"thread_id": run_id, "emitter": emitter}}

    state_src = initial or {}
    user = state_src.get("user") or {}
    lf = get_tracer(settings)
    lf.begin_run(
        run_id,
        "chat-request" if initial else None,  # resume: keep the original name/tags
        user_id=user.get("username"),
        session_id=state_src.get("session_id"),
        metadata={"run_id": run_id, "session_id": state_src.get("session_id"),
                  "context_id": state_src.get("context_id", run_id),
                  "cloud": user.get("cloud"), "environment": user.get("env"),
                  "region": user.get("region"), "resume": resume is not None},
        input={"message": state_src.get("message")} if initial else {"resume": resume},
        tags=_tags(state_src) if initial else None,
    )
    otel = get_otel_tracer("aegisops.agents")

    try:
        with otel.start_as_current_span("agent.run") as span:
            span.set_attribute("aegisops.run_id", run_id)
            if resume is not None:
                state = await graph.ainvoke(Command(resume=resume), config)
            else:
                state = await graph.ainvoke(initial, config)
            snapshot = await graph.aget_state(config)
            interrupted = bool(snapshot.next)
            span.set_attribute("aegisops.interrupted", interrupted)
            span.set_attribute("aegisops.domain", str(state.get("domain") if state else ""))
        final = state or {}
        status = "interrupted" if interrupted else "completed"
        domain = final.get("domain")
        final_user = final.get("user") or user
        lf.end_run(
            run_id,
            name=f"{domain}-run" if domain else None,
            user_id=final_user.get("username"),
            session_id=final.get("session_id") or state_src.get("session_id"),
            output={"status": status, "outcome": final.get("outcome"),
                    "answer": (final.get("answer") or "")[:2000] or None},
            metadata={"run_id": run_id, "context_id": final.get("context_id", run_id),
                      "domain": domain, "intent": final.get("intent"),
                      "workflow": final.get("workflow"), "cloud": final_user.get("cloud"),
                      "environment": final_user.get("env"), "interrupted": interrupted},
            tags=_tags({**final, "user": final_user}),
        )
        return {"interrupted": interrupted, "state": final}
    except Exception as e:  # noqa: BLE001 - surface any graph failure as an SSE error
        log.exception("runner.graph_failed", run_id=run_id)
        lf.end_run(run_id, output={"status": "error", "error": str(e)})
        await emitter.error(f"Agent run failed: {e}", code="graph_error", retriable=False)
        return {"interrupted": False, "state": {}, "error": str(e)}
    finally:
        lf.flush()
