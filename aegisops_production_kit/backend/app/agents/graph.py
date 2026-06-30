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

log = structlog.get_logger(__name__)

_graph = None


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
    if state.get("needs_change") and state.get("approval_status") == "pending":
        return "approval"
    return "finalize"


def build_graph(checkpointer):
    g = StateGraph(AgentState)

    g.add_node("router", router)
    g.add_node("cloudops_plan", cloudops_plan)
    g.add_node("devops_plan", devops_plan)
    g.add_node("sre_analyze", sre_analyze)
    g.add_node("knowledge", knowledge)
    g.add_node("general", general)
    g.add_node("approval", approval)
    g.add_node("execute", execute)
    g.add_node("verify", verify)
    g.add_node("finalize", finalize)
    g.add_node("servicenow_update", servicenow_update)
    g.add_node("notify", notify)

    g.add_edge(START, "router")
    g.add_conditional_edges("router", _after_router,
                            ["cloudops_plan", "devops_plan", "sre_analyze", "knowledge", "general"])
    for plan_node in ("cloudops_plan", "devops_plan", "sre_analyze"):
        g.add_conditional_edges(plan_node, _after_plan, ["approval", "finalize"])
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
