"""MODSEED MS-2 — azure.vnet: full registration + seamless-operation contract.

C1 source invariants (NO NSG at all — never a world-open admin rule; no 0.0.0.0/0 literal;
NAT only with private subnets; RG semantics like azure-vm; no backend block), REAL terraform
fmt/validate, registration/synonyms/params/schema, the real plan-JSON policy predicate
(≥1 subnet, RFC1918, no-NSG), and the seamless-contract integration.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from app.agents import params, templates
from app.agents.templates import _azure_vnet_policy


def _modules_dir() -> Path:
    here = Path(__file__).resolve()
    for base in here.parents:
        cand = base / "infra" / "terraform-workspaces"
        if cand.is_dir():
            return cand
    raise FileNotFoundError("infra/terraform-workspaces not found")


def _src(module: str) -> str:
    return "\n".join(p.read_text(encoding="utf-8")
                     for p in sorted((_modules_dir() / module).glob("*.tf")))


# ── C1-style source assertions ─────────────────────────────────────────────────────────────

def test_azure_vnet_module_source_invariants():
    src = _src("azure-vnet")
    assert "azurerm_network_security" not in src               # NO NSG in the network module
    assert "0.0.0.0/0" not in src                              # no world-open literal anywhere
    assert 'backend "' not in src                              # no backend BLOCK (A3 injects)
    assert 'rg_name = var.resource_group != "" ? var.resource_group : "${var.name}-rg"' in src
    assert "azurerm_nat_gateway" in src and "azurerm_subnet_nat_gateway_association" in src
    assert "azurerm_subnet_route_table_association" in src     # name-based tier association
    assert '"22"' not in src and "3389" not in src             # no admin ports here
    assert 'location            = var.location' in src or "location = var.location" in src
    assert 'version = "~> 3.110"' in src                       # repo's current azurerm major


def test_azure_vnet_terraform_fmt_and_validate():
    import subprocess

    d = str(_modules_dir() / "azure-vnet")
    fmt = subprocess.run(["terraform", "fmt", "-check", "-diff"], cwd=d,
                         capture_output=True, text=True, timeout=120)
    assert fmt.returncode == 0, f"fmt -check failed:\n{fmt.stdout}{fmt.stderr}"
    init = subprocess.run(["terraform", "init", "-backend=false", "-input=false", "-no-color"],
                          cwd=d, capture_output=True, text=True, timeout=300)
    assert init.returncode == 0, f"init failed:\n{init.stderr[-800:]}"
    val = subprocess.run(["terraform", "validate", "-no-color"], cwd=d,
                         capture_output=True, text=True, timeout=120)
    assert val.returncode == 0, f"validate failed:\n{val.stdout}{val.stderr}"


# ── registration ───────────────────────────────────────────────────────────────────────────

def test_registered_and_routable_via_synonyms():
    t = templates.by_key("azure.vnet")
    assert t is not None and t.workspace == "azure-vnet"
    assert templates.select("azure", "vnet") is t
    assert templates.select("azure", "network") is t
    assert templates.select("gcp", "vnet") is None             # never cross-cloud


def test_schema_validates_and_rejects_public_space():
    t = templates.by_key("azure.vnet")
    v = t.schema(name="prod-vnet").model_dump()
    assert v["address_space"] == "10.20.0.0/16" and v["subnet_cidrs"] == ["10.20.1.0/24"]
    with pytest.raises(Exception):
        t.schema(name="x", address_space="52.0.0.0/16")        # public space refused
    with pytest.raises(Exception):
        t.schema(name="x", subnet_cidrs=["1.2.3.0/24"])


def test_params_ask_only_the_name():
    assert {p.name for p in params.missing_required("azure.vnet", {})} == {"name"}


def test_dep_slot_resource_group_family():
    """azure.vnet joins the RG slot family: one existing RG → used; default stated."""
    from app.agents.dependency import resolve_closure

    c = resolve_closure("azure.vnet", {"name": "prod-vnet"},
                        active=[{"name": "rg-payments", "cloud": "azure",
                                 "resource_type": "resource_group", "provider_id": "x",
                                 "attributes": {}}], message="")
    assert c.status == "complete" and c.inputs["resource_group"] == "rg-payments"
    c2 = resolve_closure("azure.vnet", {"name": "prod-vnet"}, active=[], message="")
    assert c2.status == "complete" and any("rg" in n for n in c2.notes)  # stated default


def test_policy_over_real_plan_json():
    good = [
        {"type": "azurerm_virtual_network", "after": {"address_space": ["10.20.0.0/16"]}},
        {"type": "azurerm_subnet", "after": {"address_prefixes": ["10.20.1.0/24"]}},
    ]
    by = {c["name"]: c for c in _azure_vnet_policy({}, good)}
    assert by["At least one subnet"]["passed"] is True
    assert by["RFC1918 address space"]["passed"] is True
    assert by["No NSG in the network module (no admin ingress here)"]["passed"] is True

    bad = [
        {"type": "azurerm_virtual_network", "after": {"address_space": ["52.0.0.0/16"]}},
        {"type": "azurerm_network_security_group", "after": {}},
    ]
    by_bad = {c["name"]: c for c in _azure_vnet_policy({}, bad)}
    assert by_bad["At least one subnet"]["passed"] is False
    assert by_bad["RFC1918 address space"]["passed"] is False
    assert by_bad["No NSG in the network module (no admin ingress here)"]["passed"] is False

    assert _azure_vnet_policy({"subnet_cidrs": ["10.0.0.0/24"]}, None)[1]["evaluated"] is False


# ── seamless-operation contract (faked runner, live datastores) ────────────────────────────

class _FakeRunner:
    def __init__(self, workspace, settings, state_workspace=None, run_id=None):
        self.workspace = workspace

    async def init(self, on_line=None, force=False):
        return {}

    async def plan(self, variables=None, destroy=False, on_line=None):
        return {"summary": {"add": 6, "change": 0, "destroy": 0},
                "diff": [{"sign": "+", "type": "azurerm_virtual_network",
                          "address": "azurerm_virtual_network.this", "actions": ["create"]}]}

    async def show_plan(self):
        return await self.plan()

    def planned_resources(self):
        return [
            {"type": "azurerm_virtual_network", "after": {"address_space": ["10.20.0.0/16"]}},
            {"type": "azurerm_subnet", "after": {"address_prefixes": ["10.20.1.0/24"]}},
        ]

    async def apply(self, on_line=None):
        return {"outputs": {"vnet_id": "/subscriptions/x/…/virtualNetworks/prod-vnet",
                            "vnet_name": "prod-vnet", "resource_group": "prod-vnet-rg",
                            "subnet_ids": ["/subscriptions/x/…/subnets/prod-vnet-public-0"],
                            "subnet_cidrs": ["10.20.1.0/24"], "nat_enabled": False}}


class _Emitter:
    def __init__(self): self.interrupts = []
    async def step(self, *a, **k): pass
    async def token(self, *a, **k): pass
    async def console(self, *a, **k): pass
    async def confidentiality(self, *a, **k): pass
    async def analysis(self, **k): pass
    async def params(self, *a, **k): pass
    async def reference(self, *a, **k): pass
    async def interrupt(self, payload): self.interrupts.append(payload)
    async def error(self, *a, **k): pass


class _NoopCG:
    def __init__(self, *a, **k): pass
    def __getattr__(self, _n):
        async def _f(*a, **k): return None
        return _f


async def test_seamless_contract_plan_to_inventory(live_db, live_redis, live_neo4j,
                                                   throwaway_org, monkeypatch):
    from app.agents import cloudops, inventory
    from app.graph_db import world_model

    monkeypatch.setattr(cloudops, "TerraformRunner", _FakeRunner)
    monkeypatch.setattr(cloudops, "ContextGraph", _NoopCG)

    async def _avail(settings, cloud, region, emitter):
        return {"available": True, "checks": []}
    monkeypatch.setattr(cloudops, "_availability", _avail)
    em = _Emitter()
    monkeypatch.setattr(cloudops, "emitter_of", lambda cfg: em)

    org = throwaway_org
    sid, rid = str(uuid.uuid4()), str(uuid.uuid4())
    from app.db.models import Run, Session as DbSession
    from app.db.session import session_scope
    async with session_scope() as s:
        s.add(DbSession(id=uuid.UUID(sid), org_id=uuid.UUID(org), title="ms2"))
        await s.flush()
        s.add(Run(id=uuid.UUID(rid), org_id=uuid.UUID(org), session_id=uuid.UUID(sid),
                  status="running", mode="apply"))

    state = {"message": "create a vnet in azure, name=prod-vnet", "org_id": org, "run_id": rid,
             "session_id": sid, "domain": "cloudops", "intent": "provision", "action": "create",
             "cloud": "azure", "resource": "network",
             "user": {"region": "eastus", "user_id": None}}
    try:
        out = await cloudops.cloudops_plan(state, {})
        assert out["approval_status"] == "pending"
        names = {c["name"]: c for c in em.interrupts[0]["policyChecks"]}
        assert names["At least one subnet"]["passed"] is True
        assert names["No NSG in the network module (no admin ingress here)"]["passed"] is True

        exec_state = {**state, **out, "approval_status": "approved",
                      "approver": {"can_execute": True}}
        result = await cloudops.cloudops_execute(exec_state, {})
        assert result["outcome"]["status"] == "applied"
        rows = await inventory.list_active(org)
        assert any(r["name"] == "prod-vnet" and r["resource_type"] == "vnet" for r in rows)
        wm = await world_model.list_active(org)
        assert any(r["name"] == "prod-vnet" for r in wm)
    finally:
        from sqlalchemy import delete
        from app.db.models import Resource
        async with session_scope() as s:
            await s.execute(delete(Resource).where(Resource.org_id == uuid.UUID(org)))
            await s.execute(delete(Run).where(Run.id == uuid.UUID(rid)))
            await s.execute(delete(DbSession).where(DbSession.id == uuid.UUID(sid)))
        await world_model._run("MATCH (r:Resource {org_id:$o}) DETACH DELETE r", o=org)
