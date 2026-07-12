"""U2 — SRE real signals + real K8s remediation.

The deploy signal is a real Prometheus query (not the old hardcoded recent_deploy=True), and
when a cluster is configured the remediation actually executes (restart/scale/rollback) and
reports applied:True with the real result — else it stays "proposed, not executed".
"""

from __future__ import annotations

import uuid

import pytest

from app.agents import sre as sre_mod


class _NoopCG:
    def __init__(self, *a, **k): pass
    def __getattr__(self, _n):
        async def _f(*a, **k): return None
        return _f


class _Emitter:
    def __init__(self): self.tokens = []
    async def step(self, *a, **k): pass
    async def token(self, t): self.tokens.append(t)
    async def console(self, *a, **k): pass
    async def error(self, *a, **k): pass


class _FakeK8s:
    enabled = True
    def __init__(self, fail=False): self.calls = []; self._fail = fail
    async def list_deployments(self, ns="default"): return [{"name": "orders-api", "replicas": 3}]
    async def restart_deployment(self, name, ns="default"):
        if self._fail: from app.tools.kubernetes import KubernetesError; raise KubernetesError("boom")
        self.calls.append(("restart", name, ns)); return {"action": "restart", "name": name}
    async def scale_deployment(self, name, replicas, ns="default"):
        self.calls.append(("scale", name, replicas)); return {"action": "scale", "replicas": replicas}
    async def rollback_deployment(self, name, ns="default"):
        self.calls.append(("rollback", name)); return {"action": "rollback", "name": name}


def _state(action, target="orders-api"):
    return {"run_id": str(uuid.uuid4()), "org_id": str(uuid.uuid4()),
            "parsed_inputs": {"decision": {"action": action, "target": target}}}


async def _run(monkeypatch, action, k8s):
    monkeypatch.setattr(sre_mod, "ContextGraph", _NoopCG)
    monkeypatch.setattr(sre_mod, "get_kubernetes", lambda s: k8s)
    return await sre_mod.sre_execute(_state(action), {"configurable": {"emitter": _Emitter()}})


async def test_restart_executes_real_k8s_action(monkeypatch):
    k8s = _FakeK8s()
    out = await _run(monkeypatch, "restart", k8s)
    assert out["outcome"]["status"] == "remediated" and out["outcome"]["applied"] is True
    assert ("restart", "orders-api", "default") in k8s.calls


async def test_scale_out_patches_replicas_plus_one(monkeypatch):
    k8s = _FakeK8s()
    out = await _run(monkeypatch, "scale_out", k8s)
    assert out["outcome"]["applied"] is True
    assert ("scale", "orders-api", 4) in k8s.calls  # current 3 + 1


async def test_rollback_executes_real_k8s_action(monkeypatch):
    k8s = _FakeK8s()
    out = await _run(monkeypatch, "rollback", k8s)
    assert out["outcome"]["applied"] is True
    assert ("rollback", "orders-api") in k8s.calls


async def test_failed_remediation_reported_truthfully(monkeypatch):
    out = await _run(monkeypatch, "restart", _FakeK8s(fail=True))
    assert out["outcome"]["status"] == "remediation_failed" and out["outcome"]["applied"] is False


async def test_investigate_never_mutates(monkeypatch):
    k8s = _FakeK8s()
    out = await _run(monkeypatch, "investigate", k8s)
    assert out["outcome"]["applied"] is False and out["outcome"]["status"] == "proposed_not_executed"
    assert k8s.calls == []


async def test_no_cluster_stays_proposed_not_executed(monkeypatch):
    class _Disabled:
        enabled = False
    out = await _run(monkeypatch, "restart", _Disabled())
    assert out["outcome"]["status"] == "proposed_not_executed" and out["outcome"]["applied"] is False


async def test_recent_deploy_signal_is_real_not_hardcoded(monkeypatch):
    class _Prom:
        enabled = True
        async def ping(self): return True
        async def scalar(self, q, default=0.0):
            return 2.0 if "observed_generation" in q else 0.0
    monkeypatch.setattr(sre_mod, "get_prometheus", lambda s: _Prom())
    signals = await sre_mod._collect_telemetry(sre_mod.get_settings(), _Emitter())
    assert signals["recent_deploy"] is True  # from a real query result, not a constant


async def test_recent_deploy_false_without_prometheus(monkeypatch):
    class _Off:
        enabled = False
        async def ping(self): return False
    monkeypatch.setattr(sre_mod, "get_prometheus", lambda s: _Off())
    signals = await sre_mod._collect_telemetry(sre_mod.get_settings(), _Emitter())
    assert signals["recent_deploy"] is False  # never assume a deploy happened
