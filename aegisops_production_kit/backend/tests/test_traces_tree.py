"""O1 — the in-app Traces tab is a real tree derived from run_steps with real durations.

Exercises the pure span builder so it needs no datastore: every duration is the step's actual
elapsed time (never a fabricated `—`), an in-flight step shows `···` rather than a made-up
number, failed steps are marked, and the run root sits above the ordered children.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.api.artifacts import _trace_spans

_T0 = datetime(2026, 7, 12, 9, 0, 0, tzinfo=timezone.utc)


def _step(name, offset_s, dur_s, status="done", tool=None, human="auto", retries=0, error=None):
    start = _T0 + timedelta(seconds=offset_s)
    end = start + timedelta(seconds=dur_s) if dur_s is not None else None
    return SimpleNamespace(name=name, status=status, tool=tool, human_vs_auto=human,
                           retries=retries, error=error, started_at=start, ended_at=end,
                           order_index=offset_s)


def _run(status="completed", domain="cloudops"):
    return SimpleNamespace(status=status, domain=domain)


def test_root_then_children_with_real_durations():
    steps = [
        _step("router", 0, 0.48),
        _step("cloudops_agent", 0.5, 3.2, tool="terraform"),
        _step("execute", 4.0, 64, tool="terraform"),
    ]
    spans, total = _trace_spans(_run(), steps)
    # root + 3 children
    assert len(spans) == 4
    root = spans[0]
    assert root["depth"] == 0 and root["indent"] == 0 and root["name"] == "cloudops run"
    # children are indented, in order, with the tool appended and REAL formatted durations
    assert [s["name"] for s in spans[1:]] == [
        "router", "cloudops_agent · terraform", "execute · terraform"]
    assert [s["dur"] for s in spans[1:]] == ["480ms", "3.2s", "1m04s"]
    assert all(s["indent"] == 14 and s["depth"] == 1 for s in spans[1:])
    # total spans first start .. last end = 0 .. 68s
    assert total == "1m08s"
    # no fabricated placeholder anywhere
    assert "—" not in [s["dur"] for s in spans]


def test_in_flight_step_shows_progress_not_a_fake_number():
    steps = [_step("router", 0, 0.4), _step("cloudops_agent", 0.5, None, status="running")]
    spans, _total = _trace_spans(_run(status="running"), steps)
    running = spans[2]
    assert running["dur"] == "···" and running["status"] == "running"
    assert spans[0]["dur"] == "···"  # root of a still-running run is also in-flight, not a number


def test_failed_step_is_marked_red_and_carries_the_error():
    steps = [_step("cloudops_execute", 0, 2.0, status="failed", error="boom" * 80)]
    spans, _ = _trace_spans(_run(status="failed"), steps)
    assert spans[0]["dot"] == "var(--red)"          # failed run root
    child = spans[1]
    assert child["dot"] == "var(--red)" and child["status"] == "failed"
    assert child["error"] and len(child["error"]) <= 200   # truncated, present


def test_human_and_retry_annotations():
    steps = [_step("approval", 0, 5, human="human"),
             _step("execute", 5, 3, tool="terraform", retries=2)]
    spans, _ = _trace_spans(_run(), steps)
    assert spans[1]["name"] == "approval (human)"
    assert spans[2]["name"] == "execute · terraform · retry 2"
