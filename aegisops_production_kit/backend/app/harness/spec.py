"""AgentSpec (P2 — Redesign/04 §2).

A declarative agent identity. `purpose` is THE ONLY model coupling (the P1 router resolves
it); everything else is behavior configuration. P2 ships the READ tool policies only —
`GOVERNED_PROPOSE`/mutation is P3+ (04 §2 explicitly has no GOVERNED_MUTATION policy).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .budgets import Budgets

ToolPolicy = Literal["NONE", "READ_ONLY_FROZEN", "GOVERNED_PROPOSE"]


@dataclass(frozen=True)
class AgentSpec:
    name: str
    purpose: str                       # a P1 canonical purpose (router/inv_loop/sre.triage/…)
    system_prompt: str                 # inline in P2; PromptRef indirection is P2.8
    tool_policy: ToolPolicy = "READ_ONLY_FROZEN"
    budgets: Budgets = field(default_factory=Budgets)
    context_recipe: str = "inv_default"
    # Structured success criteria the verifier checks (04 §7.4). Free-form checks in P2.
    success_criteria: tuple[str, ...] = ()
