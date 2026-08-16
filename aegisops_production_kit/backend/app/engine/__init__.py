"""P3 durable execution / workflow engine (Redesign/06 §8, 07 Phase 3, ADR-07/ADR-16).

Turns the P2 harness's intelligent execution into a durable, restart-safe Task/Run/Step
system: a compiled Workflow of Steps runs in dependency-ordered waves, each step's state
and events are persisted, a process death recovers from durable state without repeating
completed work, and a failure compensates completed steps in reverse (saga).

Boundaries this package HOLDS (P3 prompt):
- the harness (app/harness) stays authoritative for REASONING; the engine orchestrates
  DURABILITY. No second agent loop, no LLM abstraction, no provider logic here.
- mutation stays governed: real Terraform apply remains the exec_loop/approval path
  (untouched). The durable engine executes idempotent steps and drives the harness for
  read/verify; it does NOT migrate CloudOps mutations (that is P4).
- coexistence: gated by `aegisops_durable_engine` (default off) — the existing exec_loop
  remains the default path (T-P3-01).
"""

from .dag import Step, Workflow, compile_workflow
from .status import RunStatus, StepStatus

__all__ = ["Step", "Workflow", "compile_workflow", "RunStatus", "StepStatus"]
