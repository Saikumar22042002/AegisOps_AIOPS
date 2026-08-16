"""P2 Agent Harness (Redesign/00 §3-4, 02 §2.1, 04, 06, 07 Phase 2).

The governed agent runtime: a cloud/provider-neutral kernel that pursues an operational
objective through OBSERVE → REASON → ACT → OBSERVE → … → VERIFY → COMPLETE/ASK/STOP.

Boundaries this package HOLDS (07 Phase 2, and the P2 prompt):
- reads only in P2 — the kernel drives the frozen read-only investigation registry;
  mutation stays on the existing exec_loop/approval path (rule two, 07 §0).
- builds ON P1 (`app/llm` service/router/executor); never a second model abstraction.
- ≤500-line loop, no SDK imports, no domain knowledge, no persistence logic, no policy
  logic inside `loop.py` (04 §11) — those live in sibling modules and the tool registry.
- LangGraph is untouched; the harness is a parallel read-path runtime, not a spine swap.
"""

from .budgets import Budgets
from .spec import AgentSpec

__all__ = ["AgentSpec", "Budgets"]
