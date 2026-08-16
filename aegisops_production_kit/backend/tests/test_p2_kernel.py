"""P2 — Agent Harness kernel: the OBSERVE→REASON→ACT loop and its intelligence proof.

The load-bearing tests are the behavioral ones (10 IP-1..IP-4): a tool failure must become
an observation that CHANGES the next hypothesis and action, ending in recovery; a
deterministic same-action retry loop must be HALTED, not rewarded. The model is a scripted
fake (no network) so the proof is about the KERNEL's control flow, not a live model.
"""

from __future__ import annotations

import pytest

from app.agents.investigation import ToolRegistry
from app.harness import registry as tool_registry
from app.harness import run_log
from app.harness.budgets import Budgets
from app.harness.loop import Kernel
from app.harness.spec import AgentSpec
from app.settings import Settings

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _no_persistence(monkeypatch):
    """Unit tier: capture run_events in memory instead of the DB."""
    events: list[tuple[str, dict]] = []

    async def _append(run_id, kind, payload, org_id=None):
        events.append((kind, payload))
        return len(events) - 1

    monkeypatch.setattr(run_log, "append", _append)
    monkeypatch.setattr("app.harness.loop.run_log.append", _append)
    return events


def _registry(**tools) -> ToolRegistry:
    reg = ToolRegistry()
    for name, fn in tools.items():
        reg.register(name, name, fn)
    return reg.freeze()


def _spec(**over) -> AgentSpec:
    base = dict(name="inv", purpose="inv_loop", system_prompt="You investigate.",
                budgets=Budgets(max_iterations=8, max_tool_calls=8))
    base.update(over)
    return AgentSpec(**base)


class ScriptedModel:
    """Returns one decision per reason() call, in order. Each is the {hypothesis,
    rationale, action} dict the kernel's structured-output call would produce."""
    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.calls = 0

    async def classify_json(self, settings, system, prompt, *, purpose=None,
                            response_schema=None, org_id=None, **kw):
        self.calls += 1
        return self.decisions.pop(0)

    async def generate(self, settings, *, purpose, system, prompt, org_id=None, **kw):
        from app.llm.types import ModelResponse, ServedBy
        return ModelResponse(content="Partial: budget reached.",
                             served_by=ServedBy(provider="fake", model="fake"))


def _wire(monkeypatch, model):
    monkeypatch.setattr("app.harness.loop.service.classify_json", model.classify_json)
    monkeypatch.setattr("app.harness.loop.service.generate", model.generate)


async def _kernel(monkeypatch, model, registry, **spec_over) -> Kernel:
    _wire(monkeypatch, model)
    return Kernel(Settings(), _spec(**spec_over), registry, run_id="11111111-0000-0000-0000-000000000001")


# ── the intelligence proof (IP-1) ─────────────────────────────────────────────────────────────

async def test_failure_changes_hypothesis_and_action_then_recovers(monkeypatch, _no_persistence):
    """IP-1: tool A fails → observation → DIFFERENT hypothesis AND a DIFFERENT tool
    (different evidence family) → success → answer citing the recovered evidence."""
    async def broken_metric(**_):
        raise RuntimeError("scrape target down")

    async def pod_logs(**_):
        return {"logs": "OOMKilled: container exceeded memory limit"}

    reg = _registry(query_prometheus=broken_metric, list_pods=pod_logs)
    model = ScriptedModel([
        {"hypothesis": "error rate is high; check the metric",
         "action": {"kind": "use_tool", "tool": "query_prometheus", "args": {"q": "err"}}},
        # After the FAILED observation, the hypothesis AND the tool change:
        {"hypothesis": "metric source is down; inspect pod logs directly instead",
         "action": {"kind": "use_tool", "tool": "list_pods", "args": {"ns": "prod"}}},
        {"hypothesis": "root cause is an OOM kill",
         "action": {"kind": "answer", "text": "Root cause: OOMKilled (obs 1)."}},
    ])
    k = await _kernel(monkeypatch, model, reg)
    res = await k.run("Why is the service unhealthy?")

    assert res.status == "answered"
    # (1) a failed observation existed
    assert any(not o.ok for o in res.observations)
    # (2) the next action targeted a DIFFERENT tool (different evidence family)
    tools = [o.tool for o in res.observations]
    assert tools == ["query_prometheus", "list_pods"]
    # (3) the hypothesis changed after the failure
    assert res.hypotheses[0] != res.hypotheses[1]
    # (4) recovered: at least one successful read, answer cites it
    assert res.evidence_ok and "OOM" in res.findings
    # (5) identical (tool,args) repetitions ≤ 2 — never happened here
    kinds = [k for k, _ in _no_persistence]
    assert "assistant_turn" in kinds and "observation" in kinds and "verification" in kinds


