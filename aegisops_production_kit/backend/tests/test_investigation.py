"""INV — read-only investigation agents: the tool surface is ASSERTED read-only at
registration, frozen once built, budget-bounded, and sub-agents can never widen it — mutation
is structurally out of a spawned agent's reach.
"""

from __future__ import annotations

import pytest

from app.agents import investigation
from app.agents.investigation import (Investigator, ReadOnlyViolation, ToolRegistry,
                                      assert_read_only, default_registry)
from app.settings import get_settings


# ── the read-only assertion itself ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", [
    "restart_deployment", "scale_deployment", "rollback_deployment", "apply_deployment",
    "terraform_apply", "delete_bucket", "destroy_vpc", "patch_sg", "set_secret",
    "update_dns", "dispatch_workflow", "create_instance", "upsert_resource",
])
def test_mutation_shaped_tools_are_rejected(bad):
    with pytest.raises(ReadOnlyViolation):
        assert_read_only(bad)


def test_read_only_shapes_pass():
    for ok in ("query_prometheus", "list_deployments", "list_pods", "describe_instance",
               "get_run", "read_state", "ping"):
        assert_read_only(ok)  # must not raise


def test_registry_rejects_mutators_and_freezes():
    reg = ToolRegistry()

    async def noop(**k): return None

    with pytest.raises(ReadOnlyViolation):
        reg.register("restart_deployment", "smuggled mutation", noop)
    reg.register("list_things", "fine", noop)
    reg.freeze()
    with pytest.raises(ReadOnlyViolation):
        reg.register("list_more", "post-freeze growth", noop)  # frozen — no new surface


# ── the default (real) tool surface ────────────────────────────────────────────────────────

def test_default_registry_is_read_only_and_never_holds_a_mutation_fn():
    settings = get_settings()
    reg = default_registry(settings)
    assert reg.names() == sorted(["query_prometheus", "list_deployments", "list_pods",
                                  "list_inventory", "query_impact"])
    # "No mutation delegation", asserted by IDENTITY: none of the registered callables is any
    # of the known mutating client methods.
    from app.tools.kubernetes import get_kubernetes
    k8s = get_kubernetes(settings)
    mutators = {k8s.restart_deployment, k8s.scale_deployment,
                k8s.rollback_deployment, k8s.apply_deployment}
    for name in reg.names():
        assert reg.get(name).fn not in mutators, f"{name} resolves to a mutating method!"
    # And the registry is frozen — an investigation can't grow its own surface.
    with pytest.raises(ReadOnlyViolation):
        reg.register("list_extra", "x", k8s.list_pods)


# ── investigator behaviour ─────────────────────────────────────────────────────────────────

def _reg_with(name="list_things", fn=None):
    reg = ToolRegistry()

    async def default_fn(**k): return {"ok": True, **k}

    reg.register(name, "test tool", fn or default_fn)
    return reg.freeze()


async def test_unregistered_tool_is_refused_not_guessed():
    inv = Investigator(_reg_with())
    ev = await inv.call("terraform_apply")
    assert ev.ok is False and "not a registered read-only tool" in ev.error


async def test_call_budget_is_enforced():
    inv = Investigator(_reg_with(), max_calls=3)
    results = await inv.run([{"tool": "list_things"} for _ in range(5)])
    assert [e.ok for e in results] == [True, True, True, False, False]
    assert "budget exhausted" in results[-1].error


async def test_spawned_subagent_shares_registry_and_budget_never_wider():
    inv = Investigator(_reg_with(), max_calls=4)
    await inv.run([{"tool": "list_things"}] * 3)          # parent uses 3 of 4
    child = inv.spawn()
    assert child.registry is inv.registry                  # same frozen surface — never wider
    first = await child.call("list_things")                # 4th call — fine
    second = await child.call("list_things")               # 5th — over the SHARED budget
    assert first.ok is True and second.ok is False
    assert "budget exhausted" in second.error


async def test_failed_read_is_evidence_not_a_crash():
    async def boom(**k):
        raise RuntimeError("prometheus is down")

    inv = Investigator(_reg_with("query_prometheus", boom))
    ev = await inv.call("query_prometheus", query="up")
    assert ev.ok is False and "prometheus is down" in ev.error


# ── SRE triage integration ─────────────────────────────────────────────────────────────────

async def test_sre_triage_collects_k8s_evidence_through_the_investigator(monkeypatch):
    from app.agents import sre as sre_mod
    from app.tools import kubernetes as k8s_mod

    class _FakeK8s:
        enabled = True
        async def list_deployments(self, namespace="default"):
            return [{"name": "orders-api"}, {"name": "checkout"}]
        async def list_pods(self, namespace="default"):
            return []

    class _Prom:
        enabled = False
        async def ping(self): return False

    class _Emitter:
        def __init__(self): self.lines = []
        async def console(self, stream, line): self.lines.append(line)

    monkeypatch.setattr(k8s_mod, "get_kubernetes", lambda s: _FakeK8s())
    monkeypatch.setattr(sre_mod, "get_prometheus", lambda s: _Prom())
    em = _Emitter()
    signals = await sre_mod._collect_telemetry(get_settings(), em)
    assert signals["deployments"] == ["orders-api", "checkout"]
    assert any("investigation (read-only): 2 deployments" in ln for ln in em.lines)
