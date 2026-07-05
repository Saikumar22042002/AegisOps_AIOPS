"""Human-in-the-loop approval gate via LangGraph interrupt.

The graph pauses here (durable checkpoint) until POST /approvals/{runId} resumes it with the
decision + approver. Records the decision immutably (Approvals table + context graph). RBAC is
enforced at the /approvals endpoint (only approver roles may resolve).
"""

from __future__ import annotations

import uuid

import structlog
from langgraph.types import interrupt

from ..db.models import Approval
from ..db.session import get_sessionmaker
from ..graph_db.context_graph import ContextGraph
from . import timing
from .runtime import emitter_of
from .state import AgentState

log = structlog.get_logger(__name__)


async def approval(state: AgentState, config) -> dict:
    emitter = emitter_of(config)
    if not state.get("needs_change") or state.get("approval_status") == "not_required":
        return {"approval_status": "not_required"}

    payload = state.get("interrupt_payload") or {"kind": "approval", "runId": state["run_id"]}
    # Record the approval start now; end after the human decides. start_step preserves the first
    # start across the resume re-entry, so the recorded duration is the real human-wait time.
    await timing.start_step(state.get("run_id"), "approval", human_vs_auto="human")
    # Pause the graph; the value returned is whatever POST /approvals/{runId} resumes with.
    decision = interrupt(payload)
    await timing.end_step(state.get("run_id"), "approval", status="done")

    if isinstance(decision, dict):
        status = decision.get("decision", "rejected")
        approver = {"user": decision.get("user", "unknown"), "role": decision.get("role", "unknown"),
                    "ts": decision.get("ts"), "rationale": decision.get("rationale")}
    else:
        status, approver = str(decision), {}

    await emitter.step(4, "Approved by you" if status == "approved" else "Rejected — halted")

    # Immutable record: Approvals table + context graph.
    try:
        async with get_sessionmaker()() as session:
            session.add(Approval(
                org_id=uuid.UUID(state["org_id"]),
                run_id=uuid.UUID(state["run_id"]),
                decision=status,
                actor_user=approver.get("user", "unknown"),
                actor_role=approver.get("role", "unknown"),
                rationale=approver.get("rationale"),
            ))
            await session.commit()
    except Exception as e:  # noqa: BLE001
        log.warning("approval.record_failed", error=str(e))
    try:
        cg = ContextGraph(state.get("context_id") or state["run_id"], state.get("org_id", ""))
        await cg.add_approval(decision=status, actor=approver.get("user", "unknown"),
                              role=approver.get("role", "unknown"), rationale=approver.get("rationale"))
    except Exception as e:  # noqa: BLE001
        log.warning("approval.cg_failed", error=str(e))

    return {"approval_status": status, "approver": approver}


def approval_decision(state: AgentState) -> str:
    return "execute" if state.get("approval_status") == "approved" else "finalize"
