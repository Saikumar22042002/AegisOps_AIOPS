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
    so completed work is returned, never repeated (06 §8.1 idempotency; P0/A1 machinery)."""
    key = idempotency.make_key("dstep", step.idempotency_key)
    if await idempotency.claim(key):
        return True, None
    stored = await idempotency.get_result(key) or await idempotency.wait_for_result(key)
    return False, stored


async def store_result(step: Step, result: dict) -> None:
    await idempotency.store_result(idempotency.make_key("dstep", step.idempotency_key),
                                   result)
