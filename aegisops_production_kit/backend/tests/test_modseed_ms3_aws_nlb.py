"""MODSEED MS-3 — aws.nlb: full registration + DEP placement + seamless contract.

The NLB's placement (vpc_id + subnets) is DEP-resolved: one existing aws.vpc → filled from its
RECORDED outputs; two → offered; none → create-first DAG (vpc → nlb) for the executive loop.
deletion_protection defaults ON for env=Production via the env-defaults hook — stated on the
card, never silent. The auto SG is EGRESS-ONLY (zero ingress rules).
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from app.agents import params, templates
from app.agents.dependency import resolve_closure
from app.agents.templates import _aws_nlb_policy, apply_env_defaults


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

def test_aws_nlb_module_source_invariants():
    src = _src("aws-nlb")
    assert 'load_balancer_type               = "network"' in src
    assert "enable_cross_zone_load_balancing = true" in src
    assert 'protocol            = "TCP"' in src and "interval            = 30" in src
    assert "healthy_threshold   = 3" in src
    import re as _re
    assert _re.search(r"^\s*ingress\s*\{", src, _re.M) is None  # the auto SG has NO ingress BLOCK
    assert src.count("0.0.0.0/0") == 1                            # the single 0/0 is the egress route
    assert _re.search(r"^\s*egress\s*\{", src, _re.M) is not None
    assert 'backend "' not in src
    assert 'version = "~> 5.60"' in src                          # repo's current aws major
    assert "region = var.region" in src


def test_aws_nlb_terraform_fmt_and_validate():
    import subprocess

    d = str(_modules_dir() / "aws-nlb")
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
    t = templates.by_key("aws.nlb")
    assert t is not None and t.workspace == "aws-nlb"
    for syn in ("nlb", "lb", "load_balancer", "loadbalancer"):
        assert templates.select("aws", syn) is t, syn
    assert templates.select("azure", "lb") is None               # never cross-cloud


def test_schema_listener_defaults_to_target_port():
    t = templates.by_key("aws.nlb")
    v = t.schema(name="web-lb", target_port=443).model_dump()
    assert v["listener_port"] == 443 and v["deletion_protection"] is None
    v2 = t.schema(name="web-lb", listener_port=8443).model_dump()
    assert v2["listener_port"] == 8443


def test_params_ask_only_the_name():
    assert {p.name for p in params.missing_required("aws.nlb", {})} == {"name"}


def test_env_default_deletion_protection():
    v = {"deletion_protection": None}
    notes = apply_env_defaults("aws.nlb", v, "Production")
    assert v["deletion_protection"] is True and any("Production" in n for n in notes)
    v2 = {"deletion_protection": None}
    apply_env_defaults("aws.nlb", v2, "Staging")
    assert v2["deletion_protection"] is False
    v3 = {"deletion_protection": False}                          # explicit choice wins
    assert apply_env_defaults("aws.nlb", v3, "Production") == []
    assert v3["deletion_protection"] is False


# ── DEP placement: one / two / none ────────────────────────────────────────────────────────

def _vpc(name, pid):
    return {"name": name, "cloud": "aws", "resource_type": "vpc", "provider_id": pid,
            "attributes": {"vpc_id": pid, "public_subnet_ids": [f"{pid}-pub-a", f"{pid}-pub-b"],
                           "private_subnet_ids": [f"{pid}-priv-a"]}}


def test_dep_one_vpc_fills_subnets_from_recorded_outputs():
    c = resolve_closure("aws.nlb", {"name": "web-lb"}, active=[_vpc("net-1", "vpc-100")],
                        message="create a load balancer web-lb in aws")
    assert c.status == "complete"
    assert c.inputs["vpc_id"] == "vpc-100"
    assert c.inputs["subnets"] == ["vpc-100-pub-a", "vpc-100-pub-b"]  # RECORDED outputs
    assert any("world model" in n for n in c.notes)


def test_dep_two_vpcs_are_offered():
    c = resolve_closure("aws.nlb", {"name": "web-lb"},
                        active=[_vpc("net-1", "vpc-100"), _vpc("net-2", "vpc-200")], message="")
    assert c.status == "ask" and len(c.options) == 2


def test_dep_none_yields_create_first_dag():
    c = resolve_closure("aws.nlb", {"name": "web-lb"}, active=[], message="")
    assert c.status == "dag"
    assert [s["template_key"] for s in c.dag] == ["aws.vpc", "aws.nlb"]  # parents FIRST
    child = c.dag[1]
    assert child["wires"] == {"vpc_id": "vpc_id", "subnets": "public_subnet_ids"}
    assert child["depends_on"] == "aws.vpc"


# ── policy over real plan JSON ─────────────────────────────────────────────────────────────

def test_policy_over_real_plan_json():
    good = [
        {"type": "aws_lb", "after": {"load_balancer_type": "network",
                                     "enable_cross_zone_load_balancing": True,
                                     "enable_deletion_protection": True}},
        {"type": "aws_lb_target_group",
         "after": {"health_check": [{"protocol": "TCP", "interval": 30,
                                     "healthy_threshold": 3}]}},
    ]
    by = {c["name"]: c for c in _aws_nlb_policy({"deletion_protection": True}, good)}
    assert by["Network load balancer"]["passed"] is True
    assert by["Cross-zone load balancing on"]["passed"] is True
    assert by["Deletion protection as approved"]["passed"] is True
    assert by["TCP health checks (30s, threshold 3)"]["passed"] is True

    # A plan that quietly dropped deletion protection FAILS the check.
    drift = [{"type": "aws_lb", "after": {"load_balancer_type": "network",
                                          "enable_cross_zone_load_balancing": True,
                                          "enable_deletion_protection": False}}]
    by_bad = {c["name"]: c for c in _aws_nlb_policy({"deletion_protection": True}, drift)}
    assert by_bad["Deletion protection as approved"]["passed"] is False
    assert _aws_nlb_policy({}, None)[0]["evaluated"] is False


# ── seamless contract (faked runner, live datastores) ──────────────────────────────────────

class _FakeRunner:
    def __init__(self, workspace, settings, state_workspace=None, run_id=None):
        self.workspace = workspace

    async def init(self, on_line=None, force=False):
        return {}

    async def plan(self, variables=None, destroy=False, on_line=None):
        return {"summary": {"add": 4, "change": 0, "destroy": 0},
                "diff": [{"sign": "+", "type": "aws_lb", "address": "aws_lb.this",
                          "actions": ["create"]}]}

    async def show_plan(self):
        return await self.plan()

    def planned_resources(self):
        return [
            {"type": "aws_lb", "after": {"load_balancer_type": "network",
                                         "enable_cross_zone_load_balancing": True,
                                         "enable_deletion_protection": False}},
            {"type": "aws_lb_target_group",
             "after": {"health_check": [{"protocol": "TCP", "interval": 30,
                                         "healthy_threshold": 3}]}},
        ]

    async def apply(self, on_line=None):
        return {"outputs": {"lb_arn": "arn:aws:elasticloadbalancing:...:loadbalancer/net/web-lb",
                            "lb_dns_name": "web-lb-abc.elb.us-east-1.amazonaws.com",
                            "target_group_arn": "arn:aws:elasticloadbalancing:...:targetgroup/web-lb-tg",
                            "attach_targets_note": "No targets are attached yet — register instances..."}}


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


async def test_seamless_contract_with_existing_vpc(live_db, live_redis, live_neo4j,
                                                   throwaway_org, monkeypatch):
    """One existing VPC in the org → the NLB's card shows DEP provenance + the env default;
    apply lands inventory + world model with a DEPENDS_ON edge to the VPC."""
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
    from app.db.models import Resource, Run, Session as DbSession
    from app.db.session import session_scope
    async with session_scope() as s:
        s.add(DbSession(id=uuid.UUID(sid), org_id=uuid.UUID(org), title="ms3"))
        await s.flush()
        s.add(Run(id=uuid.UUID(rid), org_id=uuid.UUID(org), session_id=uuid.UUID(sid),
                  status="running", mode="apply"))
        s.add(Resource(org_id=uuid.UUID(org), name="net-1", cloud="aws", resource_type="vpc",
                       workspace="aws-vpc", provider_id="vpc-100", status="active",
                       attributes={"vpc_id": "vpc-100",
                                   "public_subnet_ids": ["subnet-a", "subnet-b"]}))

    state = {"message": "create a load balancer in aws, name=web-lb", "org_id": org,
             "run_id": rid, "session_id": sid, "domain": "cloudops", "intent": "provision",
             "action": "create", "cloud": "aws", "resource": "lb",
             "user": {"region": "us-east-1", "env": "Production", "user_id": None}}
    try:
        out = await cloudops.cloudops_plan(state, {})
        assert out["approval_status"] == "pending"
        # DEP provenance + the Production deletion-protection default are ON the card.
        defaults = out["plan_json"]["defaults"]
        assert any("net-1" in d["value"] for d in defaults if d["name"] == "Dependency resolution")
        assert any("Production" in d["value"] for d in defaults if d["name"] == "Environment default")
        assert out["parsed_inputs"]["subnets"] == ["subnet-a", "subnet-b"]
        assert out["parsed_inputs"]["deletion_protection"] is True

        exec_state = {**state, **out, "approval_status": "approved",
                      "approver": {"can_execute": True}}
        result = await cloudops.cloudops_execute(exec_state, {})
        assert result["outcome"]["status"] == "applied"
        rows = await inventory.list_active(org)
        assert any(r["name"] == "web-lb" and r["resource_type"] == "nlb" for r in rows)
        deps = await world_model.impact_of(org, provider_id="vpc-100")
        assert any(d["name"] == "web-lb" for d in deps)          # DEPENDS_ON edge recorded
    finally:
        from sqlalchemy import delete
        async with session_scope() as s:
            await s.execute(delete(Resource).where(Resource.org_id == uuid.UUID(org)))
            await s.execute(delete(Run).where(Run.id == uuid.UUID(rid)))
            await s.execute(delete(DbSession).where(DbSession.id == uuid.UUID(sid)))
        await world_model._run("MATCH (r:Resource {org_id:$o}) DETACH DELETE r", o=org)