async def test_hypothesis_field_present_on_every_assistant_turn(monkeypatch, _no_persistence):
    reg = _registry(list_pods=lambda **_: {"pods": 3})
    model = ScriptedModel([
        {"hypothesis": "need pod count", "action": {"kind": "use_tool", "tool": "list_pods"}},
        {"hypothesis": "three pods, healthy", "action": {"kind": "answer", "text": "3 pods."}},
    ])
    k = await _kernel(monkeypatch, model, reg)
    await k.run("How many pods?")
    turns = [p for kind, p in _no_persistence if kind == "assistant_turn"]
    assert turns and all(t["hypothesis"] for t in turns)          # C-06: always present
    assert all("chain_of_thought" not in t and "cot" not in t for t in turns)  # no raw CoT


# ── anti-scripting oracle (IP-4) ────────────────────────────────────────────────────────────

async def test_deterministic_repeat_loop_is_halted_not_rewarded(monkeypatch, _no_persistence):
    """IP-4: a fake that repeats the SAME failing action must NOT pass as intelligence —
    the kernel halts it after the repetition limit with an honest failure."""
    async def always_fails(**_):
        raise RuntimeError("still down")

    reg = _registry(query_prometheus=always_fails)
    same = {"hypothesis": "same guess", "action": {"kind": "use_tool",
            "tool": "query_prometheus", "args": {"q": "x"}}}
    model = ScriptedModel([dict(same) for _ in range(6)])
    k = await _kernel(monkeypatch, model, reg)
    res = await k.run("diagnose")
    assert res.status == "failed"
    assert "repeated the same action" in res.findings
    # halted at the limit — not after burning the whole iteration budget
    assert sum(1 for o in res.observations) <= 2


# ── budgets (R) ────────────────────────────────────────────────────────────────────────────

async def test_iteration_budget_halts_with_honest_partial(monkeypatch, _no_persistence):
    async def ok(**_):
        return {"v": 1}
    reg = _registry(look=ok)
    # never answers — would loop forever without the budget
    model = ScriptedModel([{"hypothesis": f"h{i}",
                            "action": {"kind": "use_tool", "tool": "look", "args": {"i": i}}}
                           for i in range(50)])
    k = await _kernel(monkeypatch, model, reg, budgets=Budgets(max_iterations=3, max_tool_calls=99))
    res = await k.run("investigate forever")
    assert res.status == "budget" and res.iterations == 3
    assert "safe boundary" in res.findings or "Partial" in res.findings
    assert ("budget", ) not in []  # sanity
    assert any(kind == "budget" for kind, _ in _no_persistence)


async def test_tool_budget_halts(monkeypatch, _no_persistence):
    async def ok(**_):
        return {"v": 1}
    reg = _registry(look=ok)
    model = ScriptedModel([{"hypothesis": f"h{i}",
                            "action": {"kind": "use_tool", "tool": "look", "args": {"i": i}}}
                           for i in range(50)])
    k = await _kernel(monkeypatch, model, reg, budgets=Budgets(max_iterations=99, max_tool_calls=2))
    res = await k.run("call tools forever")
    assert res.status == "budget"
    assert sum(o.ok for o in res.observations) == 2


# ── failure-as-observation (F) ──────────────────────────────────────────────────────────────

async def test_failed_tool_becomes_a_typed_observation_not_a_crash(monkeypatch, _no_persistence):
    async def boom(**_):
        raise ValueError("kaboom")
    reg = _registry(look=boom)
    model = ScriptedModel([
        {"hypothesis": "try look", "action": {"kind": "use_tool", "tool": "look"}},
        {"hypothesis": "look failed; answer honestly",
         "action": {"kind": "answer", "text": "Could not read; tool errored."}},
    ])
    k = await _kernel(monkeypatch, model, reg)
    res = await k.run("look")
    obs = res.observations[0]
    assert obs.ok is False and obs.error["kind"] == "tool_error"
    assert not res.evidence_ok                                   # verify: unverifiable


async def test_unknown_tool_is_an_observation(monkeypatch, _no_persistence):
    reg = _registry(look=lambda **_: {"v": 1})
    model = ScriptedModel([
        {"hypothesis": "call a tool that doesn't exist",
         "action": {"kind": "use_tool", "tool": "delete_everything"}},
        {"hypothesis": "not available; answer", "action": {"kind": "answer", "text": "n/a"}},
    ])
    k = await _kernel(monkeypatch, model, reg)
    res = await k.run("x")
    assert res.observations[0].error["kind"] == "unknown_tool"


# ── ask is first-class (E) ──────────────────────────────────────────────────────────────────

async def test_ask_is_a_first_class_terminal(monkeypatch, _no_persistence):
    reg = _registry(look=lambda **_: {"v": 1})
    model = ScriptedModel([
        {"hypothesis": "missing which cluster", "action": {"kind": "ask",
         "text": "Which cluster should I inspect?"}}])
    k = await _kernel(monkeypatch, model, reg)
    res = await k.run("check the cluster")
    assert res.status == "needs_input" and "cluster" in res.ask
    assert any(kind == "run_finished" and p["status"] == "needs_input"
               for kind, p in _no_persistence)
