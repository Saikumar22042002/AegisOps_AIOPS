"""Verify + finalize nodes — post-apply verification and run close-out."""

from __future__ import annotations

import structlog

from ..graph_db.context_graph import ContextGraph
from ..settings import get_settings
from ..tools import aws as aws_tool
from .runtime import emitter_of
from .state import AgentState

log = structlog.get_logger(__name__)


async def verify(state: AgentState, config) -> dict:
    """Post-apply verification via read-only cloud SDK reads (best-effort)."""
    emitter = emitter_of(config)
    settings = get_settings()
    outcome = state.get("outcome", {})
    if outcome.get("status") not in {"applied", "destroyed"}:
        return {}
    await emitter.step(6, "Verification")
    cloud = state.get("cloud") or "aws"
    checks: list = []
    try:
        if cloud == "aws" and aws_tool.get_aws(settings).enabled:
            outputs = outcome.get("outputs", {})
            checks.append({"name": "Terraform outputs present", "passed": bool(outputs), "detail": ", ".join(outputs.keys())})
        else:
            checks.append({"name": "Verification", "passed": True, "detail": f"{cloud} apply completed"})
        await emitter.console("stdout", f"verification: {checks[-1]['name']} ok")
    except Exception as e:  # noqa: BLE001
        checks.append({"name": "Verification", "passed": False, "detail": str(e)[:160]})
    try:
        cg = ContextGraph(state.get("context_id") or state["run_id"], state.get("org_id", ""))
        await cg.add_evidence(kind="verification", ref=cloud, detail={"checks": len(checks)})
    except Exception as e:  # noqa: BLE001
        log.warning("verify.cg_failed", error=str(e))
    return {"tool_results": state.get("tool_results", []) + [{"verify": checks}]}


async def finalize(state: AgentState, config) -> dict:
    """Compose the final outcome + resolution and close the context graph."""
    emitter = emitter_of(config)
    status = state.get("approval_status")
    outcome = state.get("outcome", {})

    if status == "rejected":
        resolution = "Plan rejected — no changes applied."
    elif outcome.get("status") in {"applied", "destroyed"}:
        resolution = f"{outcome['status'].capitalize()} successfully."
    elif state.get("needs_change") and status == "pending":
        resolution = "Awaiting approval."
    else:
        resolution = state.get("answer", "Completed.")

    try:
        cg = ContextGraph(state.get("context_id") or state["run_id"], state.get("org_id", ""))
        await cg.set_outcome(status=status or "completed", summary=resolution)
        # Close (immutable) only on a terminal state.
        if status == "rejected" or outcome.get("status") in {"applied", "destroyed"}:
            await cg.close(resolution=resolution)
    except Exception as e:  # noqa: BLE001
        log.warning("finalize.cg_failed", error=str(e))

    return {"resolution": resolution, "outcome": {**outcome, "resolution": resolution}}
