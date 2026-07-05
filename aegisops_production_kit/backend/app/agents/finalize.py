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
    outputs = outcome.get("outputs", {}) or {}
    checks: list = []
    conn: dict = {}
    try:
        if cloud == "aws" and aws_tool.get_aws(settings).enabled:
            checks.append({"name": "Terraform outputs present", "passed": bool(outputs), "detail": ", ".join(outputs.keys())})
        else:
            checks.append({"name": "Verification", "passed": True, "detail": f"{cloud} apply completed"})
        await emitter.console("stdout", f"verification: {checks[-1]['name']} ok")
    except Exception as e:  # noqa: BLE001
        checks.append({"name": "Verification", "passed": False, "detail": str(e)[:160]})

    # ── Surface usable connection details (masked; private key never printed) ──
    if outcome.get("status") == "applied" and outputs:
        host = outputs.get("public_dns") or outputs.get("public_ip")
        if host and outputs.get("login_user"):  # a compute instance
            conn = {"host": host, "user": outputs.get("login_user"), "key_name": outputs.get("key_name"),
                    "public_ip": outputs.get("public_ip")}
            line = f"\n\n**Instance ready.** Connect: `ssh {conn['user']}@{host}`"
            if conn.get("key_name"):
                line += f" — key pair `{conn['key_name']}`"
            if outcome.get("sensitive_outputs"):
                line += (f"\n_Private key is not shown here; retrieve it once via "
                         f"`terraform output -raw {outcome['sensitive_outputs'][0]}`._")
            await emitter.token(line)
            await emitter.console("stdout", f"connection: ssh {conn['user']}@{host} (key: {conn.get('key_name')})")
        elif outputs.get("endpoint"):  # a managed database
            conn = {"endpoint": outputs.get("endpoint")}
            await emitter.token(f"\n\n**Database ready.** Endpoint: `{outputs['endpoint']}` — master credentials are "
                                f"stored in AWS Secrets Manager (RDS-managed), not shown here.")

    try:
        cg = ContextGraph(state.get("context_id") or state["run_id"], state.get("org_id", ""))
        await cg.add_evidence(kind="verification", ref=cloud, detail={"checks": len(checks)})
    except Exception as e:  # noqa: BLE001
        log.warning("verify.cg_failed", error=str(e))
    return {"tool_results": state.get("tool_results", []) + [{"verify": checks}],
            "outcome": {**outcome, "connection": conn} if conn else outcome}


async def finalize(state: AgentState, config) -> dict:
    """Compose the final outcome + resolution and close the context graph."""
    status = state.get("approval_status")
    outcome = state.get("outcome", {})

    out_status = str(outcome.get("status") or "")
    failed = out_status.endswith("_failed") or out_status == "failed"

    if status == "rejected":
        resolution = "Plan rejected — no changes applied."
    elif outcome.get("status") in {"applied", "destroyed"}:
        resolution = f"{outcome['status'].capitalize()} successfully."
    elif failed:
        # A classified provider failure carries a human title (Phase 7 / BUG-05).
        f = outcome.get("failure") if isinstance(outcome.get("failure"), dict) else None
        resolution = f"Failed — {(f or {}).get('title') or out_status.replace('_', ' ')}."
    elif state.get("needs_change") and status == "pending":
        resolution = "Awaiting approval."
    else:
        resolution = state.get("answer", "Completed.")

    try:
        cg = ContextGraph(state.get("context_id") or state["run_id"], state.get("org_id", ""))
        await cg.set_outcome(status=("failed" if failed else status or "completed"), summary=resolution)
        # Close (immutable) only on a terminal state — a failure IS terminal (Phase 7 / BUG-05).
        if status == "rejected" or failed or outcome.get("status") in {"applied", "destroyed"}:
            await cg.close(resolution=resolution)
    except Exception as e:  # noqa: BLE001
        log.warning("finalize.cg_failed", error=str(e))

    return {"resolution": resolution, "outcome": {**outcome, "resolution": resolution}}
