"""The intelligent execution kernel (P2 — Redesign/00 §4, 04 §3).

OBSERVE → REASON → ACT → OBSERVE → … → VERIFY → COMPLETE / ASK / STOP.

Loop laws honored (04 §3.1): L1 nothing here mutates (read-only registry); L2 budgets
checked inside the loop; L3 a failed tool is an OBSERVATION the next reason step sees;
L4 re-planning is re-reasoning over accumulated observations (no bolted-on replanner);
L5 durable at iteration boundaries (every iteration flushed to run_events); L7 ask is a
first-class action.

Discipline (04 §11): no SDK imports, no domain knowledge, no persistence logic (delegated
to run_log), no policy logic. The kernel decides ONLY: reason → act → observe → stop-or-go.
Reasoning is a native structured-output call on the P1 model layer; the required
machine-comparable `hypothesis` field lives in every assistant_turn (IP-1 / C-06).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import structlog

from ..agents.investigation import ToolRegistry
from ..llm import service
from ..llm.errors import ModelError
from ..llm.types import ToolCall
from ..settings import Settings
from . import registry as tool_registry
from . import run_log
from .budgets import BudgetState
from .spec import AgentSpec

log = structlog.get_logger(__name__)

# The reason step returns this shape (native structured output, provider-enforced, P1.8).
# `hypothesis` is one machine-comparable line (NOT chain-of-thought); `rationale` is a
# privacy-safe summary safe to surface. `action.kind` drives the loop.
DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "hypothesis": {"type": "string"},
        "rationale": {"type": "string"},
        "action": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["use_tool", "answer", "ask"]},
                "tool": {"type": "string"},
                "args": {"type": "object", "additionalProperties": True},
                "text": {"type": "string"},
            },
            "required": ["kind"],
        },
    },
    "required": ["hypothesis", "action"],
}

_STUCK_REPEAT_LIMIT = 2   # identical (tool, args_hash) allowed at most twice (IP-1 §5)


@dataclass
class Observation:
    tool: str
    ok: bool
    content: str
    error: dict[str, str] | None = None


@dataclass
class RunResult:
    status: str                       # answered | budget | failed | needs_input
    findings: str
    hypotheses: list[str] = field(default_factory=list)
    observations: list[Observation] = field(default_factory=list)
    iterations: int = 0
    evidence_ok: bool = False
    root_cause_observation: int | None = None
    ask: str | None = None


class Kernel:
    """One objective, one run. Cloud/provider-neutral; drives a frozen read-only registry."""

    def __init__(self, settings: Settings, spec: AgentSpec, registry: ToolRegistry, *,
                 run_id: str, org_id: str | None = None) -> None:
        self.settings = settings
        self.spec = spec
        self.registry = registry
        self.run_id = run_id
        self.org_id = org_id
        self.budget = BudgetState(budgets=spec.budgets, started_monotonic=time.monotonic())
        self._observations: list[Observation] = []
        self._hypotheses: list[str] = []
        self._act_counts: dict[str, int] = {}   # (tool, args_hash) → count, stuck detector

    async def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        await run_log.append(self.run_id, kind, payload, org_id=self.org_id)

    def _context(self) -> str:
        """Volatile-tail context: objective + the observation trail (06 §6 band 3). The
        stable prefix (system prompt) rides the ModelRequest system message."""
        lines = [f"OBJECTIVE: {self.objective}", "", "Available read tools:"]
        lines += [f"  - {n}" for n in self.registry.names()]
        if self._observations:
            lines += ["", "Observations so far (oldest→newest):"]
            for i, o in enumerate(self._observations):
                tag = "OK" if o.ok else "FAILED"
                body = o.content if o.ok else f"{o.error.get('kind')}: {o.error.get('message')}"
                lines.append(f"  [{i}] {o.tool} → {tag}: {body[:600]}")
        else:
            lines += ["", "No observations yet — inspect before concluding."]
        lines += ["",
                  "Decide the NEXT single action. Prefer a read tool when a fact is missing; "
                  "answer only when the objective is satisfied by the evidence; ask only when "
                  "genuinely blocked on information no tool can supply. If a tool FAILED, do "
                  "NOT repeat the same call — change approach or tool. State a one-line "
                  "machine-comparable hypothesis of the current best explanation."]
        return "\n".join(lines)

    async def _reason(self) -> dict[str, Any] | None:
        """One native structured-output call. Returns the decision dict, or None on a
        provider error (itself recorded as a budget/again-able condition)."""
        try:
            decision = await service.classify_json(
                self.settings, self.spec.system_prompt, self._context(),
                purpose=self.spec.purpose, response_schema=DECISION_SCHEMA,
                org_id=self.org_id)
        except ModelError as e:
            log.warning("kernel.reason_failed", run_id=self.run_id, kind=e.kind)
            return None
        return decision if isinstance(decision, dict) else None

    async def run(self, objective: str) -> RunResult:
        self.objective = objective
        while True:
            breach = self.budget.check(time.monotonic())
            if breach:
                return await self._halt_on_budget(breach)
            self.budget.iterations += 1
            await self._emit("iteration_started", {"n": self.budget.iterations})

            decision = await self._reason()
            if decision is None:
                # Provider hiccup: an iteration is consumed; the budget bounds the retries.
                await self._emit("observation",
                                 {"ok": False, "note": "reasoning call failed; retrying"})
                continue

            hypothesis = str(decision.get("hypothesis", "")).strip()
            self._hypotheses.append(hypothesis)
            action = decision.get("action") or {}
            # assistant_turn carries the machine-comparable hypothesis (IP-1 / C-06) and a
            # privacy-safe rationale — never raw chain-of-thought.
            await self._emit("assistant_turn", {
                "hypothesis": hypothesis,
                "rationale": str(decision.get("rationale", ""))[:1000],
                "action_kind": action.get("kind")})

            kind = action.get("kind")
            if kind == "answer":
                return await self._finish(str(action.get("text", "")))
            if kind == "ask":
                return await self._ask(str(action.get("text", "")))
            if kind == "use_tool":
                stuck = await self._maybe_stuck(action)
                if stuck is not None:
                    return stuck
                await self._act(action)
                continue
            # Unknown action shape → observation, let the model correct itself.
            self._observations.append(Observation(
                tool="(none)", ok=False, content="",
                error={"kind": "bad_action", "message": f"unknown action {kind!r}"}))

    async def _maybe_stuck(self, action: dict[str, Any]) -> RunResult | None:
        tc = ToolCall(id=f"c{self.budget.tool_calls}", name=str(action.get("tool", "")),
                      arguments=action.get("args") or {})
        key = f"{tc.name}:{tc.args_hash}"
        self._act_counts[key] = self._act_counts.get(key, 0) + 1
        if self._act_counts[key] > _STUCK_REPEAT_LIMIT:
            # Anti-scripting stop (IP-1 §5 / IP-4): identical action ≥3 times = a loop, not
            # intelligence. Halt honestly rather than burn the budget repeating.
            await self._emit("budget", {"reason": "stuck", "action": key,
                                        "repeats": self._act_counts[key]})
            return RunResult(status="failed",
                             findings="Halted: repeated the same action without progress — "
                                      "no new evidence was being gathered.",
                             hypotheses=self._hypotheses, observations=self._observations,
                             iterations=self.budget.iterations)
        self._pending_call = tc
        return None

    async def _act(self, action: dict[str, Any]) -> None:
        tc = self._pending_call
        self.budget.tool_calls += 1
        await self._emit("tool_call", {"tool": tc.name, "args_hash": tc.args_hash})
        result = await tool_registry.execute(self.registry, tc)
        obs = Observation(tool=tc.name, ok=result.ok,
                          content=str(result.content) if result.ok else "",
                          error=None if result.ok else result.error)
        self._observations.append(obs)
        await self._emit("observation", {
            "index": len(self._observations) - 1, "tool": tc.name, "ok": result.ok,
            "stage": result.stage,
            "error": result.error, "preview": (obs.content[:300] if result.ok else None)})

    async def _finish(self, text: str) -> RunResult:
        evidence_ok = any(o.ok for o in self._observations)
        # Verification (first-class, 04 §7): an "answer" with zero successful reads is not
        # evidence-backed — downgrade to an honest partial rather than assert unverified.
        verdict = "verified" if evidence_ok else "unverifiable"
        await self._emit("verification", {"verdict": verdict,
                                          "successful_reads": sum(o.ok for o in self._observations)})
        root = next((i for i, o in enumerate(self._observations) if not o.ok), None)
        await self._emit("run_finished", {"status": "answered", "evidence_ok": evidence_ok})
        return RunResult(status="answered", findings=text, hypotheses=self._hypotheses,
                         observations=self._observations, iterations=self.budget.iterations,
                         evidence_ok=evidence_ok, root_cause_observation=root)

    async def _ask(self, question: str) -> RunResult:
        await self._emit("run_finished", {"status": "needs_input"})
        return RunResult(status="needs_input", findings="", ask=question,
                         hypotheses=self._hypotheses, observations=self._observations,
                         iterations=self.budget.iterations)

    async def _halt_on_budget(self, reason: str) -> RunResult:
        await self._emit("budget", {"reason": reason})
        findings = ("Stopped at a safe boundary before completing the objective "
                    f"({reason}).")
        if self.budget.take_grace():
            try:
                grace = await service.generate(
                    self.settings, purpose=self.spec.purpose,
                    system=self.spec.system_prompt,
                    prompt=self._context() + "\n\nBUDGET REACHED. In two sentences, give an "
                           "honest partial: what was established and what remains unknown.",
                    org_id=self.org_id)
                findings = grace.content or findings
            except ModelError:
                pass
        await self._emit("run_finished", {"status": "budget", "reason": reason})
        return RunResult(status="budget", findings=findings, hypotheses=self._hypotheses,
                         observations=self._observations, iterations=self.budget.iterations)
