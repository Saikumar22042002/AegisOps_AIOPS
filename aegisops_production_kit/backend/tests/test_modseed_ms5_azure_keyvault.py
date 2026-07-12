"""MODSEED MS-5 — azure.keyvault: full registration + card statements + seamless contract.

The vault, never secret values. network_default_action=Allow is STATED on the card; the
destroy card carries the soft-delete/purge-protection semantics.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from app.agents import params, templates
from app.agents.dependency import resolve_closure
from app.agents.templates import _azure_keyvault_policy, apply_env_defaults


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


def test_azure_keyvault_module_source_invariants():
    src = _src("azure-keyvault")
    assert 'sku_name                   = "standard"' in src
    assert "soft_delete_retention_days = var.soft_delete_days" in src
    assert "purge_protection_enabled   = var.purge_protection" in src
    assert 'bypass         = "AzureServices"' in src
    assert "data.azurerm_client_config.current.object_id" in src   # current-SP policy
    assert 'key_type     = "RSA"' in src and "key_size     = 2048" in src
    assert "secret_value" not in src and "azurerm_key_vault_secret" not in src  # never secrets
    assert 'rg_name = var.resource_group != "" ? var.resource_group : "${var.name}-rg"' in src
    assert 'backend "' not in src and 'version = "~> 3.110"' in src
    assert "var.soft_delete_days >= 7" in src                      # module-level bound


def test_azure_keyvault_terraform_fmt_and_validate():
    import subprocess

    d = str(_modules_dir() / "azure-keyvault")
    fmt = subprocess.run(["terraform", "fmt", "-check", "-diff"], cwd=d,
                         capture_output=True, text=True, timeout=120)
    assert fmt.returncode == 0, f"fmt -check failed:\n{fmt.stdout}{fmt.stderr}"
    init = subprocess.run(["terraform", "init", "-backend=false", "-input=false", "-no-color"],
                          cwd=d, capture_output=True, text=True, timeout=300)
    assert init.returncode == 0, f"init failed:\n{init.stderr[-800:]}"
    val = subprocess.run(["terraform", "validate", "-no-color"], cwd=d,
                         capture_output=True, text=True, timeout=120)
    assert val.returncode == 0, f"validate failed:\n{val.stdout}{val.stderr}"


def test_registered_and_routable_via_synonyms():
    t = templates.by_key("azure.keyvault")
    assert t is not None and t.workspace == "azure-keyvault"
    for syn in ("keyvault", "key_vault", "kv", "vault"):
        assert templates.select("azure", syn) is t, syn
    assert templates.select("aws", "vault") is not t               # never cross-cloud


def test_schema_bounds():
    t = templates.by_key("azure.keyvault")
    v = t.schema(name="app-vault").model_dump()
    assert v["soft_delete_days"] == 90 and v["purge_protection"] is True
    with pytest.raises(Exception):
        t.schema(name="x1", soft_delete_days=3)
    with pytest.raises(Exception):
        t.schema(name="x1", network_default_action="Maybe")
    with pytest.raises(Exception):
        t.schema(name="ab")                                        # min_length 3


def test_params_ask_only_the_name():
    assert {p.name for p in params.missing_required("azure.keyvault", {})} == {"name"}


def test_allow_default_action_is_stated_on_the_card():
    v = {"network_default_action": "Allow"}
    notes = apply_env_defaults("azure.keyvault", v, "Staging")
    assert notes and "ALL" in notes[0]                             # stated, never silent
    assert apply_env_defaults("azure.keyvault", {"network_default_action": "Deny"}, "x") == []


def test_dep_slot_rg_family():
    c = resolve_closure("azure.keyvault", {"name": "app-vault"}, active=[], message="")
    assert c.status == "complete" and any("rg" in n for n in c.notes)  # stated default


def test_destroy_note_declares_soft_delete_semantics():
    note = templates.by_key("azure.keyvault").destroy_note
    assert note and "soft-delete" in note and "CANNOT be permanently purged" in note


def test_policy_over_real_plan_json():
    good = [{"type": "azurerm_key_vault",
             "after": {"soft_delete_retention_days": 90, "purge_protection_enabled": True,
                       "network_acls": [{"bypass": "AzureServices", "default_action": "Allow"}]}}]
    by = {c["name"]: c for c in _azure_keyvault_policy({"purge_protection": True}, good)}
    assert by["Soft delete >= 7 days"]["passed"] is True
    assert by["Purge protection as approved"]["passed"] is True
    assert by["AzureServices bypass on network ACLs"]["passed"] is True

    bad = [{"type": "azurerm_key_vault",
            "after": {"soft_delete_retention_days": 3, "purge_protection_enabled": False,
                      "network_acls": [{"bypass": "None"}]}}]
    by_bad = {c["name"]: c for c in _azure_keyvault_policy({"purge_protection": True}, bad)}
    assert by_bad["Soft delete >= 7 days"]["passed"] is False
    assert by_bad["Purge protection as approved"]["passed"] is False
    assert by_bad["AzureServices bypass on network ACLs"]["passed"] is False


class _FakeRunner:
    def __init__(self, workspace, settings, state_workspace=None, run_id=None):
        self.workspace = workspace
    async def init(self, on_line=None, force=False): return {}
    async def plan(self, variables=None, destroy=False, on_line=None):
        return {"summary": {"add": 3, "change": 0, "destroy": 0},
                "diff": [{"sign": "+", "type": "azurerm_key_vault",
                          "address": "azurerm_key_vault.this", "actions": ["create"]}]}
    async def show_plan(self): return await self.plan()
    def planned_resources(self):
        return [{"type": "azurerm_key_vault",
                 "after": {"soft_delete_retention_days": 90, "purge_protection_enabled": True,
                           "network_acls": [{"bypass": "AzureServices",
                                             "default_action": "Allow"}]}}]
    async def apply(self, on_line=None):
        return {"outputs": {"vault_id": "/subscriptions/x/.../vaults/app-vault",
                            "vault_uri": "https://app-vault.vault.azure.net/",
                            "resource_group": "app-vault-rg", "purge_protection": True,
                            "soft_delete_days": 90, "key_names": []}}


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
        s.add(DbSession(id=uuid.UUID(sid), org_id=uuid.UUID(org), title="ms5"))
        await s.flush()
        s.add(Run(id=uuid.UUID(rid), org_id=uuid.UUID(org), session_id=uuid.UUID(sid),
                  status="running", mode="apply"))

    state = {"message": "create a key vault in azure, name=app-vault", "org_id": org,
             "run_id": rid, "session_id": sid, "domain": "cloudops", "intent": "provision",
             "action": "create", "cloud": "azure", "resource": "vault",
             "user": {"region": "eastus", "user_id": None}}
    try:
        out = await cloudops.cloudops_plan(state, {})
        assert out["approval_status"] == "pending"
        names = {c["name"]: c for c in em.interrupts[0]["policyChecks"]}
        assert names["Soft delete >= 7 days"]["passed"] is True
        # The Allow default action is STATED on the card's defaults.
        defaults = out["plan_json"]["defaults"]
        assert any("ALL" in d["value"] for d in defaults if d["name"] == "Environment default")

        exec_state = {**state, **out, "approval_status": "approved",
                      "approver": {"can_execute": True}}
        result = await cloudops.cloudops_execute(exec_state, {})
        assert result["outcome"]["status"] == "applied"
        rows = await inventory.list_active(org)
        row = next(r for r in rows if r["name"] == "app-vault")
        assert row["resource_type"] == "keyvault"
        assert row["attributes"]["vault_uri"].startswith("https://")
        wm = await world_model.list_active(org)
        assert any(r["name"] == "app-vault" for r in wm)
    finally:
        from sqlalchemy import delete
        from app.db.models import Resource
        async with session_scope() as s:
            await s.execute(delete(Resource).where(Resource.org_id == uuid.UUID(org)))
            await s.execute(delete(Run).where(Run.id == uuid.UUID(rid)))
            await s.execute(delete(DbSession).where(DbSession.id == uuid.UUID(sid)))
        await world_model._run("MATCH (r:Resource {org_id:$o}) DETACH DELETE r", o=org)
