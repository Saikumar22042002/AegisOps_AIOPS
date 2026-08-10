"""P0 governance stamping + HITL default + worker role + Redis posture tests."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.security.governance_stamp import governance_stamp, stamped
from app.settings import Settings, get_settings

# ── governance stamping (D9/F-9: posture can never drift silently) ────────────────────────

def test_stamp_carries_the_governance_flags():
    s = get_settings()
    g = governance_stamp(s)
    for key in ("app_env", "role", "tenancy", "event_bus", "exec_loop",
                "approval_model", "default_execution_mode", "drift"):
        assert key in g


def test_stamp_is_additive_and_never_mutates_the_payload():
    payload = {"kind": "approval", "runId": "r1", "plan": {"steps": []}}
    out = stamped(payload)
    assert out["kind"] == "approval" and out["runId"] == "r1" and out["plan"] == {"steps": []}
    assert "governance" in out and "governance" not in payload


def test_healthz_exposes_the_governance_posture(client: TestClient):
    body = client.get("/healthz").json()
    assert body["status"] == "ok"
    assert "approval_model" in body["governance"]


# ── HITL approval model (operator-directed correction) ────────────────────────────────────

def test_four_eyes_concept_is_removed():
    """The four-eyes / second-approver concept does not exist in AegisOps — not as a
    setting, not as a stamp key. Single-user HITL (initiator == approver) is THE model;
    the positive pin is test_tenancy.test_initiator_may_approve_their_own_run."""
    assert "aegisops_four_eyes_for_production" not in Settings.model_fields
    assert "four_eyes_for_production" not in governance_stamp(get_settings())


def test_stamp_names_the_approval_model_honestly():
    assert governance_stamp(get_settings())["approval_model"] == "hitl"


def test_approval_interrupt_payloads_are_stamped_at_the_source():
    """Both interrupt sites route their payload through stamped() (additive-only rule)."""
    import inspect

    from app.agents import approval as approval_mod
    from app.agents import exec_loop as exec_loop_mod
    assert "stamped(" in inspect.getsource(approval_mod)
    assert "stamped(" in inspect.getsource(exec_loop_mod)


# ── worker foundation (F-18: exactly one sweep owner) ─────────────────────────────────────

class _SentinelReconciler:
    def __init__(self):
        self.started = False

    async def start(self):
        self.started = True

    async def stop(self):
        pass


@pytest.mark.parametrize(("role", "expect_started"), [("api", False), ("all", True), ("worker", True)])
def test_role_gates_background_ownership(monkeypatch, role, expect_started):
    import app.agents.reconciler as rec_mod
    s = get_settings()
    monkeypatch.setattr(s, "aegisops_role", role)
    monkeypatch.setattr(s, "aegisops_reconciler", "on")
    sentinel = _SentinelReconciler()
    monkeypatch.setattr(rec_mod, "get_reconciler", lambda: sentinel)
    with TestClient(app):
        pass
    assert sentinel.started is expect_started


# ── Redis posture (no silent production fallback) ─────────────────────────────────────────

def test_memory_bus_outside_local_refuses_to_start(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "app_env", "staging")
    monkeypatch.setattr(s, "aegisops_event_bus", "memory")
    with pytest.raises(RuntimeError, match="dev-only"):
        with TestClient(app):
            pass


def test_memory_bus_in_local_dev_still_works(client: TestClient):
    # The unit tier itself runs on the explicit dev memory mode — this client works.
    assert client.get("/healthz").status_code == 200


# ── /metrics auth (F-16) ──────────────────────────────────────────────────────────────────

def test_metrics_open_only_in_local_when_unset(client: TestClient, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "aegisops_metrics_token", "")
    monkeypatch.setattr(s, "app_env", "local")
    assert client.get("/metrics").status_code == 200


def test_metrics_requires_token_outside_local(client: TestClient, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "aegisops_metrics_token", "")
    monkeypatch.setattr(s, "app_env", "staging")
    assert client.get("/metrics").status_code == 403


def test_metrics_bearer_token_enforced(client: TestClient, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "aegisops_metrics_token", "t0k3n-test-only")
    assert client.get("/metrics").status_code == 401
    ok = client.get("/metrics", headers={"Authorization": "Bearer t0k3n-test-only"})
    assert ok.status_code == 200 and "aegisops_api_requests_total" in ok.text


def test_rate_limiter_uses_process_store_only_in_memory_mode():
    """In the redis coordination posture the limiter is constructed with the shared
    storage; the unit tier runs memory mode → no storage kwargs (see app/ratelimit.py)."""
    from app import ratelimit
    assert ratelimit._settings.aegisops_event_bus == "memory"
    assert ratelimit._storage_kwargs == {}
