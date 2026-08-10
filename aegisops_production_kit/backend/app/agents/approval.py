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
from . import plan_guard, timing
from .runtime import emitter_of
from .state import AgentState

log = structlog.get_logger(__name__)


def _guard_action(state: AgentState) -> str:
    """The action to re-assert the plan against at the choke-point. `execution_mode=="destroy"`
    is a destroy; every other mutating mode is an apply, for which the create rule (no
    delete/replace) is the correct, strict invariant — create and modify both forbid tearing
    anything down, so asserting it here catches a bad plan regardless of which path produced it."""
    if state.get("action"):
        return str(state["action"]).lower()
    return "destroy" if state.get("execution_mode") == "destroy" else "create"


async def approval(state: AgentState, config) -> dict:
    emitter = emitter_of(config)
    if not state.get("needs_change") or state.get("approval_status") == "not_required":
        return {"approval_status": "not_required"}

    # A2: re-assert plan_guard at the approval choke-point — the last gate before a human sees
    # the plan. Even if a plan path forgot to call the guard, a plan whose actions don't match
    # the operation (an apply that would delete/replace, a destroy that would create) is halted
    # HERE, before the durable interrupt — never shown to an approver, never applied.
    violation = plan_guard.check_plan_actions(_guard_action(state), state.get("diff") or [])
    if violation:
        log.error("approval.plan_guard_blocked", run_id=state.get("run_id"),
                  action=_guard_action(state))
        await emitter.error(violation, code="plan_guard", retriable=False)
        await emitter.token(violation)
        return {"approval_status": "blocked", "needs_change": False, "answer": violation,
                "outcome": {"status": "blocked", "error": "plan_guard: plan/action mismatch"}}

    payload = state.get("interrupt_payload") or {"kind": "approval", "runId": state["run_id"]}
    # P0.5 (D9/F-9): every approval card carries the active governance posture. Additive
    # field only — no existing payload key changes, no interrupt semantics change.
    from ..security.governance_stamp import stamped
    payload = stamped(payload)
    # Record the approval start now; end after the human decides. start_step preserves the first
    # start across the resume re-entry, so the recorded duration is the real human-wait time.
    await timing.start_step(state.get("run_id"), "approval", human_vs_auto="human")
    # Pause the graph; the value returned is whatever POST /approvals/{runId} resumes with.
    decision = interrupt(payload)
    await timing.end_step(state.get("run_id"), "approval", status="done")

    if isinstance(decision, dict):
        status = decision.get("decision", "rejected")
        approver = {"user": decision.get("user", "unknown"), "role": decision.get("role", "unknown"),
                    "ts": decision.get("ts"), "rationale": decision.get("rationale"),
                    "can_execute": bool(decision.get("can_execute"))}  # S5
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
