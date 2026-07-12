"""MODSEED MS-6 — gcp.kms: full registration + the not-deletable-ring destroy semantics +
seamless contract. The last of the six MODSEED modules.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from app.agents import params, templates
from app.agents.templates import _gcp_kms_policy


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


def test_gcp_kms_module_source_invariants():
    src = _src("gcp-kms")
    assert 'purpose         = "ENCRYPT_DECRYPT"' in src
    assert 'protection_level = "SOFTWARE"' in src
    assert 'algorithm        = "GOOGLE_SYMMETRIC_ENCRYPTION"' in src
    assert 'rotation_period = local.rotation_period' in src
    assert '"${var.rotation_days * 86400}s"' in src              # days → seconds, no magic string
    assert "roles/cloudkms.cryptoKeyEncrypterDecrypter" in src
    assert "NOT deletable" in src                                # semantics documented in-module
    assert "google_secret_manager" not in src                     # keys, never secret stores
    assert "secret_data" not in src                                # ...and never secret values
    assert 'backend "' not in src and 'version = "~> 5.40"' in src
    assert "region  = var.region" in src or "region = var.region" in src


def test_gcp_kms_terraform_fmt_and_validate():
    import subprocess

    d = str(_modules_dir() / "gcp-kms")
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
    t = templates.by_key("gcp.kms")
    assert t is not None and t.workspace == "gcp-kms"
    for syn in ("kms", "keyring", "key", "encryption_key", "secrets"):
        assert templates.select("gcp", syn) is t, syn
    assert templates.select("aws", "key").key == "aws.kms"       # per-cloud synonyms coexist


def test_schema_defaults_and_bounds():
    t = templates.by_key("gcp.kms")
    v = t.schema(name="app-ring").model_dump()
    assert v["rotation_days"] == 90 and v["keys"] == []
    with pytest.raises(Exception):
        t.schema(name="x", rotation_days=0)


def test_params_ask_only_the_name():
    assert {p.name for p in params.missing_required("gcp.kms", {})} == {"name"}


def test_destroy_note_says_rings_are_not_deletable():
    note = templates.by_key("gcp.kms").destroy_note
    assert note and "NOT deletable" in note and "remain reserved" in note


def test_policy_over_real_plan_json():
    good = [{"type": "google_kms_crypto_key",
             "after": {"purpose": "ENCRYPT_DECRYPT", "rotation_period": "7776000s",
                       "version_template": [{"protection_level": "SOFTWARE",
                                             "algorithm": "GOOGLE_SYMMETRIC_ENCRYPTION"}]}}]
    by = {c["name"]: c for c in _gcp_kms_policy({}, good)}
    assert by["Automatic rotation configured"]["passed"] is True
    assert by["SOFTWARE protection level"]["passed"] is True
    assert by["ENCRYPT_DECRYPT purpose"]["passed"] is True

    bad = [{"type": "google_kms_crypto_key",
            "after": {"purpose": "ASYMMETRIC_SIGN", "rotation_period": None,
                      "version_template": [{"protection_level": "HSM"}]}}]
    by_bad = {c["name"]: c for c in _gcp_kms_policy({}, bad)}
    assert by_bad["Automatic rotation configured"]["passed"] is False
    assert by_bad["SOFTWARE protection level"]["passed"] is False
    assert by_bad["ENCRYPT_DECRYPT purpose"]["passed"] is False


async def test_destroy_card_states_ring_permanence(live_db, live_redis, live_neo4j,
                                                   throwaway_org, monkeypatch):
    """"destroy app-ring" → the card says rings are NOT deletable (versions/IAM only)."""
    from app.agents import cloudops

    class _DestroyRunner:
        def __init__(self, *a, **k): pass
        async def init(self, on_line=None, force=False): return {}
        async def plan(self, variables=None, destroy=False, on_line=None):
            return {"summary": {"add": 0, "change": 0, "destroy": 1},
                    "diff": [{"sign": "-", "type": "google_kms_crypto_key",
                              "address": "google_kms_crypto_key.this", "actions": ["delete"]}]}
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
        s.add(DbSession(id=uuid.UUID(sid), org_id=uuid.UUID(org), title="ms6"))
        s.add(Resource(org_id=uuid.UUID(org), name="app-ring", cloud="gcp",
                       resource_type="kms", workspace="gcp-kms", provider_id="ring-1",
                       status="active", inputs={"name": "app-ring"}))

    state = {"message": "destroy app-ring", "org_id": org, "run_id": str(uuid.uuid4()),
             "session_id": sid, "domain": "cloudops", "action": "destroy",
             "target": "app-ring", "user": {"user_id": None}}
    try:
        out = await cloudops.cloudops_plan(state, {})
        assert out["approval_status"] == "pending"
        cards = em.analyses[0]["cards"]
        semantics = [c for c in cards if c["title"] == "Deletion semantics"]
        assert semantics and "NOT deletable" in semantics[0]["body"]
    finally:
        from sqlalchemy import delete
        async with session_scope() as s:
            await s.execute(delete(Resource).where(Resource.org_id == uuid.UUID(org)))
            await s.execute(delete(DbSession).where(DbSession.id == uuid.UUID(sid)))


class _FakeRunner:
    def __init__(self, workspace, settings, state_workspace=None, run_id=None):
        self.workspace = workspace
    async def init(self, on_line=None, force=False): return {}
    async def plan(self, variables=None, destroy=False, on_line=None):
        return {"summary": {"add": 2, "change": 0, "destroy": 0},
                "diff": [{"sign": "+", "type": "google_kms_key_ring",
                          "address": "google_kms_key_ring.this", "actions": ["create"]}]}
    async def show_plan(self): return await self.plan()
    def planned_resources(self):
        return [{"type": "google_kms_crypto_key",
                 "after": {"purpose": "ENCRYPT_DECRYPT", "rotation_period": "7776000s",
                           "version_template": [{"protection_level": "SOFTWARE"}]}}]
    async def apply(self, on_line=None):
        return {"outputs": {"keyring_id": "projects/p/locations/us-central1/keyRings/app-ring",
                            "keyring_name": "app-ring", "key_ids": ["…/cryptoKeys/app-ring-key"],
                            "key_names": ["app-ring-key"], "rotation_days": 90}}


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
        s.add(DbSession(id=uuid.UUID(sid), org_id=uuid.UUID(org), title="ms6"))
        await s.flush()
        s.add(Run(id=uuid.UUID(rid), org_id=uuid.UUID(org), session_id=uuid.UUID(sid),
                  status="running", mode="apply"))

    state = {"message": "create a kms keyring in gcp, name=app-ring", "org_id": org,
             "run_id": rid, "session_id": sid, "domain": "cloudops", "intent": "provision",
             "action": "create", "cloud": "gcp", "resource": "keyring",
             "user": {"region": "us-central1", "user_id": None}}
    try:
        out = await cloudops.cloudops_plan(state, {})
        assert out["approval_status"] == "pending"
        names = {c["name"]: c for c in em.interrupts[0]["policyChecks"]}
        assert names["Automatic rotation configured"]["passed"] is True
        assert names["SOFTWARE protection level"]["passed"] is True

        exec_state = {**state, **out, "approval_status": "approved",
                      "approver": {"can_execute": True}}
        result = await cloudops.cloudops_execute(exec_state, {})
        assert result["outcome"]["status"] == "applied"
        rows = await inventory.list_active(org)
        row = next(r for r in rows if r["name"] == "app-ring")
        assert row["resource_type"] == "kms"
        assert row["attributes"]["rotation_days"] == 90          # day-2 rotation answer
        wm = await world_model.list_active(org)
        assert any(r["name"] == "app-ring" for r in wm)
    finally:
        from sqlalchemy import delete
        from app.db.models import Resource
        async with session_scope() as s:
            await s.execute(delete(Resource).where(Resource.org_id == uuid.UUID(org)))
            await s.execute(delete(Run).where(Run.id == uuid.UUID(rid)))
            await s.execute(delete(DbSession).where(DbSession.id == uuid.UUID(sid)))
        await world_model._run("MATCH (r:Resource {org_id:$o}) DETACH DELETE r", o=org)
