"""INV entry point (P2.2 — Redesign/07 §2.2): the kernel drives the frozen read-only
investigation registry for a triage/discovery objective.

This is the FIRST production caller of the harness (07 §2.2 names `sre._collect_telemetry`
and `cloudops._read_path`). It is READ-ONLY by construction (the registry rejects mutation
markers at registration) and additive: callers invoke it behind a flag and fall back to
the existing hardcoded read path when the flag is off, so old and new coexist (T-P2-01).
"""

from __future__ import annotations

import uuid

import structlog

from ..agents.investigation import default_registry
from ..settings import Settings
from .budgets import Budgets
from .loop import Kernel, RunResult
from .spec import AgentSpec

log = structlog.get_logger(__name__)

_INV_SYSTEM = (
    "You are a read-only operations investigator. You may ONLY call the provided read "
    "tools to gather evidence — you cannot change anything. Inspect before concluding; "
    "when a tool fails, change your approach rather than repeating it; cite the "
    "observation index your conclusion rests on. Answer only when the evidence supports "
    "it, and ask only when blocked on information no available tool can supply. Do not "
    "reveal step-by-step private reasoning — give a concise, evidence-grounded summary.")


def inv_spec(purpose: str = "inv_loop") -> AgentSpec:
    # INV inherits the registry's MAX_CALLS=8 as the tool ceiling; stricter wins (09 §3).
    return AgentSpec(name="investigator", purpose=purpose, system_prompt=_INV_SYSTEM,
                     tool_policy="READ_ONLY_FROZEN",
                     budgets=Budgets(max_iterations=8, max_tool_calls=8))


def _read_registry(settings: Settings):
    """The harness read surface. P4: when capability packs are on (dark launch), the
    registry is sourced from the AWS/Azure/GCP/K8s/GitHub packs (cloud-neutral); otherwise
    the legacy hardcoded default registry (coexistence, T-P4-01)."""
    if getattr(settings, "aegisops_capability_packs", "off") == "on":
        from ..packs.registry import build_read_registry
        return build_read_registry(settings)
    return default_registry(settings)


async def investigate(settings: Settings, objective: str, *, run_id: str | None = None,
                      org_id: str | None = None, purpose: str = "inv_loop") -> RunResult:
    """Run a read-only investigation to completion; every iteration is durably logged to
    run_events under `run_id` (a fresh id when the caller has no run context)."""
    rid = run_id or str(uuid.uuid4())
    registry = _read_registry(settings)
    kernel = Kernel(settings, inv_spec(purpose), registry, run_id=rid, org_id=org_id)
    result = await kernel.run(objective)
    log.info("harness.inv_complete", run_id=rid, status=result.status,
             iterations=result.iterations, evidence_ok=result.evidence_ok)
    return result
