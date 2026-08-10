"""Deterministic eval lane (P0): replay RECORDED model outputs through the REAL code.

What runs is not a simulation: each case's recorded LLM response text goes through the
actual production functions —

    app.agents.llm._extract_json            (response parsing, fence handling)
    app.agents.router.normalize_classification   (field normalization)
    app.agents.intent_guard.guard_classification (the deterministic safety guard)
    app.agents.router.apply_post_guard_rules     (broad-inventory + ambiguity rules)
    app.agents.templates.select                  (catalog template selection)

— so a regression in any of them fails the gate. What is NOT exercised here: the live
LLM (that's the key-gated judge lane), the graph, tools, or any datastore. This lane
is hermetic and runs anywhere.

Future phases plug richer evidence sources into the same scorer: when `run_events`
lands (P2), the intelligence-proof tests (IP-1..4, Redesign/10 §2) evaluate real run
logs through this same gate. P0 deliberately proves only that the evaluation
infrastructure can reject known-bad behavior.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.agents import intent_guard, templates
from app.agents.llm import _extract_json
from app.agents.router import apply_post_guard_rules, normalize_classification
from app.evals.scoring import CaseResult, score_case

DATASET = Path(__file__).parent / "dataset.jsonl"


def load_dataset(path: Path | None = None) -> list[dict[str, Any]]:
    lines = (path or DATASET).read_text(encoding="utf-8").splitlines()
    return [json.loads(ln) for ln in lines if ln.strip()]


def evaluate_case(case: dict[str, Any]) -> dict[str, Any]:
    """Produce the ACTUAL observables for one case by running the real pipeline."""
    message: str = case["message"]
    cls = _extract_json(case["recorded_response"])

    updates = normalize_classification(cls)
    guarded = intent_guard.guard_classification(message, updates)
    if guarded:
        updates["guard_note"] = guarded.pop("guard_note", "guard fired")
        updates.update(guarded)
    else:
        updates["guard_note"] = None
    updates = apply_post_guard_rules(updates, message, float(cls.get("confidence", 0.5)))

    # Template selection — only meaningful for actionable cloudops classifications.
    template_key = None
    if (updates.get("domain") == "cloudops"
            and updates.get("action") in ("create", "modify", "destroy")
            and updates.get("cloud") and updates.get("resource")):
        tpl = templates.select(updates["cloud"], updates["resource"])
        template_key = tpl.key if tpl else None
    updates["template_key"] = template_key
    return updates


def run_dataset(path: Path | None = None) -> list[CaseResult]:
    return [score_case(case, evaluate_case(case)) for case in load_dataset(path)]
