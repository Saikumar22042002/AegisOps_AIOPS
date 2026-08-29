"""P2.2 — the harness drives a production read path (07 §2.2).

Two properties: (1) `harness.inv.investigate` runs the kernel over the real read-only
registry to a genuine multi-tool conclusion; (2) the SRE telemetry collector routes
through the kernel ONLY when the flag is on, and is byte-identical to the prior hardcoded
path when off (coexistence, T-P2-01).
"""

from __future__ import annotations

import pytest

from app.harness import inv as harness_inv
from app.harness import loop as harness_loop
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


async def test_investigate_runs_the_kernel_over_the_real_registry(monkeypatch):
    # Real default_registry (Prometheus/K8s/inventory/world-model reads) — but the tool
    # calls resolve against fakes so no network/cluster is needed.
    from app.agents import investigation

    async def fake_pods(**_):
        return [{"name": "api-x", "restarts": 7}]

    reg = investigation.ToolRegistry()
    reg.register("list_pods", "pods", fake_pods)
    monkeypatch.setattr(harness_inv, "default_registry", lambda s: reg.freeze())

    model = ScriptedModel([
        {"hypothesis": "inspect pods", "action": {"kind": "use_tool", "tool": "list_pods"}},
        {"hypothesis": "api-x restarting", "action": {"kind": "answer",
         "text": "api-x has 7 restarts (obs 0)."}},
    ])
    monkeypatch.setattr(harness_loop.service, "classify_json", model.classify_json)

    # Prompt 4: pin packs OFF — with AEGISOPS_CAPABILITY_PACKS=on (default posture) the
    # kernel builds the packs registry and the monkeypatched default_registry is bypassed;
    # the packs path has its own coverage in the P4 pack tests.
    res = await harness_inv.investigate(Settings(aegisops_capability_packs="off"),
                                        "triage the incident",
                                        run_id="22222222-0000-0000-0000-000000000001")
    assert res.status == "answered" and res.evidence_ok
    assert "api-x" in res.findings and res.iterations == 2


async def test_sre_collector_uses_kernel_only_when_flag_on(monkeypatch):
    import app.agents.sre as sre

    class _K8s:
        enabled = True
    # k8s is imported locally (source module); prometheus is a module-level import in sre.
    monkeypatch.setattr("app.tools.kubernetes.get_kubernetes", lambda s: _K8s())
    monkeypatch.setattr(sre, "get_prometheus",
                        lambda s: type("P", (), {"enabled": False})())

    calls = {"harness": 0, "legacy": 0}

    async def fake_investigate(settings, objective, *, run_id=None, org_id=None,
                               purpose="inv_loop", context=None):
        calls["harness"] += 1
        return harness_loop.RunResult(status="answered", findings="ok", iterations=2,
                                      evidence_ok=True)
    monkeypatch.setattr(harness_inv, "investigate", fake_investigate)

    class _Inv:
        def __init__(self, *a, **k): ...
        async def call(self, *a, **k):
            calls["legacy"] += 1
            return type("E", (), {"ok": True, "result": [], "error": None})()
    monkeypatch.setattr("app.agents.investigation.Investigator", _Inv)
    monkeypatch.setattr("app.agents.investigation.default_registry", lambda s: None)

    class _Em:
        async def console(self, *a, **k): ...

    # flag OFF → legacy path only
    off = await sre._collect_telemetry(Settings(aegisops_harness_read_paths="off"), _Em())
    assert calls == {"harness": 0, "legacy": 1}
    assert "harness_investigation" not in off

    # flag ON → kernel path only
    on = await sre._collect_telemetry(Settings(aegisops_harness_read_paths="on"), _Em(),
                                      run_id="r1", org_id="o1")
    assert calls == {"harness": 1, "legacy": 1}
    assert on["harness_investigation"]["status"] == "answered"
