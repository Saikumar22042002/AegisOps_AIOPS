"""Plan-summary integrity (Phase 7) — the approval card must NEVER show "+0 ~0 -0" for a plan
that isn't zero-change. Screenshots 2/9/13/17 showed +0 chips while the logs said "Plan: 8 to
add": the plan JSON was read through the redacting line-pump, got corrupted, and the parse
failure silently degraded to zeros. show_plan now captures the JSON raw and RAISES on a parse
failure instead of reporting a false zero-change summary."""

from __future__ import annotations

import pytest

from app.settings import get_settings
from app.tools.terraform import TerraformError, TerraformRunner, _summarize_plan


def _plan(*actions_list):
    return {"format_version": "1.2",
            "resource_changes": [{"address": f"r{i}", "type": "t",
                                  "change": {"actions": list(a)}} for i, a in enumerate(actions_list)]}


def test_summarize_counts_actions():
    s = _summarize_plan(_plan(["create"], ["create"], ["update"], ["delete"],
                              ["create", "delete"], ["no-op"]))
    assert s["summary"] == {"add": 2, "change": 2, "destroy": 1}
    assert len(s["diff"]) == 6


def test_summarize_empty_plan_is_zero():
    assert _summarize_plan({})["summary"] == {"add": 0, "change": 0, "destroy": 0}


async def test_show_plan_raises_on_unparseable_json(monkeypatch):
    # A corrupted/unparseable plan JSON must raise — never silently return +0 ~0 -0 to an
    # approver (the plan actually added 8 resources in screenshot 9's run).
    runner = TerraformRunner("aws-s3", get_settings())

    async def _fake_capture(args):
        return 0, '{"format_version": "1.2", "resource_changes": [BROKEN', ""

    monkeypatch.setattr(runner, "_capture_json", _fake_capture)
    with pytest.raises(TerraformError, match="refusing to report a"):
        await runner.show_plan()


async def test_show_plan_parses_clean_json(monkeypatch):
    runner = TerraformRunner("aws-s3", get_settings())
    import json

    async def _fake_capture(args):
        return 0, json.dumps(_plan(["create"], ["create"], ["create"], ["create"])), ""

    monkeypatch.setattr(runner, "_capture_json", _fake_capture)
    plan = await runner.show_plan()
    assert plan["summary"] == {"add": 4, "change": 0, "destroy": 0}


async def test_state_list_empty_on_missing_state(monkeypatch):
    runner = TerraformRunner("aws-s3", get_settings())

    async def _fake_capture(args):
        return 1, "", "No state file was found!"

    monkeypatch.setattr(runner, "_capture_json", _fake_capture)
    assert await runner.state_list() == []
