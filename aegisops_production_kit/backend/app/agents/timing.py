"""Real per-step timing for the LangGraph run — persisted to the `run_steps` table.

Each graph node (and a few sub-steps inside CloudOps) records a real `started_at`/`ended_at`
so the artifact Timeline can show genuine durations ("terraform apply took 17s") instead of
hardcoded values. Timings survive the run, so past messages render their real timings too.

`start_step` is upsert-by-(run_id, name): on a resume re-entry (e.g. the approval node runs
again after the human decision) the original `started_at` is preserved, so the recorded
duration of "Human Approval" is the true wall-clock wait for the human.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import structlog
from sqlalchemy import select

from ..db.models import RunStep
from ..db.session import session_scope

log = structlog.get_logger(__name__)

# Canonical display order for the steps the Timeline renders.
ORDER: dict[str, int] = {
    "router": 0,
    "cloudops_agent": 1, "devops_plan": 1, "sre_analyze": 1, "knowledge": 1, "general": 1,
    "policy_evaluation": 2, "planner": 3, "approval": 4, "execute": 5, "verify": 6,
    "finalize": 7, "servicenow_update": 8, "notify": 9,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def start_step(run_id: str, name: str, *, tool: str | None = None, human_vs_auto: str = "auto") -> None:
    """Mark a step started. Idempotent: preserves the first start across resume re-entries."""
    if not run_id:
        return
    try:
        async with session_scope() as s:
            row = (await s.execute(
                select(RunStep).where(RunStep.run_id == uuid.UUID(run_id), RunStep.name == name)
            )).scalar_one_or_none()
            if row is None:
                s.add(RunStep(run_id=uuid.UUID(run_id), name=name, status="running", tool=tool,
                              human_vs_auto=human_vs_auto, started_at=_now(), order_index=ORDER.get(name, 50)))
            elif row.ended_at is None:
                row.status = "running"  # re-entry before completion — keep original started_at
    except Exception as e:  # noqa: BLE001 - timing is best-effort, never breaks a run
        log.warning("timing.start_failed", name=name, error=str(e))


async def end_step(run_id: str, name: str, *, status: str = "done", error: str | None = None,
                   result: dict | None = None) -> None:
    """Mark a step ended (sets ended_at + final status)."""
    if not run_id:
        return
    try:
        async with session_scope() as s:
            row = (await s.execute(
                select(RunStep).where(RunStep.run_id == uuid.UUID(run_id), RunStep.name == name)
            )).scalar_one_or_none()
            if row is not None:
                row.ended_at = _now()
                row.status = status
                if error:
                    row.error = error[:2000]
                if result is not None:
                    row.result = result
    except Exception as e:  # noqa: BLE001
        log.warning("timing.end_failed", name=name, error=str(e))
