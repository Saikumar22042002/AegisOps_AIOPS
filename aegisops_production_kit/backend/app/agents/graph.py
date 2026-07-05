"""Assemble the LangGraph multi-agent graph.

START → router → {cloudops_plan | devops_plan | sre_analyze | knowledge | general}
  action plans → approval[INTERRUPT] → execute → verify → finalize
  read-only/clarify → finalize
finalize → servicenow_update → notify → END

Durable checkpoint after every node (Postgres checkpointer); dynamic interrupt at the approval
gate; resume from checkpoint on approve/reject or after restart.
"""

from __future__ import annotations

import structlog
from langgraph.graph import END, START, StateGraph

from .approval import approval, approval_decision
from .cloudops import cloudops_plan
from .devops import devops_plan
from .execute import execute
from .finalize import finalize, verify
from .general import general
from .knowledge import knowledge
from .notify import notify
from .router import router
from .servicenow_agent import servicenow_update
from .sre import sre_analyze
from .state import AgentState
from . import timing

log = structlog.get_logger(__name__)

_graph = None


def _timed(name: str, fn):
    """Wrap a graph node to record real start/end timings into run_steps.

    Not used for `approval` (it self-times across the human-in-the-loop interrupt) or
    `cloudops_plan` (it records finer sub-steps: cloudops_agent, policy_evaluation, planner).
    None of the wrapped nodes raise LangGraph's interrupt, so a plain try/except is safe.
    """

    async def wrapper(state: AgentState, config):
        run_id = state.get("run_id")
        await timing.start_step(run_id, name)
        try:
            result = await fn(state, config)
        except Exception as e:  # noqa: BLE001 - record the failure timing, then propagate
            await timing.end_step(run_id, name, status="failed", error=str(e))
            raise
        await timing.end_step(run_id, name, status="done")
        return result

    return wrapper


def _after_router(state: AgentState) -> str:
    if state.get("needs_clarification"):
        return "general"
    return {
        "cloudops": "cloudops_plan",
        "devops": "devops_plan",
        "sre": "sre_analyze",
        "knowledge": "knowledge",
        "general": "general",
    }.get(state.get("domain", "general"), "general")


def _after_plan(state: AgentState) -> str:
    # A plan node that needs the user to clarify (ambiguous cloud, no approved module, invalid
    # inputs) routes to `general`, which streams the clarification message to the user.
    if state.get("needs_clarification"):
        return "general"
    if state.get("needs_change") and state.get("approval_status") == "pending":
        return "approval"
    return "finalize"


def build_graph(checkpointer):
    g = StateGraph(AgentState)

    g.add_node("router", _timed("router", router))
    g.add_node("cloudops_plan", cloudops_plan)  # self-records cloudops_agent/policy_evaluation/planner
    g.add_node("devops_plan", _timed("devops_plan", devops_plan))
    g.add_node("sre_analyze", _timed("sre_analyze", sre_analyze))
    g.add_node("knowledge", _timed("knowledge", knowledge))
    g.add_node("general", _timed("general", general))
    g.add_node("approval", approval)  # self-times across the human-in-the-loop interrupt
    g.add_node("execute", _timed("execute", execute))
    g.add_node("verify", _timed("verify", verify))
    g.add_node("finalize", _timed("finalize", finalize))
    g.add_node("servicenow_update", _timed("servicenow_update", servicenow_update))
    g.add_node("notify", _timed("notify", notify))

    g.add_edge(START, "router")
    g.add_conditional_edges("router", _after_router,
                            ["cloudops_plan", "devops_plan", "sre_analyze", "knowledge", "general"])
    for plan_node in ("cloudops_plan", "devops_plan", "sre_analyze"):
        g.add_conditional_edges(plan_node, _after_plan, ["approval", "finalize", "general"])
    g.add_edge("knowledge", "finalize")
    g.add_edge("general", "finalize")
    g.add_conditional_edges("approval", approval_decision, ["execute", "finalize"])
    g.add_edge("execute", "verify")
    g.add_edge("verify", "finalize")
    g.add_edge("finalize", "servicenow_update")
    g.add_edge("servicenow_update", "notify")
    g.add_edge("notify", END)

    return g.compile(checkpointer=checkpointer)


def init_graph(checkpointer):
    global _graph
    _graph = build_graph(checkpointer)
    log.info("graph.compiled")
    return _graph


def get_graph():
    if _graph is None:
        raise RuntimeError("Graph not built; call init_graph() at startup.")
    return _graph
