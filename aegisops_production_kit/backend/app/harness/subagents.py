"""Subagent foundation (P2.7 — Redesign/05 §6, 02 §5).

A parent kernel may spawn a read-only investigator subagent over the SAME frozen registry,
sharing ONE budget pool, depth 1 only. The child returns a typed `AgentResult` — findings +
evidence refs + confidence — NEVER a transcript (the parent must never ingest a child's raw
reasoning: prompt-injection containment, 05 §6 / 10-N). Blocked in a child: delegate,
ask_user, memory-write, channel-send, schedule (05 §6). Mutation is never delegated (the
registry is read-only by construction).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import structlog

from ..agents.investigation import ToolRegistry
from ..settings import Settings
from .budgets import Budgets
from .spec import AgentSpec

log = structlog.get_logger(__name__)

_MAX_DEPTH = 1
_CHILD_FINDINGS_CAP = 32_000   # 05 §6: AgentResult.findings ≤ 32k chars


@dataclass
class AgentResult:
    status: Literal["answered", "budget", "failed", "needs_input"]
    findings: str
    evidence_refs: list[int] = field(default_factory=list)
    confidence: Literal["low", "medium", "high"] = "low"
    # spend rolls into the parent ledger as agent_kind='subagent' (05 §6)
    iterations: int = 0


class SubagentPool:
    """Shared-budget spawner. `remaining()` decrements as children spawn; depth-capped."""

    def __init__(self, budgets: Budgets, *, depth: int = 0) -> None:
        self.budgets = budgets
        self.depth = depth
        self._spawned = 0

    def can_spawn(self) -> bool:
        return self.depth < _MAX_DEPTH and self._spawned < self.budgets.max_subagents

    async def spawn(self, settings: Settings, registry: ToolRegistry, *, subgoal: str,
                    run_id: str, org_id: str | None = None,
                    purpose: str = "inv_loop") -> AgentResult:
        """Run a read-only sub-investigation; return a TYPED result (never a transcript)."""
        if not self.can_spawn():
            return AgentResult(status="failed",
                               findings="subagent budget/depth exhausted", confidence="low")
        self._spawned += 1
        from .loop import Kernel  # local import: loop imports nothing from here (no cycle)

        # Child gets a fraction of the pool and cannot spawn again (depth 1).
        child_budget = Budgets(
            max_iterations=max(2, self.budgets.max_iterations // 2),
            max_tool_calls=max(2, self.budgets.max_tool_calls // 2),
            wall_clock_s=self.budgets.wall_clock_s, max_subagents=0)
        spec = AgentSpec(name="subagent", purpose=purpose,
                         system_prompt="You are a read-only sub-investigator. Gather evidence "
                                       "for the given subgoal using read tools only; return a "
                                       "concise finding. You cannot delegate, ask, write "
                                       "memory, or send messages.",
                         tool_policy="READ_ONLY_FROZEN", budgets=child_budget)
        kernel = Kernel(settings, spec, registry, run_id=run_id, org_id=org_id)
        res = await kernel.run(subgoal)
        confidence = "high" if res.evidence_ok and res.status == "answered" else (
            "medium" if res.evidence_ok else "low")
        refs = [i for i, o in enumerate(res.observations) if o.ok]
        return AgentResult(status=res.status, findings=res.findings[:_CHILD_FINDINGS_CAP],
                           evidence_refs=refs, confidence=confidence,
                           iterations=res.iterations)
