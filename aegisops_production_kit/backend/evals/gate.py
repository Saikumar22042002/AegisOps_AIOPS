"""P0 release gate (Redesign/04 §9, 07 item 0.2). Exit code 0 = gate OPEN; 1 = CLOSED.

Modes:
    python -m evals.gate               all regular cases must pass → OPEN, else CLOSED
    python -m evals.gate --self-test   ONLY the known-bad seeded cases run, and every one
                                       must FAIL scoring. This proves the property the
                                       mandate demands: known-bad behavior → evaluation →
                                       FAIL → gate rejects it. If a known-bad case ever
                                       scores as passing, the self-test CLOSES the gate.
    python -m evals.gate --judge       additionally run the key-gated judge lane
                                       (honest skip when GEMINI_API_KEY is absent).

Machine-readable artifacts (evals/out/, gitignored, uploaded by CI):
    eval_report.json   latest verdict
    eval_runs.jsonl    append-only verdict history
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from app.evals.scoring import summarize

from .runner import DATASET, run_dataset

OUT_DIR = Path(__file__).parent / "out"


def _write_artifacts(report: dict) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "eval_report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with open(OUT_DIR / "eval_runs.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(report, sort_keys=True) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P0 behavioral evaluation gate")
    parser.add_argument("--self-test", action="store_true",
                        help="require every known-bad case to FAIL (gate calibration)")
    parser.add_argument("--judge", action="store_true",
                        help="also run the key-gated judge lane")
    parser.add_argument("--dataset", type=Path, default=None)
    args = parser.parse_args(argv)

    results = run_dataset(args.dataset)
    summary = summarize(results)
    report = {
        "ts": datetime.now(UTC).isoformat(),
        "dataset": str(args.dataset or DATASET),
        "mode": "self-test" if args.self_test else "gate",
        **summary,
    }

    if args.self_test:
        ok = (summary["known_bad_total"] > 0
              and not summary["known_bad_wrongly_passing"])
        report["verdict"] = "pass" if ok else "fail"
        _write_artifacts(report)
        if ok:
            print(f"GATE SELF-TEST OK — {summary['known_bad_correctly_rejected']}/"
                  f"{summary['known_bad_total']} known-bad case(s) correctly rejected.")
            return 0
        print("GATE CLOSED — the evaluation system FAILED to reject known-bad behavior: "
              f"{summary['known_bad_wrongly_passing']}")
        return 1

    judge_note = "not run"
    if args.judge:
        from .judge import run_judge
        judge_result = run_judge()
        judge_note = judge_result["status"]
        report["judge"] = judge_result
        if judge_result["status"] == "fail":
            report["verdict"] = "fail"
            _write_artifacts(report)
            print("GATE CLOSED — judge scores below threshold.")
            return 1

    if summary["failed"]:
        report["verdict"] = "fail"
        _write_artifacts(report)
        print(f"GATE CLOSED — {summary['failed']}/{summary['total']} case(s) failed: "
              f"{sorted(summary['failed_cases'])}")
        for case_id, failures in sorted(summary["failed_cases"].items()):
            for failure in failures:
                print(f"  {case_id}: {failure}")
        return 1

    report["verdict"] = "pass"
    _write_artifacts(report)
    print(f"GATE OPEN — {summary['passed']}/{summary['total']} deterministic case(s) passed; "
          f"judge: {judge_note}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
