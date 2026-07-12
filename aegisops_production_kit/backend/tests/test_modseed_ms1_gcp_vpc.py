"""MODSEED MS-1 — gcp.vpc: full registration + seamless-operation contract.

Covers the MODSEED non-negotiables for this module: C1-style source assertions (no world-open
ingress, custom mode, NAT logging, no backend block, no hardcoded region), REAL `terraform
fmt/validate` on the directory, the registry↔disk consistency test (new — binds ALL modules),
the real plan-JSON policy predicate, params/synonym routing, and the seamless-contract
integration (faked runner, live datastores): plan → real policy checks → approval card →
apply → inventory row + world-model node.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from app.agents import params, templates
from app.agents.templates import _gcp_vpc_policy


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


# ── registry↔disk consistency (MODSEED non-negotiable; binds every module) ─────────────────

_DIR_ALLOWLIST = {"demo-null"}          # exercised directly by the runner tests, not chat
_DIR_PREFIX_ALLOWLIST = ("promoted-",)  # MPP materializes promoted modules at runtime


def test_every_workspace_dir_maps_to_a_registered_template():
    registered = {t.workspace for t in templates.TEMPLATES}
    for d in sorted(p.name for p in _modules_dir().iterdir() if p.is_dir()):
        if d in _DIR_ALLOWLIST or d.startswith(_DIR_PREFIX_ALLOWLIST):
            continue
        assert d in registered, f"workspace dir '{d}' has no registered template"


def test_every_registered_template_has_a_workspace_dir():
    for t in templates.TEMPLATES:
        assert (_modules_dir() / t.workspace).is_dir(), f"{t.key} → missing dir {t.workspace}"


# ── C1-style source assertions over the module HCL ─────────────────────────────────────────

def test_gcp_vpc_module_source_invariants():
    src = _src("gcp-vpc")
    assert "auto_create_subnetworks = false" in src            # custom mode, policy-checked
    assert "0.0.0.0/0" not in src                              # never world-open anywhere
    assert 'filter = "ERRORS_ONLY"' in src                     # NAT logging cost-aware
    assert "private_ip_google_access = true" in src
    assert 'backend "' not in src                              # no backend BLOCK (A3 injects)
    assert 'source_ranges = var.subnet_cidrs' in src           # internal fw scoped to OUR cidrs
    assert '"22"' not in src and "3389" not in src             # no admin-port ingress here
    # region always from the variable — never a hardcoded region literal in resources.
    assert 'region  = var.region' in src or 'region = var.region' in src


def test_gcp_vpc_terraform_fmt_and_validate():
    """REAL terraform fmt -check + init -backend=false + validate on the module dir."""
    import subprocess

    d = str(_modules_dir() / "gcp-vpc")
    fmt = subprocess.run(["terraform", "fmt", "-check", "-diff"], cwd=d,
                         capture_output=True, text=True, timeout=120)
    assert fmt.returncode == 0, f"fmt -check failed:\n{fmt.stdout}{fmt.stderr}"
    init = subprocess.run(["terraform", "init", "-backend=false", "-input=false", "-no-color"],
                          cwd=d, capture_output=True, text=True, timeout=300)
    assert init.returncode == 0, f"init failed:\n{init.stderr[-800:]}"
    val = subprocess.run(["terraform", "validate", "-no-color"], cwd=d,
                         capture_output=True, text=True, timeout=120)
    assert val.returncode == 0, f"validate failed:\n{val.stdout}{val.stderr}"


# ── registration: schema, params, synonyms, policy ─────────────────────────────────────────

def test_registered_and_routable_via_synonyms():
    t = templates.by_key("gcp.vpc")
    assert t is not None and t.workspace == "gcp-vpc"
    assert templates.select("gcp", "vpc") is t
    assert templates.select("gcp", "network") is t             # synonym
    assert templates.select("aws", "network") is not t         # never cross-cloud


def test_schema_validates_and_rejects_bad_cidrs():
    t = templates.by_key("gcp.vpc")
    v = t.schema(name="prod-net").model_dump()
    assert v["subnet_cidrs"] == ["10.10.0.0/20", "10.10.16.0/20"] and v["enable_nat"] is True
    with pytest.raises(Exception):
        t.schema(name="x", subnet_cidrs=["8.8.8.0/24"])        # public space refused
    with pytest.raises(Exception):
        t.schema(name="x", subnet_cidrs=["not-a-cidr"])
    with pytest.raises(Exception):
        t.schema(name="x", subnet_cidrs=[])


def test_params_ask_only_the_name():
    missing = {p.name for p in params.missing_required("gcp.vpc", {})}
    assert missing == {"name"}


def test_policy_over_real_plan_json():
    good = [
        {"type": "google_compute_network", "after": {"auto_create_subnetworks": False}},
        {"type": "google_compute_subnetwork", "after": {"ip_cidr_range": "10.10.0.0/20"}},
        {"type": "google_compute_subnetwork", "after": {"ip_cidr_range": "10.10.16.0/20"}},
    ]
    checks = _gcp_vpc_policy({}, good)
    by = {c["name"]: c for c in checks}
    assert by["Custom-mode network (no auto subnets)"]["passed"] is True
    assert by["At least one explicit subnet"]["passed"] is True

    bad = [{"type": "google_compute_network", "after": {"auto_create_subnetworks": True}}]
    checks_bad = _gcp_vpc_policy({}, bad)
    by_bad = {c["name"]: c for c in checks_bad}
    assert by_bad["Custom-mode network (no auto subnets)"]["passed"] is False
    assert by_bad["At least one explicit subnet"]["passed"] is False  # zero subnets planned

    none_checks = _gcp_vpc_policy({"subnet_cidrs": ["10.0.0.0/20"]}, None)
    assert none_checks[0]["evaluated"] is False                # no plan → pending, never fake


# ── seamless-operation contract (faked runner, live datastores) ────────────────────────────

class _FakeRunner:
    def __init__(self, workspace, settings, state_workspace=None, run_id=None):
        self.workspace = workspace

    async def init(self, on_line=None, force=False):
        return {}

    async def plan(self, variables=None, destroy=False, on_line=None):
        return {"summary": {"add": 5, "change": 0, "destroy": 0},
                "diff": [{"sign": "+", "type": "google_compute_network",
                          "address": "google_compute_network.this", "actions": ["create"]}]}

    async def show_plan(self):
        return await self.plan()

    def planned_resources(self):
        return [
            {"type": "google_compute_network", "after": {"auto_create_subnetworks": False}},
            {"type": "google_compute_subnetwork", "after": {"ip_cidr_range": "10.10.0.0/20"}},
        ]

    async def apply(self, on_line=None):
        return {"outputs": {"network_id": "projects/p/global/networks/prod-net",
                            "network_name": "prod-net",
                            "subnet_ids": ["projects/p/regions/us-central1/subnetworks/prod-net-subnet-0"],
                            "subnet_cidrs": ["10.10.0.0/20"],
                            "secondary_range_names": [["prod-net-pods-0", "prod-net-services-0"]]}}


class _Emitter:
    def __init__(self): self.interrupts = []; self.tokens = []
    async def step(self, *a, **k): pass
    async def token(self, t): self.tokens.append(t)
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
    """The MS-1 acceptance bar: plan → REAL policy checks on the approval card → apply →
    inventory row + world-model node, through the real cloudops nodes (runner faked)."""
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
        s.add(DbSession(id=uuid.UUID(sid), org_id=uuid.UUID(org), title="ms1"))
        await s.flush()  # the Run's session_id FK needs the Session row flushed first
        # Production always has the Run row before the graph runs (created by /chat) — the D2
        # same-txn inventory write references it by FK.
        s.add(Run(id=uuid.UUID(rid), org_id=uuid.UUID(org), session_id=uuid.UUID(sid),
                  status="running", mode="apply"))

    state = {"message": "create a vpc in gcp, name=prod-net", "org_id": org, "run_id": rid,
             "session_id": sid, "domain": "cloudops", "intent": "provision", "action": "create",
             "cloud": "gcp", "resource": "network",  # synonym — resolves to gcp.vpc
             "user": {"region": "us-central1", "user_id": None}}
    try:
        out = await cloudops.cloudops_plan(state, {})
        assert out["approval_status"] == "pending" and out["needs_change"] is True
        card = em.interrupts[0]
        names = {c["name"]: c for c in card["policyChecks"]}
        assert names["Custom-mode network (no auto subnets)"]["passed"] is True   # REAL check
        assert card["plan"]["summary"]["add"] == 5

        # Post-approval execute (capability asserted upstream) → inventory + world model.
        exec_state = {**state, **out, "approval_status": "approved",
                      "approver": {"can_execute": True}}
        result = await cloudops.cloudops_execute(exec_state, {})
        assert result["outcome"]["status"] == "applied"
        rows = await inventory.list_active(org)
        assert any(r["name"] == "prod-net" and r["resource_type"] == "vpc" for r in rows)
        wm = await world_model.list_active(org)
        assert any(r["name"] == "prod-net" for r in wm)
    finally:
        from sqlalchemy import delete
        from app.db.models import Resource
        async with session_scope() as s:
            await s.execute(delete(Resource).where(Resource.org_id == uuid.UUID(org)))
            await s.execute(delete(Run).where(Run.id == uuid.UUID(rid)))
            await s.execute(delete(DbSession).where(DbSession.id == uuid.UUID(sid)))
        await world_model._run("MATCH (r:Resource {org_id:$o}) DETACH DELETE r", o=org)
