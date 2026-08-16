"""P2.7 subagents + P2.8 prompt registry foundations."""

from __future__ import annotations

import pytest

from app.agents.investigation import ToolRegistry
from app.harness import loop as harness_loop
from app.harness.budgets import Budgets
from app.harness.subagents import AgentResult, SubagentPool
from app.settings import Settings

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _no_persistence(monkeypatch):
    async def _append(run_id, kind, payload, org_id=None):
        return 0
    monkeypatch.setattr("app.harness.loop.run_log.append", _append)


class ScriptedModel:
    def __init__(self, decisions):
        self.decisions = list(decisions)

    async def classify_json(self, settings, system, prompt, **kw):
        return self.decisions.pop(0)


def _reg(**tools):
    r = ToolRegistry()
    for n, f in tools.items():
        r.register(n, n, f)
    return r.freeze()


# ── subagents ───────────────────────────────────────────────────────────────────────────────

async def test_subagent_returns_typed_result_not_transcript(monkeypatch):
    async def pods(**_):
        return {"restarts": 5}
    reg = _reg(list_pods=pods)
    model = ScriptedModel([
        {"hypothesis": "check pods", "action": {"kind": "use_tool", "tool": "list_pods"}},
        {"hypothesis": "5 restarts", "action": {"kind": "answer", "text": "5 restarts found"}},
    ])
    monkeypatch.setattr(harness_loop.service, "classify_json", model.classify_json)
    pool = SubagentPool(Budgets(max_iterations=6, max_tool_calls=6, max_subagents=3))
    res = await pool.spawn(Settings(), reg, subgoal="how many restarts?",
                           run_id="33333333-0000-0000-0000-000000000001")
    assert isinstance(res, AgentResult)
    assert res.status == "answered" and res.confidence == "high"
    assert res.evidence_refs == [0] and "5 restarts" in res.findings
    # a typed result: no transcript / raw reasoning leaks to the parent
    assert not hasattr(res, "observations") and not hasattr(res, "hypotheses")


async def test_subagent_depth_capped_at_one():
    pool = SubagentPool(Budgets(max_subagents=3), depth=1)   # already at depth 1
    assert pool.can_spawn() is False


async def test_subagent_pool_budget_capped(monkeypatch):
    async def ok(**_):
        return {"v": 1}
    reg = _reg(look=ok)
    model = ScriptedModel([{"hypothesis": "h", "action": {"kind": "answer", "text": "done"}}
                           for _ in range(20)])
    monkeypatch.setattr(harness_loop.service, "classify_json", model.classify_json)
    pool = SubagentPool(Budgets(max_subagents=2))
    a = await pool.spawn(Settings(), reg, subgoal="x", run_id="r1")
    b = await pool.spawn(Settings(), reg, subgoal="y", run_id="r1")
    c = await pool.spawn(Settings(), reg, subgoal="z", run_id="r1")   # over the cap
    assert a.status == "answered" and b.status == "answered"
    assert c.status == "failed" and "exhausted" in c.findings


# ── prompt registry (integration: real PostgreSQL) ─────────────────────────────────────────

def test_content_hash_is_stable():
    from app.harness import prompts
    assert prompts.content_hash("x") == prompts.content_hash("x")
    assert prompts.content_hash("x") != prompts.content_hash("y")


@pytest.mark.usefixtures("live_db")
async def test_prompt_registry_versions_and_is_idempotent_by_hash():
    from app.harness import prompts

    name = "test.router." + __import__("uuid").uuid4().hex[:8]
    r1 = await prompts.register(name, "v-one content", owner="op")
    r1b = await prompts.register(name, "v-one content", owner="op")   # identical → no bump
    r2 = await prompts.register(name, "v-two content", changelog="tweaked")
    assert r1.version == 1 and r1b.version == 1                       # idempotent by hash
    assert r2.version == 2 and r2.content_hash != r1.content_hash
    latest = await prompts.resolve(name)
    assert latest.version == 2 and latest.stamp == f"{name}@2"
    pinned = await prompts.resolve(name, version=1)
    assert pinned.content == "v-one content"
    assert await prompts.resolve("never.registered") is None         # additive, not required
