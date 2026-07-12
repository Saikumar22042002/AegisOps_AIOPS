"""MODSEED MS-8 — azure-postgres → azure.db multi-engine (postgresql/mysql/mssql) with
optional HA / geo-redundant backup / delegated-subnet private access. KEEPs the generated
random_password. B1 is proven by the workspace's committed `terraform test` (mock providers,
offline): old-shape stored inputs render the EXACT pre-enhancement postgres plan, and
`moved` blocks migrate the old resource addresses so real state re-plans as a no-op rename.
B3: the workspace DIR NAME is unchanged and day-2 destroy on an old-shape inventory row
still works — proven end-to-end here.
"""

from __future__ import annotations

import re
import subprocess
import uuid
from pathlib import Path

import pytest

from app.agents import dependency, params, templates
from app.agents.templates import _azure_db_policy
from app.schemas import workflows as wf
from app.schemas.workflows import AzureDBInputs


def _ws() -> Path:
    here = Path(__file__).resolve()
    for base in here.parents:
        cand = base / "infra" / "terraform-workspaces"
        if cand.is_dir():
            return cand / "azure-postgres"
    raise FileNotFoundError("infra/terraform-workspaces not found")


def _src() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted(_ws().glob("*.tf")))


def test_source_invariants_multi_engine_and_moved_blocks():
    src = _src()
    # address migration for existing state (B1): the old unkeyed addresses move
    assert re.search(r"moved\s*\{\s*from\s*=\s*azurerm_postgresql_flexible_server\.this\s*"
                     r"to\s*=\s*azurerm_postgresql_flexible_server\.this\[\"postgresql\"\]", src)
    assert 'from = azurerm_postgresql_flexible_server_firewall_rule.azure' in src
    # three engine families, each for_each-gated on the selected engine
    assert 'contains(["postgresql", "mysql", "mssql"], var.engine)' in src
    assert 'resource "azurerm_mysql_flexible_server" "this"' in src
    assert 'resource "azurerm_mssql_server" "this"' in src
    assert 'resource "azurerm_mssql_database" "this"' in src
    # the generated admin password is KEPT and unconditional (shared by every engine)
    assert re.search(r'resource "random_password" "admin" \{\n  length', src)
    assert "administrator_password        = random_password.admin.result" in src
    assert "administrator_login_password  = random_password.admin.result" in src
    # mssql hardening + honest engine limits
    assert 'minimum_tls_version           = "1.2"' in src
    assert src.count("precondition {") == 2
    # geo-backup is secure-by-default at MODULE level (the schema defaults it off, B2)
    assert re.search(r'variable "geo_redundant_backup" \{[^}]*default     = true', src, re.S)
    # storage_mb maps to the mysql flexible storage block
    assert "size_gb = max(20, floor(var.storage_mb / 1024))" in src
    # pins + backend unchanged
    assert 'version = "~> 3.110"' in src and 'version = "~> 3.6"' in src
    assert 'backend "local" {}' in src and 'backend "pg"' not in src


def test_fmt_init_validate_real_terraform():
    d = str(_ws())
    fmt = subprocess.run(["terraform", "fmt", "-check", "-diff"], cwd=d,
                         capture_output=True, text=True, timeout=120)
    assert fmt.returncode == 0, f"fmt -check failed:\n{fmt.stdout}{fmt.stderr}"
    init = subprocess.run(["terraform", "init", "-reconfigure", "-input=false", "-no-color"],
                          cwd=d, capture_output=True, text=True, timeout=300)
    assert init.returncode == 0, f"init failed:\n{init.stderr[-800:]}"
    val = subprocess.run(["terraform", "validate", "-no-color"], cwd=d,
                         capture_output=True, text=True, timeout=120)
    assert val.returncode == 0, f"validate failed:\n{val.stdout}{val.stderr}"


def test_b1_gate_via_native_terraform_test_with_mock_providers():
    """The committed tests/b1_backcompat.tftest.hcl IS the B1/B2 gate: old-shape inputs →
    exactly the old postgres plan; each engine renders only its family; HA/private access
    render only when asked; mssql rejects HA via precondition. Offline (mock providers)."""
    res = subprocess.run(["terraform", "test", "-no-color"], cwd=str(_ws()),
                         capture_output=True, text=True, timeout=600)
    assert res.returncode == 0, f"terraform test failed:\n{res.stdout[-2000:]}{res.stderr[-500:]}"
    assert "6 passed, 0 failed" in res.stdout


def test_schema_backcompat_defaults_and_engine_rules():
    v = AzureDBInputs(name="db1").model_dump()
    assert v["engine"] == "postgresql" and v["pg_version"] == "15"
    assert v["ha_enabled"] is False and v["geo_redundant_backup"] is False    # B2
    assert v["delegated_subnet_id"] == "" and v["private_dns_zone_id"] == ""
    assert AzureDBInputs(name="db1", engine="postgres").engine == "postgresql"  # shorthand
    with pytest.raises(Exception):
        AzureDBInputs(name="db1", engine="oracle")
    with pytest.raises(Exception):
        AzureDBInputs(name="db1", storage_mb=1024)                            # floor kept
    with pytest.raises(Exception):
        AzureDBInputs(name="db1", engine="mssql", ha_enabled=True)
    with pytest.raises(Exception):
        AzureDBInputs(name="db1", engine="mssql", delegated_subnet_id="/x",
                      private_dns_zone_id="/y")
    with pytest.raises(Exception):
        AzureDBInputs(name="db1", delegated_subnet_id="/x")                   # needs DNS zone
    assert wf.AzurePostgresInputs is AzureDBInputs                            # old import lives


