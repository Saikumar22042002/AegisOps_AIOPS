"""Execute dispatcher — routes the post-approval execution to the right domain executor."""

from __future__ import annotations

import structlog

from .cloudops import cloudops_execute
from .devops import devops_execute
from .runtime import emitter_of
from .sre import sre_execute
from .state import AgentState

log = structlog.get_logger(__name__)


async def execute(state: AgentState, config) -> dict:
    # S5: capability assertion at the mutation choke-point (defense-in-depth behind the approval
    # gate). Execution is only reached after an approved interrupt, and /approvals already
    # requires an approver (can_approve == can_execute). Re-assert here: the recorded approver
    # must actually hold execute capability. Fail closed — never mutate on a missing/false cap.
    if state.get("approval_status") != "approved":
        return {}
    if not (state.get("approver") or {}).get("can_execute"):
        log.error("execute.capability_denied", run_id=state.get("run_id"),
                  approver=(state.get("approver") or {}).get("user"))
        await emitter_of(config).error(
            "Execution blocked: the approving principal lacks execute capability.",
            code="capability_denied", retriable=False)
        return {"outcome": {"status": "blocked", "error": "capability assertion failed at execute"}}

    domain = state.get("domain")
    if domain == "cloudops":
        return await cloudops_execute(state, config)
    if domain == "devops":
        return await devops_execute(state, config)
    if domain == "sre":
        return await sre_execute(state, config)
    return {}
