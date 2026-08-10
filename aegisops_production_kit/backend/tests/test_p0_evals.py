"""P0 evaluation-gate tests: the gate passes on good behavior, and — the property the
mandate demands — provably REJECTS known-bad behavior (fixture + corrupted-expectation)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).parents[1]
sys.path.insert(0, str(BACKEND))  # `evals` is a repo package (not installed; app* only)

from evals.runner import evaluate_case, load_dataset, run_dataset  # noqa: E402


def test_regular_cases_all_pass():
    results = run_dataset()
    failed = [r for r in results if not r.known_bad and not r.ok]
    assert not failed, {r.case_id: r.failures for r in failed}


def test_known_bad_fixture_is_rejected_by_the_scorer():
    """The seeded known-bad case encodes deliberately wrong expectations (a question that
    'should' destroy). The REAL guard downgrades it to read → the scorer MUST fail it.
    If this test fails, the evaluation system can no longer reject known-bad behavior."""
    results = run_dataset()
    known_bad = [r for r in results if r.known_bad]
    assert known_bad, "dataset must always carry at least one known-bad fixture"
    assert all(not r.ok for r in known_bad), (
        f"known-bad case(s) scored as PASSING: {[r.case_id for r in known_bad if r.ok]}")


def test_the_guard_is_what_rejects_it_not_the_scorer_shape():
    """White-box: the known-bad case's ACTUAL behavior is the guard downgrade — proving
    the eval replays real production code, not a simulation."""
    case = next(c for c in load_dataset() if c.get("known_bad"))
    actual = evaluate_case(case)
    assert actual["action"] == "read" and actual["guard_note"] is not None


def test_gate_exit_codes_end_to_end():
    """The CI contract, executed exactly as CI runs it: default gate exits 0; self-test
    (known-bad must fail) exits 0; a corrupted expectation closes the gate with exit 1."""
    def gate(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "evals.gate", *args],
                              cwd=BACKEND, capture_output=True, text=True, timeout=120)

    assert gate().returncode == 0
    assert gate("--self-test").returncode == 0

    # Corrupt one good case's expectation → the gate MUST close (exit 1).
    cases = load_dataset()
    good = next(c for c in cases if not c.get("known_bad"))
    good["expect"]["domain"] = "sre"  # wrong on purpose
    bad_dataset = BACKEND / "evals" / "out" / "corrupted_dataset_for_test.jsonl"
    bad_dataset.parent.mkdir(parents=True, exist_ok=True)
    bad_dataset.write_text("\n".join(json.dumps(c) for c in cases) + "\n", encoding="utf-8")
    try:
        proc = gate("--dataset", str(bad_dataset))
        assert proc.returncode == 1, f"gate stayed open on a regression: {proc.stdout}"
        assert "GATE CLOSED" in proc.stdout
    finally:
        bad_dataset.unlink(missing_ok=True)


def test_verdict_artifacts_are_machine_readable():
    from evals.gate import main
    assert main([]) == 0
    report = json.loads((BACKEND / "evals" / "out" / "eval_report.json").read_text())
    for key in ("ts", "verdict", "total", "passed", "failed", "known_bad_total"):
        assert key in report
    history = (BACKEND / "evals" / "out" / "eval_runs.jsonl").read_text().splitlines()
    assert history and json.loads(history[-1])["verdict"] == "pass"
