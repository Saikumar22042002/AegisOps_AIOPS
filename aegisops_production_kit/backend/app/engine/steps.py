"""Durable step store (P3 — Redesign/06 §8.1).

Persists each Step's lifecycle to `run_steps` and mirrors transitions to `run_events`, so a
restart can read exactly which steps completed. `claim_or_recover` is the no-double-apply
guard: it reuses the P0/A1 idempotency machinery (Redis claim + stored result) AND the
durable DB row, so a step that finished before a crash returns its stored result and is
NEVER executed again.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select

from ..db.models import RunStep
from ..db.session import session_scope
from ..harness import run_log
from ..security import idempotency
from .dag import Step
from .status import StepStatus

log = structlog.get_logger(__name__)


@dataclass
class StepRecord:
    step_id: str
    status: str
    result: dict | None
    wave: int


async def load_steps(run_id: str) -> dict[str, StepRecord]:
    """Durable step state for a run, keyed by the logical step id (RunStep.name)."""
    async with session_scope() as s:
        rows = (await s.execute(select(RunStep).where(
            RunStep.run_id == uuid.UUID(run_id)))).scalars().all()
    return {r.name: StepRecord(step_id=r.name, status=r.status, result=r.result,
                               wave=r.wave if r.wave is not None else 0) for r in rows}


async def start(run_id: str, step: Step, *, org_id: str | None = None) -> None:
    async with session_scope() as s:
        row = (await s.execute(select(RunStep).where(
            RunStep.run_id == uuid.UUID(run_id), RunStep.name == step.id))).scalar_one_or_none()
        if row is None:
            row = RunStep(run_id=uuid.UUID(run_id), name=step.id, order_index=step.wave)
            s.add(row)
        row.status = StepStatus.RUNNING.value
        row.tool = step.kind
        row.kind = step.kind
        row.wave = step.wave
        row.depends_on = ",".join(step.depends_on) or None
        row.idempotency_key = step.idempotency_key
        row.started_at = datetime.now(UTC)
    await run_log.append(run_id, "step_started",
                         {"step": step.id, "wave": step.wave, "kind": step.kind},
                         org_id=org_id)


async def finish(run_id: str, step: Step, *, ok: bool, result: dict | None = None,
                 error: str | None = None, evidence: dict | None = None,
                 org_id: str | None = None) -> None:
    status = StepStatus.DONE.value if ok else StepStatus.FAILED.value
    async with session_scope() as s:
        row = (await s.execute(select(RunStep).where(
            RunStep.run_id == uuid.UUID(run_id), RunStep.name == step.id))).scalar_one()
        row.status = status
        row.result = result
        row.error = error
        row.evidence = evidence
        row.ended_at = datetime.now(UTC)
    await run_log.append(run_id, "step_finished",
                         {"step": step.id, "ok": ok, "error": error,
                          "evidence": evidence}, org_id=org_id)


async def mark_compensated(run_id: str, step_id: str, *, org_id: str | None = None) -> None:
    async with session_scope() as s:
        row = (await s.execute(select(RunStep).where(
            RunStep.run_id == uuid.UUID(run_id), RunStep.name == step_id))).scalar_one()
        row.status = StepStatus.COMPENSATED.value
        row.compensation_of = step_id
    await run_log.append(run_id, "deviation",
                         {"compensated": step_id}, org_id=org_id)


async def claim_or_recover(step: Step) -> tuple[bool, dict | None]:
    """Returns (should_execute, recovered_result).

    `should_execute` is True only when this process wins a fresh idempotency claim; if the
    step already ran (before a crash, or on another worker), returns (False, stored_result)
    so completed work is returned, never repeated (06 §8.1 idempotency; P0/A1 machinery).

    CRASH RECOVERY (Prompt 3, found by the live restart test): a worker killed MID-STEP
    leaves the claim held with NO stored result. Waiting yields nothing — the claimant is
    dead. Treating that as "done with empty outputs" would poison downstream wires, so the
    stale claim is released and re-claimed: the step RE-EXECUTES, which is safe for module
    steps because Terraform re-plans against its state (a completed-but-unrecorded apply
    reconciles to zero-change → ALREADY_SATISFIED, never a duplicate resource)."""
    key = idempotency.make_key("dstep", step.idempotency_key)
    if await idempotency.claim(key):
        return True, None
    stored = await idempotency.get_result(key) or await idempotency.wait_for_result(key)
    if stored is not None:
        return False, stored
    # Prod-hardening (2026-08-17): a held claim with no result after the wait is only
    # reclaimable when the DRIVING RUN is provably dead — the run heartbeat, not the 20s
    # result deadline, is the liveness signal (a live 45-minute terraform apply must never
    # be misidentified as stale). idempotency_key = "dstep:{run_id}:{step_id}".
    run_id = step.idempotency_key.split(":")[1] if ":" in step.idempotency_key else None
    if run_id:
        try:
            from ..cache.redis import get_redis
            from ..agents.supervisor import hb_key
            if bool(await get_redis().exists(hb_key(run_id))):
                log.warning("engine.claim_held_by_live_run_waiting", step=step.id, run=run_id)
                return False, await idempotency.wait_for_result(key)
        except Exception:  # noqa: BLE001 — unknown liveness ⇒ do NOT reclaim
            log.warning("engine.claim_liveness_unknown_not_reclaiming", step=step.id)
            return False, None
    log.warning("engine.stale_claim_reclaimed", step=step.id,
                detail="claim held with no result and the driving run's heartbeat is dead — "
                       "prior worker died mid-step; re-executing (terraform reconciles)")
    await idempotency.release(key)
    if await idempotency.claim(key):
        return True, None
    # Another live worker re-claimed it in the same instant — genuinely in flight now.
    stored = await idempotency.wait_for_result(key)
    return False, stored


async def store_result(step: Step, result: dict) -> None:
    await idempotency.store_result(idempotency.make_key("dstep", step.idempotency_key),
                                   result)


async def release_claim(step: Step) -> None:
    """Release a FAILED step's idempotency claim so a retry/recovery can re-claim it.

    Prompt 3 fix (found by the retain-then-recover pin): without this, a failed step's
    stale claim made `claim_or_recover` treat it as already-done on the next attempt —
    the step was skipped with an empty result and a failed workflow could never truly
    resume. Successful steps keep their claim + stored result (never re-applied)."""
    await idempotency.release(idempotency.make_key("dstep", step.idempotency_key))
