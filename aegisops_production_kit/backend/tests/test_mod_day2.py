"""MOD — day-2 modify beyond ports: S3 lifecycle/versioning, RDS scaling, tags, and the
owner's Option-A power state (Terraform-encoded: aws_ec2_instance_state / GCE
desired_status — approval-gated, plan-guarded, audited; NEVER an SDK call). Azure gets the
honest answer verbatim. B1-protected: every new field defaults to the current behavior, so
existing resources' re-plans are unchanged (the committed workspace terraform tests prove
the renderings; module-level runs live in the MS-10/12 gates, extended deliberately).
"""

from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path

import pytest

from app.agents import cloudops
from app.agents.cloudops import _AZURE_POWER_ANSWER, _MODIFY_CAPS, _apply_modification
from app.schemas.workflows import AWSEC2Inputs, AWSRDSInputs, AWSS3Inputs, GCPComputeInputs


def _ws(name: str) -> Path:
    here = Path(__file__).resolve()
    for base in here.parents:
        cand = base / "infra" / "terraform-workspaces"
        if cand.is_dir():
            return cand / name
    raise FileNotFoundError("infra/terraform-workspaces not found")


# ── extraction (regex fallbacks — no LLM in this environment) ──────────────────────────────

async def test_extract_power_and_tags():
    from app.settings import get_settings
    s = get_settings()
    assert (await cloudops._extract_modification(s, "stop web-01"))["power"] == "stopped"
    assert (await cloudops._extract_modification(s, "please power on the vm"))["power"] == "running"
    assert (await cloudops._extract_modification(s, "shut down batch-runner"))["power"] == "stopped"
    tags = (await cloudops._extract_modification(s, "tag env=prod on web-01"))["tags"]
    assert tags == {"env": "prod"}


async def test_extract_s3_and_rds_changes():
    from app.settings import get_settings
    s = get_settings()
    assert (await cloudops._extract_modification(s, "turn versioning off on logs-bucket"))["versioning"] is False
    assert (await cloudops._extract_modification(
        s, "expire objects after 30 days in logs-bucket"))["lifecycle_expire_days"] == 30
    assert (await cloudops._extract_modification(
        s, "scale payments-db to db.t3.large"))["instance_class"] == "db.t3.large"
    assert (await cloudops._extract_modification(
        s, "grow storage to 100 GiB on payments-db"))["allocated_storage"] == 100
    assert (await cloudops._extract_modification(s, "what's the weather")) == {}


# ── merge semantics + schema fields (B1: defaults = current behavior) ─────────────────────

def test_apply_modification_merges_not_replaces():
    merged, desc = _apply_modification(
        {"name": "web", "ingress_ports": [80], "extra_tags": {"team": "core"}},
        {"ingress_ports": [8080], "power": "stopped", "tags": {"env": "prod"}})
    assert merged["ingress_ports"] == [80, 8080]           # union, never replace
    assert merged["power_state"] == "stopped"
    assert merged["extra_tags"] == {"team": "core", "env": "prod"}
    assert any("no SDK call" in d for d in desc)


def test_schema_defaults_preserve_current_behavior():
    assert AWSEC2Inputs(name="x").power_state == "" and AWSEC2Inputs(name="x").extra_tags == {}
    assert GCPComputeInputs(name="x").power_state == ""
    assert AWSS3Inputs(bucket_name="bkt").lifecycle_expire_days == 0
    assert AWSRDSInputs(identifier="db").extra_tags == {}
    with pytest.raises(Exception):
        AWSEC2Inputs(name="x", power_state="hibernate")
    with pytest.raises(Exception):
        AWSS3Inputs(bucket_name="bkt", lifecycle_expire_days=9999)
    assert AWSEC2Inputs(name="x", power_state="STOPPED").power_state == "stopped"


def test_capability_map_matches_owner_decision():
    assert "power" in _MODIFY_CAPS["aws.ec2"] and "power" in _MODIFY_CAPS["gcp.vm"]
    assert "power" not in _MODIFY_CAPS["azure.vm"]          # Option A: no Azure power path
    assert {"versioning", "lifecycle_expire_days", "tags"} <= _MODIFY_CAPS["aws.s3"]
    assert {"instance_class", "allocated_storage", "tags"} <= _MODIFY_CAPS["aws.rds"]


# ── the committed s3 rendering gate (ec2/gce power runs live in the MS-10/12 gates) ───────

def test_s3_terraform_test_gate(tmp_path):
    d = str(_ws("aws-s3"))
    env = dict(os.environ)
    env["TF_DATA_DIR"] = str(tmp_path / "tfdata")
    init = subprocess.run(["terraform", "init", "-backend=false", "-input=false", "-no-color"],
                          cwd=d, capture_output=True, text=True, timeout=300, env=env)
    assert init.returncode == 0, f"init failed:\n{init.stderr[-800:]}"
    test = subprocess.run(["terraform", "test", "-no-color"], cwd=d,
                          capture_output=True, text=True, timeout=600, env=env)
    assert test.returncode == 0, f"terraform test failed:\n{test.stdout[-2000:]}{test.stderr[-500:]}"
    assert "3 passed, 0 failed" in test.stdout


# ── _modify_resource end-to-end (faked runner, live datastores) ────────────────────────────

