"""MODSEED MS-4 — aws.kms: full registration + honest destroy semantics + seamless contract.

Keys, never secrets: secret VALUES are permanently out of scope. The destroy card must state
the scheduled-deletion window (a destroyed key is NOT removed immediately).
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from app.agents import params, templates
from app.agents.templates import _aws_kms_policy


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


# ── C1 source invariants ───────────────────────────────────────────────────────────────────

def test_aws_kms_module_source_invariants():
    src = _src("aws-kms")
    assert "enable_key_rotation     = var.enable_rotation" in src
    assert "deletion_window_in_days = var.deletion_window" in src
    assert 'name          = "alias/${var.name}"' in src
    assert "data.aws_caller_identity.current.account_id" in src   # root admin via caller identity
    assert '"kms:Decrypt", "kms:DescribeKey", "kms:CreateGrant"' in src
    assert "password" not in src.lower() and "secret_string" not in src  # keys, never secrets
    assert 'backend "' not in src
    assert 'version = "~> 5.60"' in src
    assert "region = var.region" in src
    assert "var.deletion_window >= 7" in src                       # module-level bound too


def test_aws_kms_terraform_fmt_and_validate():
    import subprocess

    d = str(_modules_dir() / "aws-kms")
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
    t = templates.by_key("aws.kms")
    assert t is not None and t.workspace == "aws-kms"
    for syn in ("kms", "key", "encryption_key", "secrets"):      # secrets → kms (keys, not values)
        assert templates.select("aws", syn) is t, syn
    assert templates.select("gcp", "secrets") is not t           # never cross-cloud


def test_schema_bounds_deletion_window():
    t = templates.by_key("aws.kms")
    v = t.schema(name="app-secrets").model_dump()
    assert v["deletion_window"] == 30 and v["enable_rotation"] is True
    assert v["allowed_services"] == ["secretsmanager", "rds"]
    with pytest.raises(Exception):
        t.schema(name="x", deletion_window=3)                    # below the 7-day floor
    with pytest.raises(Exception):
        t.schema(name="x", deletion_window=45)


def test_params_ask_only_the_name():
    assert {p.name for p in params.missing_required("aws.kms", {})} == {"name"}


def test_destroy_note_declares_scheduled_deletion():
    note = templates.by_key("aws.kms").destroy_note
    assert note and "scheduled-deletion" in note and "NOT removed immediately" in note


def test_policy_over_real_plan_json():
    good = [{"type": "aws_kms_key", "after": {"enable_key_rotation": True,
                                              "deletion_window_in_days": 30}}]
    by = {c["name"]: c for c in _aws_kms_policy({}, good)}
    assert by["Key rotation enabled"]["passed"] is True
    assert by["Deletion window >= 7 days"]["passed"] is True

    bad = [{"type": "aws_kms_key", "after": {"enable_key_rotation": False,
                                             "deletion_window_in_days": 3}}]
    by_bad = {c["name"]: c for c in _aws_kms_policy({}, bad)}
    assert by_bad["Key rotation enabled"]["passed"] is False
    assert by_bad["Deletion window >= 7 days"]["passed"] is False

    none_checks = _aws_kms_policy({"enable_rotation": True, "deletion_window": 30}, None)
    assert none_checks[0]["passed"] is True                      # input-based fallback pre-plan


# ── destroy card carries the semantics ─────────────────────────────────────────────────────

async def test_destroy_card_states_the_deletion_window(live_db, live_redis, live_neo4j,
                                                       throwaway_org, monkeypatch):
    """"destroy app-secrets" → the approval card's reasoning includes the module's honest
    deletion semantics (scheduled window, not immediate)."""
    from app.agents import cloudops

    class _DestroyRunner:
        def __init__(self, *a, **k): pass
        async def init(self, on_line=None, force=False): return {}
        async def plan(self, variables=None, destroy=False, on_line=None):
            return {"summary": {"add": 0, "change": 0, "destroy": 2},
                    "diff": [{"sign": "-", "type": "aws_kms_key", "address": "aws_kms_key.this",
                              "actions": ["delete"]},
                             {"sign": "-", "type": "aws_kms_alias", "address": "aws_kms_alias.this",
                              "actions": ["delete"]}]}
        async def show_plan(self): return await self.plan()
        def planned_resources(self): return []

    class _Emitter:
        def __init__(self): self.interrupts = []; self.analyses = []
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
        s.add(DbSession(id=uuid.UUID(sid), org_id=uuid.UUID(org), title="ms4"))
        s.add(Resource(org_id=uuid.UUID(org), name="app-secrets", cloud="aws",
                       resource_type="kms", workspace="aws-kms", provider_id="key-123",
                       status="active", inputs={"name": "app-secrets"}))

    state = {"message": "destroy app-secrets", "org_id": org, "run_id": str(uuid.uuid4()),
             "session_id": sid, "domain": "cloudops", "action": "destroy",
             "target": "app-secrets", "user": {"user_id": None}}
    try:
        out = await cloudops.cloudops_agent(state, {}) if hasattr(cloudops, "cloudops_agent") \
            else await cloudops.cloudops_plan(state, {})
        assert out["approval_status"] == "pending"
        cards = em.analyses[0]["cards"]
        semantics = [c for c in cards if c["title"] == "Deletion semantics"]
        assert semantics and "scheduled-deletion" in semantics[0]["body"]
    finally:
        from sqlalchemy import delete
        async with session_scope() as s:
            await s.execute(delete(Resource).where(Resource.org_id == uuid.UUID(org)))
            await s.execute(delete(DbSession).where(DbSession.id == uuid.UUID(sid)))


# ── seamless contract ──────────────────────────────────────────────────────────────────────

class _FakeRunner:
    def __init__(self, workspace, settings, state_workspace=None, run_id=None):
        self.workspace = workspace
    async def init(self, on_line=None, force=False): return {}
    async def plan(self, variables=None, destroy=False, on_line=None):
        return {"summary": {"add": 2, "change": 0, "destroy": 0},
                "diff": [{"sign": "+", "type": "aws_kms_key", "address": "aws_kms_key.this",
                          "actions": ["create"]}]}
    async def show_plan(self): return await self.plan()
    def planned_resources(self):
        return [{"type": "aws_kms_key", "after": {"enable_key_rotation": True,
                                                  "deletion_window_in_days": 30}}]
    async def apply(self, on_line=None):
        return {"outputs": {"key_id": "1234-key", "key_arn": "arn:aws:kms:...:key/1234-key",
                            "alias": "alias/app-secrets", "rotation_enabled": True,
                            "deletion_window_days": 30}}


class _Emitter2:
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
    em = _Emitter2()
    monkeypatch.setattr(cloudops, "emitter_of", lambda cfg: em)

    org = throwaway_org
    sid, rid = str(uuid.uuid4()), str(uuid.uuid4())
    from app.db.models import Run, Session as DbSession
    from app.db.session import session_scope
    async with session_scope() as s:
        s.add(DbSession(id=uuid.UUID(sid), org_id=uuid.UUID(org), title="ms4"))
        await s.flush()
        s.add(Run(id=uuid.UUID(rid), org_id=uuid.UUID(org), session_id=uuid.UUID(sid),
                  status="running", mode="apply"))

    state = {"message": "create a kms key in aws, name=app-secrets", "org_id": org,
             "run_id": rid, "session_id": sid, "domain": "cloudops", "intent": "provision",
             "action": "create", "cloud": "aws", "resource": "kms",
             "user": {"region": "us-east-1", "user_id": None}}
    try:
        out = await cloudops.cloudops_plan(state, {})
        assert out["approval_status"] == "pending"
        names = {c["name"]: c for c in em.interrupts[0]["policyChecks"]}
        assert names["Key rotation enabled"]["passed"] is True
        assert names["Deletion window >= 7 days"]["passed"] is True

        exec_state = {**state, **out, "approval_status": "approved",
                      "approver": {"can_execute": True}}
        result = await cloudops.cloudops_execute(exec_state, {})
        assert result["outcome"]["status"] == "applied"
        rows = await inventory.list_active(org)
        row = next(r for r in rows if r["name"] == "app-secrets")
        assert row["resource_type"] == "kms"
        assert row["attributes"]["rotation_enabled"] is True     # day-2 "what's the rotation"
        wm = await world_model.list_active(org)
        assert any(r["name"] == "app-secrets" for r in wm)
    finally:
        from sqlalchemy import delete
        from app.db.models import Resource
        async with session_scope() as s:
            await s.execute(delete(Resource).where(Resource.org_id == uuid.UUID(org)))
            await s.execute(delete(Run).where(Run.id == uuid.UUID(rid)))
            await s.execute(delete(DbSession).where(DbSession.id == uuid.UUID(sid)))
        await world_model._run("MATCH (r:Resource {org_id:$o}) DETACH DELETE r", o=org)
