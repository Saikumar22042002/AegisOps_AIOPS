"""COST — static catalog estimate on the approval card + a REAL guardrail policy check.
Owner directive: spec-minimum static provider pricing (Infracost = backlog). Every row
says "static catalog estimate" — honest about what it is; guardrail breach = failed check
the approver sees.
"""

from __future__ import annotations

import uuid

import pytest

from app.agents import cost


def test_catalog_estimates_compute_and_db():
    e = cost.estimate("aws.ec2", {"instance_type": "t3.micro", "root_volume_size": 30})
    assert e.monthly_usd == pytest.approx(0.0104 * 730 + 30 * 0.08, abs=0.01)
    e = cost.estimate("aws.rds", {"instance_class": "db.t3.large", "allocated_storage": 100})
    assert e.monthly_usd == pytest.approx(0.136 * 730 + 100 * 0.115, abs=0.01)
    e = cost.estimate("gcp.vm", {"machine_type": "e2-micro"})
    assert e.monthly_usd == pytest.approx(0.008378 * 730, abs=0.01)
    assert cost.estimate("azure.vm", {"size": "Standard_B2s"}).monthly_usd > 0


def test_honest_states_for_unpriced_and_usage_based():
    e = cost.estimate("aws.ec2", {"instance_type": "u-24tb1.metal"})
    assert e.monthly_usd is None and "not in the catalog" in e.notes[0]
    e = cost.estimate("aws.s3", {"bucket_name": "b"})
    assert e.monthly_usd == 0.0 and "usage-based" in e.notes[0]
    assert "usage-based" in cost.estimate("aws.s3", {}).text or "catalog" in cost.estimate("aws.s3", {}).text


def test_power_and_spot_shape_the_estimate():
    stopped = cost.estimate("aws.ec2", {"instance_type": "t3.micro", "power_state": "stopped"})
    running = cost.estimate("aws.ec2", {"instance_type": "t3.micro"})
    assert stopped.monthly_usd < running.monthly_usd
    assert any("EBS storage still accrues" in n for n in stopped.notes)
    spot = cost.estimate("gcp.vm", {"machine_type": "e2-medium", "spot": True})
    on_demand = cost.estimate("gcp.vm", {"machine_type": "e2-medium"})
    assert spot.monthly_usd < on_demand.monthly_usd


def test_guardrail_check_fails_on_breach(monkeypatch):
    monkeypatch.setenv("AEGISOPS_COST_GUARDRAIL_USD", "50")
    rows = cost.checks_for("aws.rds", {"instance_class": "db.t3.large", "allocated_storage": 100})
    by = {r["name"]: r for r in rows}
    assert by["Cost estimate (catalog)"]["passed"] is True
    assert "static catalog estimate" in by["Cost estimate (catalog)"]["detail"]
    guard = by["Cost guardrail (≤ $50/mo)"]
    assert guard["passed"] is False and "$50 cap" in guard["detail"]

    ok = {r["name"]: r for r in cost.checks_for("aws.ec2", {"instance_type": "t3.micro"})}
    assert ok["Cost guardrail (≤ $50/mo)"]["passed"] is True


def test_guardrail_fails_closed_when_unpriced(monkeypatch):
    monkeypatch.setenv("AEGISOPS_COST_GUARDRAIL_USD", "100")
    rows = {r["name"]: r for r in cost.checks_for("aws.ec2", {"instance_type": "x9.mega"})}
    assert rows["Cost guardrail (≤ $100/mo)"]["passed"] is False
    assert "cannot verify" in rows["Cost guardrail (≤ $100/mo)"]["detail"]


def test_no_guardrail_row_when_off(monkeypatch):
    monkeypatch.delenv("AEGISOPS_COST_GUARDRAIL_USD", raising=False)
    rows = cost.checks_for("aws.ec2", {"instance_type": "t3.micro"})
    assert len(rows) == 1 and rows[0]["name"] == "Cost estimate (catalog)"


async def test_cost_rows_reach_the_approval_card(live_db, live_redis, live_neo4j,
                                                 throwaway_org, monkeypatch):
    """The interrupt payload's policyChecks carry the estimate row (and the guardrail fail
    when breached) — through the REAL modify path with a faked runner."""
    from app.agents import cloudops
    from app.db.models import Resource, Session as DbSession
    from app.db.session import session_scope
    from sqlalchemy import delete

    monkeypatch.setenv("AEGISOPS_COST_GUARDRAIL_USD", "5")

    class _Runner:
        def __init__(self, *a, **k): pass
        async def init(self, on_line=None, force=False): return {}
        async def plan(self, variables=None, destroy=False, on_line=None):
            return {"summary": {"add": 0, "change": 1, "destroy": 0},
                    "diff": [{"sign": "~", "type": "aws_instance",
                              "address": "aws_instance.this", "actions": ["update"]}]}
        async def show_plan(self): return await self.plan()
        def planned_resources(self): return []

    class _Emitter:
        def __init__(self): self.interrupts = []
        async def step(self, *a, **k): pass
        async def token(self, *a, **k): pass
        async def console(self, *a, **k): pass
        async def confidentiality(self, *a, **k): pass
        async def analysis(self, **k): pass
        async def interrupt(self, payload): self.interrupts.append(payload)
        async def error(self, *a, **k): pass

    monkeypatch.setattr(cloudops, "TerraformRunner", _Runner)
    em = _Emitter()
    monkeypatch.setattr(cloudops, "emitter_of", lambda cfg: em)

    org, sid = throwaway_org, str(uuid.uuid4())
    async with session_scope() as s:
        s.add(DbSession(id=uuid.UUID(sid), org_id=uuid.UUID(org), title="cost"))
        s.add(Resource(org_id=uuid.UUID(org), name="web-99", cloud="aws",
                       resource_type="ec2", workspace="aws-ec2", provider_id="i-99",
                       status="active",
                       inputs={"name": "web-99", "instance_type": "t3.medium",
                               "os": "amazon-linux-2023", "allowed_cidr": "10.0.0.0/16"}))
    state = {"message": "tag env=prod on web-99", "org_id": org, "run_id": str(uuid.uuid4()),
             "session_id": sid, "domain": "cloudops", "action": "modify",
             "target": "web-99", "user": {"user_id": None}}
    try:
        out = await cloudops.cloudops_plan(state, {})
        assert out["approval_status"] == "pending"
        names = {c["name"]: c for c in em.interrupts[0]["policyChecks"]}
        assert "Cost estimate (catalog)" in names
        assert names["Cost guardrail (≤ $5/mo)"]["passed"] is False   # t3.medium > $5 cap
    finally:
        async with session_scope() as s:
            await s.execute(delete(Resource).where(Resource.org_id == uuid.UUID(org)))
            await s.execute(delete(DbSession).where(DbSession.id == uuid.UUID(sid)))