class _Emitter:
    def __init__(self): self.interrupts = []; self.tokens = []; self.analyses = []
    async def step(self, *a, **k): pass
    async def token(self, t): self.tokens.append(t)
    async def console(self, *a, **k): pass
    async def confidentiality(self, *a, **k): pass
    async def analysis(self, **k): self.analyses.append(k)
    async def interrupt(self, payload): self.interrupts.append(payload)
    async def error(self, *a, **k): pass


class _ModifyRunner:
    seen: dict = {}
    def __init__(self, workspace, settings, state_workspace=None, run_id=None):
        _ModifyRunner.seen["workspace"] = workspace
    async def init(self, on_line=None, force=False): return {}
    async def plan(self, variables=None, destroy=False, on_line=None):
        if variables is not None:
            _ModifyRunner.seen["variables"] = variables
        return {"summary": {"add": 1, "change": 1, "destroy": 0},
                "diff": [{"sign": "~", "type": "aws_instance", "address": "aws_instance.this",
                          "actions": ["update"]}]}
    async def show_plan(self): return await self.plan()
    def planned_resources(self): return []


async def _run_modify(monkeypatch, org, sid, message, target):
    em = _Emitter()
    monkeypatch.setattr(cloudops, "TerraformRunner", _ModifyRunner)
    monkeypatch.setattr(cloudops, "emitter_of", lambda cfg: em)
    state = {"message": message, "org_id": org, "run_id": str(uuid.uuid4()),
             "session_id": sid, "domain": "cloudops", "action": "modify",
             "target": target, "user": {"user_id": None}}
    out = await cloudops.cloudops_plan(state, {})
    return out, em


@pytest.fixture
async def _mod_rows(live_db, live_redis, live_neo4j, throwaway_org):
    from app.db.models import Resource, Session as DbSession
    from app.db.session import session_scope
    from sqlalchemy import delete
    org = throwaway_org
    sid = str(uuid.uuid4())
    rows = [
        dict(name="web-01", cloud="aws", resource_type="ec2", workspace="aws-ec2",
             inputs={"name": "web-01", "instance_type": "t3.micro",
                     "os": "amazon-linux-2023", "allowed_cidr": "10.0.0.0/16"}),
        dict(name="logs-bucket", cloud="aws", resource_type="s3", workspace="aws-s3",
             inputs={"bucket_name": "logs-bucket"}),
        dict(name="payments-db", cloud="aws", resource_type="rds", workspace="aws-rds",
             inputs={"identifier": "payments-db", "engine": "postgres"}),
        dict(name="az-box", cloud="azure", resource_type="vm", workspace="azure-vm",
             inputs={"name": "az-box", "size": "Standard_B1s", "os": "ubuntu-22.04"}),
    ]
    async with session_scope() as s:
        s.add(DbSession(id=uuid.UUID(sid), org_id=uuid.UUID(org), title="mod"))
        for r in rows:
            s.add(Resource(org_id=uuid.UUID(org), status="active", provider_id=f"id-{r['name']}", **r))
    yield org, sid
    async with session_scope() as s:
        await s.execute(delete(Resource).where(Resource.org_id == uuid.UUID(org)))
        await s.execute(delete(DbSession).where(DbSession.id == uuid.UUID(sid)))


async def test_power_stop_is_a_governed_modify(monkeypatch, _mod_rows):
    org, sid = _mod_rows
    out, em = await _run_modify(monkeypatch, org, sid, "stop web-01", "web-01")
    assert out["approval_status"] == "pending"              # approval-gated, like any modify
    assert _ModifyRunner.seen["variables"]["power_state"] == "stopped"
    assert "power state" in out["answer"] and "no SDK call" in out["answer"]


async def test_azure_power_gets_the_honest_answer(monkeypatch, _mod_rows):
    org, sid = _mod_rows
    out, em = await _run_modify(monkeypatch, org, sid, "stop az-box", "az-box")
    assert out["approval_status"] == "not_required"         # no plan, no interrupt
    assert out["answer"] == _AZURE_POWER_ANSWER
    assert "portal" in out["answer"]


async def test_s3_versioning_and_lifecycle_modify(monkeypatch, _mod_rows):
    org, sid = _mod_rows
    out, _ = await _run_modify(monkeypatch, org, sid,
                               "turn versioning off on logs-bucket", "logs-bucket")
    assert out["approval_status"] == "pending"
    assert _ModifyRunner.seen["variables"]["versioning"] is False

    out2, _ = await _run_modify(monkeypatch, org, sid,
                                "expire objects after 30 days in logs-bucket", "logs-bucket")
    assert out2["approval_status"] == "pending"
    assert _ModifyRunner.seen["variables"]["lifecycle_expire_days"] == 30


async def test_rds_scale_modify(monkeypatch, _mod_rows):
    org, sid = _mod_rows
    out, _ = await _run_modify(monkeypatch, org, sid,
                               "scale payments-db to db.t3.large", "payments-db")
    assert out["approval_status"] == "pending"
    assert _ModifyRunner.seen["variables"]["instance_class"] == "db.t3.large"
    assert _ModifyRunner.seen["variables"]["engine"] == "postgres"   # stored inputs kept


async def test_unsupported_change_gets_the_honest_list(monkeypatch, _mod_rows):
    org, sid = _mod_rows
    out, _ = await _run_modify(monkeypatch, org, sid,
                               "turn versioning off on payments-db", "payments-db")
    assert out["approval_status"] == "not_required"
    assert "aws.rds" in out["answer"] and "not versioning" in out["answer"]
