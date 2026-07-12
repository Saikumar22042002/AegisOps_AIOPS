"""Shared typed state for the LangGraph multi-agent graph (01_REQUIREMENTS / 05_AGENTS §1)."""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypedDict

from langgraph.graph.message import add_messages


class AgentState(TypedDict, total=False):
    # identity / correlation
    org_id: str
    user: dict[str, Any]          # {sub, username, roles, can_approve, ...}
    session_id: str
    run_id: str
    context_id: str
    trace_id: str

    # conversation
    message: str                  # the current user message
    messages: Annotated[list, add_messages]  # full thread (LangChain messages)

    # routing
    intent: str
    intent_confidence: float
    routing_reason: str
    domain: Literal["cloudops", "devops", "sre", "knowledge", "general"]

    # workflow
    workflow: str
    workflow_version: str
    cloud: str
    resource: str
    action: Literal["create", "modify", "destroy", "read"]
    target: str                   # reference to an EXISTING resource for day-2 ops (name/context)
    state_workspace: str          # per-resource Terraform state workspace (N-08 isolation)
    raw_inputs: str
    parsed_inputs: dict[str, Any]
    validation_errors: list
    needs_clarification: bool
    clarification: str
    collecting: bool              # mid multi-turn parameter collection (resumed via Redis)
    param_request: dict[str, Any] # structured "required inputs" payload for the UI
    llm_unavailable: bool
    interrupt_payload: dict[str, Any]

    # planning / execution
    plan_json: dict[str, Any]
    diff: list
    policy_checks: list
    dependencies: list
    tool_results: list
    execution_mode: Literal["dry_run", "plan", "apply", "destroy"]
    needs_change: bool

    # human-in-the-loop
    approval_status: Literal["pending", "approved", "rejected", "not_required"]
    approver: dict[str, Any]      # {user, role, ts}

    # outputs
    references: list
    confidentiality: dict[str, Any]   # {level, score}
    reasoning_cards: list
    answer: str
    outcome: dict[str, Any]
    resolution: str

    # bookkeeping
    snow_id: str
    snow_table: str
    snow_sys_id: str
    errors: list
    retries: int
