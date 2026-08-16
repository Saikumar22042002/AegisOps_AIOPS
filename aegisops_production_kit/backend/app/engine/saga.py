"""Saga compensation (P3.2 — Redesign/07 P3.2).

On a step failure, completed steps are compensated in REVERSE order using the
pre-declared compensation the workflow carried at compile time. A compensation that
itself fails FREEZES the saga and surfaces a page-worthy signal (never a silent
half-rollback) — 07 P3.2: "compensation-failure freeze + page".

P3 keeps this at the durable-orchestration layer: real cloud/Terraform compensation is
the exec_loop/engine-executor mutation path (untouched here); the durable engine records
the compensation intent + outcome per step and drives the registered compensator.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import structlog

from . import steps as step_store
from .dag import Step, Workflow

log = structlog.get_logger(__name__)

# A compensator undoes one completed step; returns True on success. Injected by the caller
# (tests inject a fake; the domain executors register real ones in P4). Default = no-op ok
# (a step with no declared compensation is treated as irreversible-but-noted).
Compensator = Callable[[Step], Awaitable[bool]]


class CompensationFrozen(Exception):
    """A compensator failed — the saga is frozen; a human must resolve it."""

    def __init__(self, step_id: str, detail: str):
        self.step_id = step_id
        self.detail = detail
        super().__init__(f"compensation failed for {step_id!r}: {detail}")


async def compensate(run_id: str, workflow: Workflow, completed: list[str], *,
                     compensator: Compensator, org_id: str | None = None) -> list[str]:
    """Compensate `completed` step ids in REVERSE order. Returns the compensated ids.
    Raises CompensationFrozen the moment a compensator fails (freeze + page)."""
    compensated: list[str] = []
    for sid in reversed(completed):
        step = workflow.step(sid)
        try:
            ok = await compensator(step)
        except Exception as e:  # noqa: BLE001 — a raising compensator is a failed one
            log.error("saga.compensation_raised", run_id=run_id, step=sid, error=str(e))
            raise CompensationFrozen(sid, str(e)[:300]) from e
        if not ok:
            log.error("saga.compensation_failed", run_id=run_id, step=sid)
            raise CompensationFrozen(sid, "compensator returned False")
        await step_store.mark_compensated(run_id, sid, org_id=org_id)
        compensated.append(sid)
    return compensated
