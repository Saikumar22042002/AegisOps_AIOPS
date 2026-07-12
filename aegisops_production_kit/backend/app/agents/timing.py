"""Real per-step timing for the LangGraph run — persisted to the `run_steps` table.

Each graph node (and a few sub-steps inside CloudOps) records a real `started_at`/`ended_at`
so the artifact Timeline can show genuine durations ("terraform apply took 17s") instead of
hardcoded values. Timings survive the run, so past messages render their real timings too.

`start_step` is upsert-by-(run_id, name): on a resume re-entry (e.g. the approval node runs
again after the human decision) the original `started_at` is preserved, so the recorded
duration of "Human Approval" is the true wall-clock wait for the human.

Every step also opens/closes a Langfuse span (deterministic id `<run_id>:<name>`), so the
run's trace carries the same node tree — including the approval span across the interrupt
and failed steps recorded as ERROR spans. Both sinks are best-effort: neither DB timing nor
tracing can break a run.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import structlog
from sqlalchemy import select

from ..db.models import RunStep
from ..db.session import session_scope
from ..integrations.langfuse_client import get_tracer
from ..metrics import AGENT_STEP_DURATION
from ..settings import get_settings

log = structlog.get_logger(__name__)

# Canonical display order for the steps the Timeline renders.
ORDER: dict[str, int] = {
    "router": 0,
    "cloudops_agent": 1, "devops_plan": 1, "sre_analyze": 1, "knowledge": 1, "general": 1,
    "policy_evaluation": 2, "planner": 3, "approval": 4, "execute": 5, "verify": 6,
    "finalize": 7, "servicenow_update": 8, "notify": 9,
}

# Subsystem each step belongs to (the `agent` label of the AGENT_STEP_DURATION histogram, so
# the series is grouped by subsystem rather than exploding one label per node name).
_AGENT_OF: dict[str, str] = {
    "router": "router",
    "cloudops_agent": "cloudops", "devops_plan": "devops", "sre_analyze": "sre",
    "knowledge": "knowledge", "general": "general",
    "policy_evaluation": "core", "planner": "core", "execute": "core", "verify": "core",
    "approval": "approval", "finalize": "finalize",
    "servicenow_update": "servicenow", "notify": "notify",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def start_step(run_id: str, name: str, *, tool: str | None = None, human_vs_auto: str = "auto") -> None:
    """Mark a step started. Idempotent: preserves the first start across resume re-entries."""
    if not run_id:
        return
    started_at = _now()
    try:
        async with session_scope() as s:
            row = (await s.execute(
                select(RunStep).where(RunStep.run_id == uuid.UUID(run_id), RunStep.name == name)
            )).scalar_one_or_none()
            if row is None:
                s.add(RunStep(run_id=uuid.UUID(run_id), name=name, status="running", tool=tool,
                              human_vs_auto=human_vs_auto, started_at=started_at, order_index=ORDER.get(name, 50)))
            elif row.ended_at is None:
                row.status = "running"  # re-entry before completion — keep original started_at
                started_at = row.started_at
    except Exception as e:  # noqa: BLE001 - timing is best-effort, never breaks a run
        log.warning("timing.start_failed", name=name, error=str(e))
    try:
        get_tracer(get_settings()).step_started(run_id, name, tool=tool, started_at=started_at)
    except Exception as e:  # noqa: BLE001
        log.warning("timing.trace_start_failed", name=name, error=str(e))


async def end_step(run_id: str, name: str, *, status: str = "done", error: str | None = None,
                   result: dict | None = None) -> None:
    """Mark a step ended (sets ended_at + final status)."""
    if not run_id:
        return
    started_at: datetime | None = None
    ended_at = _now()
    try:
        async with session_scope() as s:
            row = (await s.execute(
                select(RunStep).where(RunStep.run_id == uuid.UUID(run_id), RunStep.name == name)
            )).scalar_one_or_none()
            if row is not None:
                row.ended_at = ended_at
                row.status = status
                if error:
                    row.error = error[:2000]
                if result is not None:
                    row.result = result
                started_at = row.started_at
    except Exception as e:  # noqa: BLE001
        log.warning("timing.end_failed", name=name, error=str(e))
    # O3: emit the real per-step latency (non-empty series; was declared but never observed).
    if started_at is not None:
        elapsed = (ended_at - started_at).total_seconds()
        if elapsed >= 0:
            AGENT_STEP_DURATION.labels(agent=_AGENT_OF.get(name, "other"), step=name).observe(elapsed)
    try:
        get_tracer(get_settings()).step_ended(run_id, name, status=status, error=error,
                                              result=result, started_at=started_at, ended_at=ended_at)
    except Exception as e:  # noqa: BLE001
        log.warning("timing.trace_end_failed", name=name, error=str(e))
