"""B4 — verification reconciles Azure/GCP (not just AWS), and always terminates.

Unit: `_reconcile_checks` gets a real per-cloud live check for Azure VMs (Azure Compute list) and
GCP VMs (Compute aggregated list), matched by the resource's stable name; a missing resource
yields a real FAILED check, not a fake pass. Plus a slow-SDK case proving `verify()` warns within
its bound rather than hanging (the N-01 guarantee, per cloud).
"""

from __future__ import annotations

import asyncio

import pytest

from app.agents import finalize as fz
from app.agents.events import Emitter, RunChannel


def _state(cloud: str, name: str, resource: str = "vm"):
    return {"run_id": "vcc", "org_id": "o", "cloud": cloud, "resource": resource,
            "parsed_inputs": {"name": name}, "outcome": {}}


class _FakeAzure:
    enabled = True

    def __init__(self, vms):
        self._vms = vms

    async def list_vms(self):
        return self._vms


class _FakeGcp:
    enabled = True

    def __init__(self, insts):
        self._insts = insts

    async def list_all_instances(self):
        return self._insts


async def test_azure_vm_reconciled_present(monkeypatch):
    monkeypatch.setattr(fz.azure_tool, "get_azure",
                        lambda s: _FakeAzure([{"name": "web-01", "location": "eastus", "size": "Standard_B2s"}]))
    checks = await fz._reconcile_checks(_state("azure", "web-01"), {"public_ip": "20.0.0.1"})
    az = next(c for c in checks if "Azure" in c["name"])
    assert az["passed"] is True and az["detail"] == "eastus"


async def test_azure_vm_missing_is_a_real_failed_check(monkeypatch):
    monkeypatch.setattr(fz.azure_tool, "get_azure", lambda s: _FakeAzure([{"name": "other", "location": "eastus"}]))
    checks = await fz._reconcile_checks(_state("azure", "web-01"), {"public_ip": "20.0.0.1"})
    az = next(c for c in checks if "Azure" in c["name"])
    assert az["passed"] is False and az["detail"] == "not found"


async def test_gcp_vm_reconciled_running(monkeypatch):
    monkeypatch.setattr(fz.gcp_tool, "get_gcp",
                        lambda s: _FakeGcp([{"name": "gce-01", "status": "RUNNING", "zone": "us-central1-a"}]))
    checks = await fz._reconcile_checks(_state("gcp", "gce-01"), {"public_ip": "34.0.0.1"})
    g = next(c for c in checks if "Compute API" in c["name"])
    assert g["passed"] is True and g["detail"] == "RUNNING"


async def test_gcp_vm_terminated_fails(monkeypatch):
    monkeypatch.setattr(fz.gcp_tool, "get_gcp",
                        lambda s: _FakeGcp([{"name": "gce-01", "status": "TERMINATED"}]))
    checks = await fz._reconcile_checks(_state("gcp", "gce-01"), {"public_ip": "34.0.0.1"})
    g = next(c for c in checks if "Compute API" in c["name"])
    assert g["passed"] is False and g["detail"] == "TERMINATED"


async def test_verify_bounded_when_azure_sdk_stalls(monkeypatch):
    """N-01, cross-cloud: a hanging Azure list must yield a warned verification within verify()'s
    own timeout — never an infinite spinner."""
    class _Hang:
        enabled = True

        async def list_vms(self):
            await asyncio.sleep(3600)

    monkeypatch.setattr(fz.azure_tool, "get_azure", lambda s: _Hang())
    monkeypatch.setattr(fz, "_VERIFY_TIMEOUT_S", 1)  # tighten the bound for the test
    state = _state("azure", "web-01")
    state["outcome"] = {"status": "applied", "outputs": {"public_ip": "20.0.0.1", "login_user": "azureuser"}}
    cfg = {"configurable": {"emitter": Emitter(RunChannel("vcc"))}}
    out = await asyncio.wait_for(fz.verify(state, cfg), timeout=15)
    checks = next((tr["verify"] for tr in out.get("tool_results", []) if "verify" in tr), [])
    assert checks, "verify must record its checks even when the cloud SDK stalls"
    assert any(not c["passed"] for c in checks), "a timeout is surfaced as a warned/failed check"
