"""Run + Step status machines (P3.6 — Redesign/06 §8.3).

The full run-status machine that replaces the phantom `applying` (D5, now fully dead):
running · awaiting_approval · awaiting_input · scheduled · executing · verifying ·
completed · failed · rolled_back · cancelled. Every literal has exactly one writer owner;
`can_transition` is the guard the durable engine (and only the durable engine) enforces on
its runs so an illegal jump is a loud error, never a silent bad state.

This is code, not a DB CHECK, on purpose: the legacy exec_loop path writes a subset of
these literals and must keep working during coexistence (P3 boundary — additive).
"""

from __future__ import annotations

from enum import Enum


class RunStatus(str, Enum):
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    AWAITING_INPUT = "awaiting_input"
    SCHEDULED = "scheduled"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"
    CANCELLED = "cancelled"


# `applying` is intentionally ABSENT — D5 fully dead at P3.6 (06 §8.3).
_TERMINAL = {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.ROLLED_BACK,
             RunStatus.CANCELLED}

# Allowed transitions for a durable-engine run. A pause state (awaiting_*) can resume to
# executing; executing verifies or fails; a failed execution may roll back (saga).
_RUN_EDGES: dict[RunStatus, set[RunStatus]] = {
    RunStatus.RUNNING: {RunStatus.SCHEDULED, RunStatus.EXECUTING, RunStatus.AWAITING_APPROVAL,
                        RunStatus.AWAITING_INPUT, RunStatus.FAILED, RunStatus.CANCELLED},
    RunStatus.SCHEDULED: {RunStatus.EXECUTING, RunStatus.CANCELLED, RunStatus.FAILED},
    RunStatus.AWAITING_APPROVAL: {RunStatus.EXECUTING, RunStatus.FAILED, RunStatus.CANCELLED},
    RunStatus.AWAITING_INPUT: {RunStatus.EXECUTING, RunStatus.RUNNING, RunStatus.CANCELLED,
                               RunStatus.FAILED},
    RunStatus.EXECUTING: {RunStatus.VERIFYING, RunStatus.EXECUTING, RunStatus.AWAITING_APPROVAL,
                          RunStatus.AWAITING_INPUT, RunStatus.FAILED, RunStatus.ROLLED_BACK,
                          RunStatus.CANCELLED, RunStatus.COMPLETED},
    RunStatus.VERIFYING: {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.EXECUTING,
                          RunStatus.ROLLED_BACK},
    # `failed` and `rolled_back` are terminal with no exit: compensation runs from
    # EXECUTING (executing→rolled_back on success, executing→failed on freeze), so the
    # engine never needs to leave a terminal state (06 §8.3 single-writer machine).
}


def is_terminal(status: str) -> bool:
    try:
        return RunStatus(status) in _TERMINAL
    except ValueError:
        return status in {"completed", "failed", "rolled_back", "cancelled"}


def can_transition(frm: str, to: str) -> bool:
    try:
        f, t = RunStatus(frm), RunStatus(to)
    except ValueError:
        return False
    if f in _TERMINAL:
        return False
    return t in _RUN_EDGES.get(f, set())


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    COMPENSATED = "compensated"   # a done step whose effect was rolled back by saga


STEP_TERMINAL = {StepStatus.DONE, StepStatus.FAILED, StepStatus.CANCELLED,
                 StepStatus.COMPENSATED}
