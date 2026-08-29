"""Durable-engine driver + P2 harness integration (P3 — 07 P3.1, ADR-16).

Two things production needs on top of the pure engine:

1. `harness_step_executor` — a `StepExecutor` for READ / VERIFY / GATE steps that drives the
   P2 Agent Harness (the harness reasons; the engine orchestrates durability). This is where
   P3 builds ON P2 without a second loop or LLM abstraction. Mutation steps (module/day2/k8s)
   are NOT executed here — real Terraform apply stays the exec_loop/approval path (P3
   boundary); a mutation step reaching this executor returns an honest "not wired in P3"
   outcome rather than silently pretending to apply.

2. `run_durable_workflow` / `recover_run` — the entry + restart-recovery entry. Both call the
   same `engine.execute_workflow`, which recovers durable state and never repeats completed
   work. `recover_run` is what a worker/reconciler calls after a crash.
"""

from __future__ import annotations

import uuid

import structlog

from ..harness import inv as harness_inv
from ..settings import Settings
from . import engine
from .dag import Step, Workflow
from .engine import StepOutcome, WorkflowResult

log = structlog.get_logger(__name__)


async def _noop_compensator(step: Step) -> bool:
    """Default compensator: a step with no declared compensation is treated as
    irreversible-but-noted (the saga records the attempt; nothing to undo)."""
    log.info("engine.no_compensation_declared", step=step.id)
    return True


def harness_step_executor(settings: Settings, *, org_id: str | None = None):
    """A StepExecutor that runs read/verify/gate steps through the P2 harness INV loop."""
    async def execute(step: Step, outputs: dict) -> StepOutcome:
        if step.kind in ("read", "verify", "gate"):
            objective = step.params.get("objective") or (
                f"Gather read-only evidence for step {step.name!r}.")
            res = await harness_inv.investigate(settings, objective, org_id=org_id)
            return StepOutcome(
                ok=res.status == "answered" and res.evidence_ok,
                result={"findings": res.findings[:1000], "status": res.status},
                evidence={"evidence_ok": res.evidence_ok, "iterations": res.iterations},
                error=None if res.status == "answered" else f"harness: {res.status}")
        # Mutation kinds are governed by the untouched exec_loop/approval path in P3.
        return StepOutcome(ok=False,
                           error=f"step kind {step.kind!r} is not executed by the durable "
                                 "engine in P3 (mutation stays on the approval path)")
    return execute


def terraform_step_executor(settings: Settings, state: dict, config,
                            order: dict[str, int]):
    """DEF-17 (Prompt 3): the REAL mutation StepExecutor — module steps run through the
    EXISTING `exec_loop.execute_governed_step` (wire-resolution → schema → plan → plan-guard
    → policy → apply → Prompt-1 bookkeeping), so the durable engine gains real Terraform
    execution without a second execution path. Read/verify/gate steps still go through the
    P2 harness. Outputs are keyed by step id (== template_key for DEP-drafted DAGs), which
    is exactly the observation key `resolve_wires` expects. `order` maps step id → its
    stable draft ordinal (the loop-step idempotency identity across restarts)."""
    from ..agents import exec_loop

    read_exec = harness_step_executor(settings, org_id=state.get("org_id"))

    async def execute(step: Step, outputs: dict) -> StepOutcome:
        if step.kind != "module":
            return await read_exec(step, outputs)
        loop_step = dict(step.params)
        loop_step.setdefault("template_key", step.template_key)
        obs = await exec_loop.execute_governed_step(
            state, loop_step, order.get(step.id, step.wave), config, observations=outputs)
        ok = obs.get("status") in ("applied", "already_satisfied")
        return StepOutcome(ok=ok, result=obs,
                           evidence={"policy_checks": len(obs.get("policy_checks") or [])},
                           error=None if ok else obs.get("error"))
    return execute


async def run_durable_workflow(settings: Settings, run_id: str, draft: list[dict], *,
                               org_id: str | None = None, executor=None,
                               compensator=None, on_failure: str = "compensate",
                               should_cancel=None) -> WorkflowResult:
    """Compile + run a goal-DAG as a durable workflow. `executor`/`compensator` default to
    the harness-backed / no-op implementations; callers (or tests) may inject others."""
    from .dag import compile_workflow
    workflow = compile_workflow(draft, run_id=run_id)
    return await engine.execute_workflow(
        settings, run_id, workflow,
        executor=executor or harness_step_executor(settings, org_id=org_id),
        compensator=compensator or _noop_compensator, org_id=org_id,
        on_failure=on_failure, should_cancel=should_cancel)


async def recover_run(settings: Settings, run_id: str, draft: list[dict], *,
                      org_id: str | None = None, executor=None,
                      compensator=None, on_failure: str = "compensate",
                      should_cancel=None) -> WorkflowResult:
    """Restart-recovery entry (called after a crash): identical to run_durable_workflow —
    `execute_workflow` reads durable step state + idempotency claims and continues from the
    first incomplete wave, never repeating completed work (06 §8.1)."""
    log.info("engine.recover_run", run_id=run_id)
    return await run_durable_workflow(settings, run_id, draft, org_id=org_id,
                                      executor=executor, compensator=compensator,
                                      on_failure=on_failure, should_cancel=should_cancel)
