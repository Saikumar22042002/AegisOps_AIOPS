"""The one shared, pure eval scorer (P0 behavioral evaluation gate).

Scores ACTUAL classification/guard/template observables against a case's EXPECTED
observables. No LLM calls, no I/O, no clock — a pure function of its inputs, so the
CI gate, pytest, and any future scoreboard produce identical verdicts.

"LLM returned a response" is never a pass condition: every case asserts specific,
machine-comparable observables (domain, action, guard behavior, template selection).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# The observable keys a case may assert on, and how each is compared.
_EXACT_KEYS = ("domain", "action", "target", "template_key", "needs_clarification", "cloud")


@dataclass
class CaseResult:
    case_id: str
    ok: bool
    known_bad: bool
    failures: list[str] = field(default_factory=list)


def score_case(case: dict[str, Any], actual: dict[str, Any]) -> CaseResult:
    """Score one case. `case["expect"]` holds the expected observables; `actual` holds
    what the real classification/guard/template code produced for the recorded input."""
    expect: dict[str, Any] = case.get("expect") or {}
    failures: list[str] = []

    for key in _EXACT_KEYS:
        if key in expect:
            got = actual.get(key)
            if got != expect[key]:
                failures.append(f"{key}: expected {expect[key]!r}, got {got!r}")

    if "intent_prefix" in expect:
        intent = str(actual.get("intent") or "")
        if not intent.startswith(expect["intent_prefix"]):
            failures.append(
                f"intent: expected prefix {expect['intent_prefix']!r}, got {intent!r}")

    if "guard_fired" in expect:
        fired = actual.get("guard_note") is not None
        if fired != bool(expect["guard_fired"]):
            failures.append(
                f"guard_fired: expected {expect['guard_fired']}, got {fired} "
                f"(guard_note={actual.get('guard_note')!r})")

    return CaseResult(
        case_id=str(case.get("id", "?")),
        ok=not failures,
        known_bad=bool(case.get("known_bad", False)),
        failures=failures,
    )


def summarize(results: list[CaseResult]) -> dict[str, Any]:
    """Machine-readable roll-up for eval_report.json / eval_runs.jsonl."""
    scored = [r for r in results if not r.known_bad]
    known_bad = [r for r in results if r.known_bad]
    failed = [r for r in scored if not r.ok]
    return {
        "total": len(scored),
        "passed": len(scored) - len(failed),
        "failed": len(failed),
        "failed_cases": {r.case_id: r.failures for r in failed},
        # Self-test axis: every known-bad case MUST fail scoring; one passing = the
        # gate can no longer reject known-bad behavior.
        "known_bad_total": len(known_bad),
        "known_bad_correctly_rejected": sum(1 for r in known_bad if not r.ok),
        "known_bad_wrongly_passing": [r.case_id for r in known_bad if r.ok],
    }
