"""Drive the compiled graph for a run, with per-run Langfuse trace + OTel span.

Reports whether the graph paused at an interrupt. One Langfuse trace + one OTel span per run,
linked to the context-graph id; per-node records are written to the context graph and emitted
as SSE step events.
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


async def run_graph(run_id: str, channel: RunChannel, *, initial: dict | None = None, resume=None) -> dict:
    graph = get_graph()
    settings = get_settings()
    emitter = Emitter(channel)
    config = {"configurable": {"thread_id": run_id, "emitter": emitter}}

    state_src = initial or {}
    lf = get_tracer(settings)
    trace = lf.trace(
        name="aegisops.run",
        context_id=state_src.get("context_id", run_id),
        user_id=state_src.get("user", {}).get("username"),
        metadata={"run_id": run_id, "domain": state_src.get("domain"), "resume": resume is not None},
        input={"message": state_src.get("message")} if initial else {"resume": True},
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
        trace.update(output={"status": "interrupted" if interrupted else "completed",
                             "outcome": (state or {}).get("outcome")})
        return {"interrupted": interrupted, "state": state or {}}
    except Exception as e:  # noqa: BLE001 - surface any graph failure as an SSE error
        log.exception("runner.graph_failed", run_id=run_id)
        trace.update(output={"status": "error", "error": str(e)})
        await emitter.error(f"Agent run failed: {e}", code="graph_error", retriable=False)
        return {"interrupted": False, "state": {}, "error": str(e)}
    finally:
        lf.flush()
