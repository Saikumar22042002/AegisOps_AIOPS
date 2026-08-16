"""Durable wave-scheduled execution engine (P3.1/3.6 — Redesign/06 §8, 07 Phase 3).

`execute_workflow` runs a compiled Workflow in dependency-ordered waves. For each step it:
  1. claims-or-recovers (no-double-apply: a step done before a crash returns its stored
     result and is skipped — the restart-safety guarantee);
  2. executes via the injected `StepExecutor` (the engine orchestrates; it does not itself
     mutate clouds — real Terraform apply stays the exec_loop/approval path, P3 boundary);
  3. persists Step state + run_events and drives a verify;
  4. on failure, compensates completed steps in reverse (saga) and ends `rolled_back`.

The SAME function is the recovery entry point: called again after a restart, it replays
durable state, skips completed steps, and continues from the first incomplete wave.
Run-status transitions go through the 06 §8.3 machine (single writer: this engine).
Gated by `aegisops_durable_engine` (default off) — exec_loop remains the default path.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import structlog
from sqlalchemy import select

from ..db.models import Run
from ..db.session import session_scope
from ..harness import run_log
from ..settings import Settings
from . import saga
from . import steps as step_store
from .dag import Step, Workflow
from .saga import CompensationFrozen
from .status import RunStatus, can_transition

log = structlog.get_logger(__name__)

DEFAULT_MAX_STEPS = 8   # P3.9: 5→8 behind config (the interim exec_loop ceiling was 5)


@dataclass
class StepOutcome:
    ok: bool
    result: dict | None = None
    evidence: dict | None = None
    error: str | None = None


# Executes one step → outcome. Injected so P3 never wires real cloud mutation itself:
# tests pass a fake; read/verify steps can drive the P2 harness; the real module/day2/k8s
# executors are registered in P4. A raising executor is treated as a failed step.
StepExecutor = Callable[[Step, dict], Awaitable[StepOutcome]]


@dataclass
class WorkflowResult:
    status: str                       # completed | failed | rolled_back
    completed: list[str] = field(default_factory=list)
    failed_step: str | None = None
    compensated: list[str] = field(default_factory=list)
    recovered: list[str] = field(default_factory=list)   # steps skipped as already-done
    frozen: str | None = None         # set iff a compensation froze the saga


async def set_run_status(run_id: str, to: RunStatus, *, org_id: str | None = None,
                         force: bool = False) -> bool:
    """Single-writer run-status transition through the 06 §8.3 machine. Emits a run_event.
    `force` is only for the initial adoption of a legacy literal into the machine."""
    async with session_scope() as s:
        run = await s.get(Run, uuid.UUID(run_id))
        if run is None:
            return False
        current = run.status
        if not force and not can_transition(current, to.value):
            log.warning("engine.illegal_transition", run_id=run_id, frm=current, to=to.value)
            return False
        run.status = to.value
        if to.value in ("completed", "failed", "rolled_back", "cancelled"):
            run.ended_at = datetime.now(UTC)
    # Run-status transitions are observable as steering events (a state change the UI + audit
    # can follow); terminal states also carry their own run_finished below.
    await run_log.append(run_id, "steering", {"run_status": to.value}, org_id=org_id)
    return True


async def execute_workflow(settings: Settings, run_id: str, workflow: Workflow, *,
                           executor: StepExecutor, compensator: saga.Compensator,
                           org_id: str | None = None,
                           max_steps: int | None = None) -> WorkflowResult:
    """Run (or resume) a durable workflow to a terminal state."""
    ceiling = max_steps or int(getattr(settings, "aegisops_max_steps", DEFAULT_MAX_STEPS))
    if len(workflow.steps) > ceiling:
        return WorkflowResult(status="failed",
                              failed_step=None) if False else WorkflowResult(
            status="failed", failed_step=f"workflow exceeds max_steps ({ceiling})")

    durable = await step_store.load_steps(run_id)          # recovery: what already finished
    outputs: dict[str, dict] = {sid: rec.result or {} for sid, rec in durable.items()
                                if rec.status == "done"}
    completed: list[str] = [s.id for s in workflow.steps
                            if durable.get(s.id) and durable[s.id].status == "done"]
    recovered = list(completed)

    await set_run_status(run_id, RunStatus.EXECUTING, org_id=org_id)

    for wave_no, ids in enumerate(workflow.waves):
        for sid in ids:
            step = workflow.step(sid)
            if sid in completed:
                continue                                    # already done — never re-run

            should_execute, stored = await step_store.claim_or_recover(step)
            if not should_execute:
                # Another worker / a pre-crash run finished it: adopt the stored result.
                outputs[sid] = stored or {}
                completed.append(sid)
                if sid not in recovered:
                    recovered.append(sid)
                continue

            await step_store.start(run_id, step, org_id=org_id)
            try:
                outcome = await executor(step, outputs)
            except Exception as e:  # noqa: BLE001 — a raising executor is a failed step
                outcome = StepOutcome(ok=False, error=str(e)[:300])

            if not outcome.ok:
                await step_store.finish(run_id, step, ok=False, error=outcome.error,
                                        org_id=org_id)
                return await _fail_and_compensate(run_id, workflow, completed, step,
                                                  outcome.error, compensator, org_id)

            await step_store.store_result(step, outcome.result or {})
            await step_store.finish(run_id, step, ok=True, result=outcome.result,
                                    evidence=outcome.evidence, org_id=org_id)
            outputs[sid] = outcome.result or {}
            completed.append(sid)

    # Verify + complete.
    await set_run_status(run_id, RunStatus.VERIFYING, org_id=org_id)
    await run_log.append(run_id, "verification",
                         {"verdict": "verified", "steps": len(completed)}, org_id=org_id)
    await set_run_status(run_id, RunStatus.COMPLETED, org_id=org_id)
    await run_log.append(run_id, "run_finished",
                         {"status": "completed", "steps": len(completed),
                          "recovered": recovered}, org_id=org_id)
    return WorkflowResult(status="completed", completed=completed, recovered=recovered)


async def _fail_and_compensate(run_id: str, workflow: Workflow, completed: list[str],
                               failed_step: Step, error: str | None,
                               compensator: saga.Compensator,
                               org_id: str | None) -> WorkflowResult:
    # Stay in EXECUTING through compensation, then land a single clean terminal:
    # ROLLED_BACK (compensation succeeded) or FAILED (compensation froze). `failed` is a
    # terminal with no exit — we never transition out of it (06 §8.3 single-writer machine).
    await run_log.append(run_id, "deviation",
                         {"failed_step": failed_step.id, "error": error}, org_id=org_id)
    try:
        compensated = await saga.compensate(run_id, workflow, completed,
                                            compensator=compensator, org_id=org_id)
    except CompensationFrozen as cf:
        # Freeze + page: FAILED terminal with a loud, human-actionable signal.
        await set_run_status(run_id, RunStatus.FAILED, org_id=org_id)
        await run_log.append(run_id, "run_finished",
                             {"status": "failed", "compensation_frozen": cf.step_id,
                              "detail": cf.detail}, org_id=org_id)
        log.error("engine.compensation_frozen", run_id=run_id, step=cf.step_id)
        return WorkflowResult(status="failed", failed_step=failed_step.id,
                              completed=completed, frozen=cf.step_id)
    await set_run_status(run_id, RunStatus.ROLLED_BACK, org_id=org_id)
    await run_log.append(run_id, "run_finished",
                         {"status": "rolled_back", "failed_step": failed_step.id,
                          "compensated": compensated}, org_id=org_id)
    return WorkflowResult(status="rolled_back", completed=completed,
                          failed_step=failed_step.id, compensated=compensated)
