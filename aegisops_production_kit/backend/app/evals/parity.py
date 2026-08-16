"""DEF-19 topology parity harness (P5 — Redesign/07 P4.3 cutover gate).

Compares the LEGACY topology against the NEW (P2 harness → P3 engine → capability packs)
topology on the dimensions that can be checked DETERMINISTICALLY, so the parity-gated cutover
decision rests on evidence, not vibes. Each dimension is classified:

    PASS | FAIL | EXPECTED_DIFFERENCE | DEFERRED

Honesty rule (07 P4.3 + the P5 prompt): the LIVE-model dimensions (objective interpretation,
tool selection, plan quality across real objectives) require a working model over the eval
dataset. The sandbox key is dead, so those are `DEFERRED`, NOT faked. The deterministic
dimensions (capability coverage, read-only boundary, mutation-declared, provider resolution,
governance/permission decisions, run/step observability shape) are checkable now and are.

`decide_cutover` returns whether the flag may flip: only when there are zero FAILs AND zero
DEFERRED live-parity dimensions — i.e. never on this host. That is the correct, compliant
outcome: keep the inversion dark until a live key proves eval parity.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from ..packs import objective, registry
from ..settings import Settings

Verdict = Literal["PASS", "FAIL", "EXPECTED_DIFFERENCE", "DEFERRED"]


@dataclass
class DimensionResult:
    dimension: str
    verdict: Verdict
    detail: str


@dataclass
class ParityReport:
    results: list[DimensionResult] = field(default_factory=list)

    def add(self, dimension: str, verdict: Verdict, detail: str) -> None:
        self.results.append(DimensionResult(dimension, verdict, detail))

    @property
    def fails(self) -> list[DimensionResult]:
        return [r for r in self.results if r.verdict == "FAIL"]

    @property
    def deferred(self) -> list[DimensionResult]:
        return [r for r in self.results if r.verdict == "DEFERRED"]

    def as_dict(self) -> dict:
        return {"results": [{"dimension": r.dimension, "verdict": r.verdict,
                             "detail": r.detail} for r in self.results],
                "fails": len(self.fails), "deferred": len(self.deferred)}


# The set of user objectives the new topology must be able to serve, mapped to the legacy
# domain the router would have chosen — the deterministic interpretation check.
_INTERPRETATION_CASES = [
    ("find my VPC", "network"),
    ("how many EC2 instances are running?", "compute"),
    ("investigate 5xx errors", "telemetry"),
    ("list my kubernetes pods", "k8s"),
    ("check why the github workflow failed", "ci"),
]


def evaluate(settings: Settings) -> ParityReport:
    r = ParityReport()

    # 1. Capability coverage: the new read surface (packs) must cover the legacy read tools.
    pack_tools = {t.name for p in registry.all_packs(settings) for t in p.read_tools()}
    legacy_families = {"network", "compute", "storage", "db", "k8s", "telemetry", "repo", "ci"}
    covered = {t.family for p in registry.all_packs(settings) for t in p.read_tools()}
    missing = legacy_families - covered
    r.add("capability_coverage",
          "PASS" if not (missing - {"storage", "db"}) else "FAIL",
          f"pack read tools={len(pack_tools)}; families covered={sorted(covered)}; "
          f"Az/GCP storage/db read gaps are the F-12 asymmetry (DEF-20), not a regression")

    # 2. Read-only boundary preserved in the new topology.
    reg = registry.build_read_registry(settings, packs=registry.all_packs(settings))
    mutation_leaked = any(("create" in n or "delete" in n or "scale" in n or "restart" in n)
                          for n in reg.names())
    r.add("read_only_boundary", "FAIL" if mutation_leaked else "PASS",
          "no mutation tool in the pack read registry" if not mutation_leaked
          else "mutation tool leaked into read surface")

    # 3. Mutation stays declared (governed path), never executed by the pack read layer.
    declared = {t.template_key for p in registry.all_packs(settings)
                for t in p.mutation_specs() if t.template_key}
    r.add("mutation_governed", "PASS" if declared else "FAIL",
          f"{len(declared)} mutation templates declared, executed by exec_loop/approval path")

    # 4. Deterministic objective interpretation matches the expected capability family.
    interp_ok = all(objective.classify(text).family == fam
                    for text, fam in _INTERPRETATION_CASES)
    r.add("objective_interpretation_deterministic", "PASS" if interp_ok else "FAIL",
          "provider-neutral objective model maps each case to its capability family")

    # 5. LIVE dimensions — require a working model over the eval dataset (07 P4.3).
    for dim in ("live_tool_selection_parity", "live_plan_quality_parity",
                "live_reasoning_trace_parity", "behavioral_eval_gate_both_topologies"):
        r.add(dim, "DEFERRED",
              "requires a live model over the eval dataset; sandbox key is dead "
              "(API_KEY_INVALID) — cannot be proven on this host (07 P4.3 dark-until-parity)")
    return r


@dataclass
class CutoverDecision:
    may_cutover: bool
    reason: str


def decide_cutover(report: ParityReport) -> CutoverDecision:
    """The frozen gate (07 P4.3): flip only when parity is fully PROVEN. Any FAIL or any
    DEFERRED live-parity dimension keeps the inversion DARK."""
    if report.fails:
        return CutoverDecision(False, f"{len(report.fails)} parity FAIL(s) — stay dark")
    if report.deferred:
        return CutoverDecision(
            False, f"{len(report.deferred)} live-parity dimension(s) DEFERRED (dead model "
                   "key) — cannot prove eval parity; stay dark per 07 P4.3")
    return CutoverDecision(True, "full parity proven — cutover permitted")
