"""Verify + finalize nodes — post-apply verification and run close-out.

Phase 8: verification is TIME-BOUNDED (N-01 — an apply run must reach completed or fail with
a reason, never hang), and every successful apply posts a resource-appropriate success card in
the conversation (N-06) with credentials delivered via the one-time reveal (N-02)."""

from __future__ import annotations

import asyncio

import structlog

from ..graph_db.context_graph import ContextGraph
from ..settings import get_settings
from ..tools import aws as aws_tool
from ..tools import azure as azure_tool
from ..tools import gcp as gcp_tool
from . import cards
from .runtime import emitter_of
from .state import AgentState

log = structlog.get_logger(__name__)

_VERIFY_TIMEOUT_S = 30  # per-check budget: verification must terminate, never spin (N-01)


async def _reconcile_checks(state: AgentState, outputs: dict) -> list[dict]:
    """Read-only SDK reconciliation of the applied resource (real check, not decoration)."""
    settings = get_settings()
    cloud = state.get("cloud") or "aws"
    checks: list[dict] = [{"name": "Terraform outputs present", "passed": bool(outputs),
                           "detail": ", ".join(list(outputs.keys())[:8])}]
    if cloud == "aws" and outputs.get("instance_id") and aws_tool.get_aws(settings).enabled:
        inst = await aws_tool.get_aws(settings).list_instances(state.get("parsed_inputs", {}).get("region"))
        found = next((i for i in inst if i["id"] == outputs["instance_id"]), None)
        checks.append({"name": "Instance visible via EC2 API",
                       "passed": bool(found and found.get("state") in ("pending", "running")),
                       "detail": (found or {}).get("state", "not found")})
    if cloud == "aws" and outputs.get("bucket_name") and aws_tool.get_aws(settings).enabled:
        taken = await aws_tool.get_aws(settings).bucket_taken(outputs["bucket_name"])
        checks.append({"name": "Bucket visible via S3 API", "passed": taken is True,
                       "detail": outputs["bucket_name"]})
    # B4: cross-cloud reconciliation. The VM's stable name (module resource name == var.name) is
    # matched against the read-only Compute listing for that cloud — a real live check, not just
    # "outputs present". Both readers thread-offload their SDK calls; the whole reconcile is
    # 30s-bounded by verify(), so a slow cloud warns rather than hangs.
    vm_name = (state.get("parsed_inputs") or {}).get("name")
    resource = (state.get("resource") or "").lower()
    is_vm = resource in ("vm", "instance", "compute", "gce", "server")
    if cloud == "azure" and is_vm and vm_name and azure_tool.get_azure(settings).enabled:
        vms = await azure_tool.get_azure(settings).list_vms()
        found = next((v for v in vms if v.get("name") == vm_name), None)
        checks.append({"name": "VM visible via Azure Compute API", "passed": bool(found),
                       "detail": (found or {}).get("location") or "not found"})
    if cloud == "gcp" and is_vm and vm_name and gcp_tool.get_gcp(settings).enabled:
        insts = await gcp_tool.get_gcp(settings).list_all_instances()
        found = next((i for i in insts if i.get("name") == vm_name), None)
        running = bool(found and str(found.get("status", "")).upper() in ("RUNNING", "PROVISIONING", "STAGING"))
        checks.append({"name": "Instance visible via Compute API", "passed": running,
                       "detail": (found or {}).get("status") or "not found"})
    return checks


async def verify(state: AgentState, config) -> dict:
    """Post-apply verification: bounded, real, and it always terminates (N-01)."""
    emitter = emitter_of(config)
    outcome = state.get("outcome", {})
    if outcome.get("status") not in {"applied", "destroyed"}:
        return {}
    await emitter.step(6, "Verification")
    cloud = state.get("cloud") or "aws"
    outputs = outcome.get("outputs", {}) or {}
    try:
        checks = await asyncio.wait_for(_reconcile_checks(state, outputs), timeout=_VERIFY_TIMEOUT_S)
    except asyncio.TimeoutError:
        checks = [{"name": "Verification", "passed": False,
                   "detail": f"cloud reconciliation timed out after {_VERIFY_TIMEOUT_S}s — the apply "
                             "succeeded per Terraform; live status is unconfirmed"}]
        await emitter.console("stderr", f"verification: timed out after {_VERIFY_TIMEOUT_S}s (warned)")
    except Exception as e:  # noqa: BLE001 - verification must never crash or hang the run
        checks = [{"name": "Verification", "passed": False, "detail": str(e)[:160]}]
    for c in checks:
        await emitter.console("stdout", f"verification: {c['name']} = {'ok' if c['passed'] else 'warn'} {c['detail']}")

    updates: dict = {"tool_results": state.get("tool_results", []) + [{"verify": checks}],
                     "outcome": outcome}
    # ── Success card in the conversation (N-06) — secrets go via the one-time reveal, never here.
    if outcome.get("status") == "applied" and outputs:
        card = cards.success_card(state.get("resource") or "", outputs, state.get("parsed_inputs") or {})
        if card:
            await emitter.token("\n\n" + card)
            updates["answer"] = card
        host = outputs.get("public_dns") or outputs.get("public_ip")
        if host and outputs.get("login_user"):
            updates["outcome"] = {**outcome, "connection": {"host": host, "user": outputs.get("login_user"),
                                                            "key_name": outputs.get("key_name"),
                                                            "public_ip": outputs.get("public_ip")}}

    try:
        cg = ContextGraph(state.get("context_id") or state["run_id"], state.get("org_id", ""))
        await cg.add_evidence(kind="verification", ref=cloud,
                              detail={"checks": len(checks), "passed": sum(1 for c in checks if c["passed"])})
    except Exception as e:  # noqa: BLE001
        log.warning("verify.cg_failed", error=str(e))
    return updates


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
        # N-07: the timeline's Finalize node shows a short status — never a verbatim copy of
        # the chat bubble (screenshots 15/16/18 duplicated whole answers into the timeline).
        ans = (state.get("answer") or "Completed.").strip()
        resolution = ans if len(ans) <= 140 else (ans.split("\n", 1)[0][:137].rstrip(" .,") + "…")

    try:
        cg = ContextGraph(state.get("context_id") or state["run_id"], state.get("org_id", ""))
        await cg.set_outcome(status=("failed" if failed else status or "completed"), summary=resolution)
        # Close (immutable) only on a terminal state — a failure IS terminal (Phase 7 / BUG-05).
        if status == "rejected" or failed or outcome.get("status") in {"applied", "destroyed"}:
            await cg.close(resolution=resolution)
    except Exception as e:  # noqa: BLE001
        log.warning("finalize.cg_failed", error=str(e))

    return {"resolution": resolution, "outcome": {**outcome, "resolution": resolution}}