def test_registration_alias_and_synonyms():
    t = templates.by_key("azure.db")
    assert t is not None and t.workspace == "azure-postgres"                  # B3: dir immutable
    assert templates.by_key("azure.postgres") is t                            # old key alive
    for syn in ("postgres", "postgresql", "database", "db", "sql",
                "mysql", "mssql", "sqlserver", "sql_server"):
        assert templates.select("azure", syn) is t, syn
    assert templates.select("aws", "mysql").key == "aws.rds"                  # per-cloud coexistence
    assert templates.select("gcp", "postgres").key == "gcp.cloudsql"
    assert "azure.db" in dependency.SLOTS                                     # RG slot moved keys


def test_params_ask_only_the_name():
    assert {p.name for p in params.missing_required("azure.db", {})} == {"name"}


def test_policy_engine_aware_checks():
    by = {c["name"]: c for c in _azure_db_policy({"engine": "postgresql", "pg_version": "15"})}
    assert by["Approved engine"]["passed"] is True
    assert by["Approved PostgreSQL version"]["passed"] is True

    mssql_plan = [{"type": "azurerm_mssql_server", "after": {"minimum_tls_version": "1.2"}}]
    by_ms = {c["name"]: c for c in _azure_db_policy({"engine": "mssql"}, mssql_plan)}
    assert by_ms["TLS 1.2 minimum (SQL Server)"]["passed"] is True
    assert "Approved PostgreSQL version" not in by_ms

    by_ha = {c["name"]: c for c in _azure_db_policy({"engine": "mysql", "ha_enabled": True})}
    assert by_ha["Zone-redundant HA"]["passed"] is True

    by_priv = {c["name"]: c for c in _azure_db_policy(
        {"engine": "postgresql", "delegated_subnet_id": "/x"})}
    assert by_priv["Private access (delegated subnet)"]["passed"] is False    # DNS zone missing


def test_geo_backup_waiver_is_gone_and_no_ms8_references():
    cfg = (_ws() / ".checkov.yaml").read_text(encoding="utf-8")
    assert "CKV_AZURE_136" not in cfg
    assert "MS-8" not in cfg


async def test_b3_day2_destroy_on_an_old_shape_inventory_row(live_db, live_redis, live_neo4j,
                                                             throwaway_org, monkeypatch):
    """An inventory row created BEFORE the enhancement (workspace azure-postgres,
    resource_type 'postgres', old-shape inputs) still resolves and plans its teardown —
    the synonym lands on azure.db, the runner takes the ROW's workspace, and the old
    inputs pass the new schema at B2 defaults."""
    from app.agents import cloudops

    seen: dict = {}

    class _DestroyRunner:
        def __init__(self, workspace, settings, state_workspace=None, run_id=None):
            seen["workspace"] = workspace
        async def init(self, on_line=None, force=False): return {}
        async def plan(self, variables=None, destroy=False, on_line=None):
            if variables is not None:                       # show_plan re-calls with no args
                seen["variables"] = variables
            return {"summary": {"add": 0, "change": 0, "destroy": 4},
                    "diff": [{"sign": "-", "type": "azurerm_postgresql_flexible_server",
                              "address": "azurerm_postgresql_flexible_server.this",
                              "actions": ["delete"]}]}
        async def show_plan(self): return await self.plan()
        def planned_resources(self): return []

    class _Emitter:
        def __init__(self): self.analyses = []; self.interrupts = []
        async def step(self, *a, **k): pass
        async def token(self, *a, **k): pass
        async def console(self, *a, **k): pass
        async def confidentiality(self, *a, **k): pass
        async def analysis(self, **k): self.analyses.append(k)
        async def interrupt(self, payload): self.interrupts.append(payload)
        async def error(self, *a, **k): pass

    monkeypatch.setattr(cloudops, "TerraformRunner", _DestroyRunner)
    em = _Emitter()
    monkeypatch.setattr(cloudops, "emitter_of", lambda cfg: em)

    org = throwaway_org
    sid = str(uuid.uuid4())
    from app.db.models import Resource, Session as DbSession
    from app.db.session import session_scope
    async with session_scope() as s:
        s.add(DbSession(id=uuid.UUID(sid), org_id=uuid.UUID(org), title="ms8"))
        s.add(Resource(org_id=uuid.UUID(org), name="legacy-pg", cloud="azure",
                       resource_type="postgres", workspace="azure-postgres",
                       provider_id="srv-1", status="active",
                       inputs={"name": "legacy-pg", "location": "eastus",
                               "admin_username": "pgadmin", "sku_name": "B_Standard_B1ms",
                               "storage_mb": 32768, "pg_version": "15",
                               "resource_group": ""}))

    state = {"message": "destroy legacy-pg", "org_id": org, "run_id": str(uuid.uuid4()),
             "session_id": sid, "domain": "cloudops", "action": "destroy",
             "target": "legacy-pg", "user": {"user_id": None}}
    try:
        out = await cloudops.cloudops_plan(state, {})
        assert out["approval_status"] == "pending"
        assert seen["workspace"] == "azure-postgres"                    # the ROW's workspace
        assert seen["variables"]["engine"] == "postgresql"              # B2 default filled
        assert seen["variables"]["geo_redundant_backup"] is False
        assert seen["variables"]["pg_version"] == "15"
    finally:
        from sqlalchemy import delete
        async with session_scope() as s:
            await s.execute(delete(Resource).where(Resource.org_id == uuid.UUID(org)))
            await s.execute(delete(DbSession).where(DbSession.id == uuid.UUID(sid)))
