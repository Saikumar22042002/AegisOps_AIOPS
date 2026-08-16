"""Budget governor (P2 — Redesign/04 §5).

Hard ceilings enforced INSIDE the loop, not observed after it (00 §4.4). On breach the
kernel is allowed exactly one grace model call to produce an honest partial, then halts
`failed(budget)`. Enforcement points: iteration boundary, pre-provider-call, pre-tool-call.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Budgets:
    max_iterations: int = 10
    max_tool_calls: int = 24
    max_cost_usd: float | None = None
    max_tokens_total: int | None = None
    wall_clock_s: float = 1800.0
    # INV read-mode inherits the registry's MAX_CALLS=8; stricter wins (09 §3).
    max_subagents: int = 3


@dataclass
class BudgetState:
    """Live counters for one run; `check()` returns a breach reason or None."""
    budgets: Budgets
    started_monotonic: float
    iterations: int = 0
    tool_calls: int = 0
    tokens_total: int = 0
    cost_usd: float = 0.0
    subagents: int = 0
    _grace_used: bool = field(default=False)

    def spend(self, *, tokens: int = 0, cost: float = 0.0) -> None:
        self.tokens_total += tokens
        self.cost_usd += cost

    def check(self, now_monotonic: float) -> str | None:
        b = self.budgets
        if self.iterations >= b.max_iterations:
            return f"iteration ceiling ({b.max_iterations})"
        if self.tool_calls >= b.max_tool_calls:
            return f"tool-call ceiling ({b.max_tool_calls})"
        if b.max_tokens_total is not None and self.tokens_total >= b.max_tokens_total:
            return f"token ceiling ({b.max_tokens_total})"
        if b.max_cost_usd is not None and self.cost_usd >= b.max_cost_usd:
            return f"cost ceiling (${b.max_cost_usd:.2f})"
        if now_monotonic - self.started_monotonic >= b.wall_clock_s:
            return f"wall-clock ceiling ({b.wall_clock_s:.0f}s)"
        return None

    def take_grace(self) -> bool:
        """One grace model call is allowed after a breach, for an honest partial."""
        if self._grace_used:
            return False
        self._grace_used = True
        return True
