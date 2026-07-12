"""D3 — World Model: org-scoped resource graph, real dependency edges, impact_of destroy gate.

Integration (live Neo4j — the same compose service the app uses). Dependency edges come from
a pure extraction over the resource's REAL inputs/outputs, so an edge can never be hallucinated;
impact_of answers "what depends on this?" before a destroy.
"""

from __future__ import annotations

import uuid

import pytest

from app.graph_db import world_model


@pytest.fixture
async def wm_schema(live_neo4j):
    """World-model constraints on the shared live-Neo4j fixture."""
    await world_model.ensure_schema()
    yield


def _payload(org, name, rtype, pid, inputs=None, attrs=None):
    return {"name": name, "cloud": "aws", "resource_type": rtype, "workspace": f"aws-{rtype}",
            "state_workspace": name, "region": "us-east-1", "provider_id": pid,
            "attributes": attrs or {}, "inputs": inputs or {},
            "session_id": None, "run_id": None}


async def _cleanup(*pids: str):
    await world_model._run(
        "MATCH (r:Resource) WHERE r.provider_id IN $pids DETACH DELETE r", pids=list(pids))


# ── pure extraction ────────────────────────────────────────────────────────────────────────

def test_dependencies_from_extracts_real_parent_refs():
    payload = _payload("o", "web-1", "ec2", "i-1",
                       inputs={"vpc_id": "vpc-abc", "subnet_id": "subnet-1", "name": "web-1"},
                       attrs={"security_group_ids": ["sg-1", "sg-2"]})
    deps = world_model.dependencies_from(payload)
    refs = {(d["ref"], d["kind"]) for d in deps}
    assert refs == {("vpc-abc", "vpc"), ("subnet-1", "subnet"),
                    ("sg-1", "security_group"), ("sg-2", "security_group")}


def test_dependencies_from_skips_placeholders_and_dedups():
    payload = _payload("o", "x", "ec2", "i-2",
                       inputs={"vpc_id": "default", "subnet_id": ""},
                       attrs={"vpc_id": "vpc-abc", "subnet_ids": ["vpc-abc"]})
    deps = world_model.dependencies_from(payload)
    assert [d["ref"] for d in deps] == ["vpc-abc"]  # placeholder + empty skipped, ref de-duped


def test_dependencies_from_azure_resource_group():
    payload = _payload("o", "store1", "storage", "sa-1",
                       inputs={"resource_group": "rg-payments"})
    assert world_model.dependencies_from(payload) == [{"ref": "rg-payments", "kind": "resource_group"}]


# ── graph integration ─────────────────────────────────────────────────────────────────────

async def test_impact_of_names_active_dependents(wm_schema):
    org = str(uuid.uuid4())
    try:
        await world_model.upsert_resource(org, _payload(org, "net-1", "vpc", "vpc-100"))
        await world_model.upsert_resource(
            org, _payload(org, "web-1", "ec2", "i-100", inputs={"vpc_id": "vpc-100"}))
        deps = await world_model.impact_of(org, provider_id="vpc-100")
        assert [(d["name"], d["kind"]) for d in deps] == [("web-1", "vpc")]
        # …and by name too (the destroy path may only have a name).
        by_name = await world_model.impact_of(org, name="net-1")
        assert [d["name"] for d in by_name] == ["web-1"]
    finally:
        await _cleanup("vpc-100", "i-100")


async def test_impact_of_is_org_scoped(wm_schema):
    org_a, org_b = str(uuid.uuid4()), str(uuid.uuid4())
    try:
        await world_model.upsert_resource(org_a, _payload(org_a, "net-2", "vpc", "vpc-200"))
        await world_model.upsert_resource(
            org_a, _payload(org_a, "web-2", "ec2", "i-200", inputs={"vpc_id": "vpc-200"}))
        assert await world_model.impact_of(org_b, provider_id="vpc-200") == []
    finally:
        await _cleanup("vpc-200", "i-200")


async def test_destroyed_dependent_no_longer_counts(wm_schema):
    org = str(uuid.uuid4())
    try:
        await world_model.upsert_resource(org, _payload(org, "net-3", "vpc", "vpc-300"))
        await world_model.upsert_resource(
            org, _payload(org, "web-3", "ec2", "i-300", inputs={"vpc_id": "vpc-300"}))
        await world_model.mark_destroyed(org, provider_id="i-300")
        assert await world_model.impact_of(org, provider_id="vpc-300") == []
    finally:
        await _cleanup("vpc-300", "i-300")


async def test_external_parent_gets_a_stub_never_a_fake_managed_node(wm_schema):
    """Depending on a VPC AegisOps did NOT create still records a real edge — the parent is a
    status='external' stub, and destroying it would still warn about the dependent."""
    org = str(uuid.uuid4())
    try:
        await world_model.upsert_resource(
            org, _payload(org, "web-4", "ec2", "i-400", inputs={"vpc_id": "vpc-external-9"}))
        deps = await world_model.impact_of(org, provider_id="vpc-external-9")
        assert [d["name"] for d in deps] == ["web-4"]
        rows = await world_model._run(
            "MATCH (r:Resource {provider_id:'vpc-external-9'}) RETURN r.status AS status")
        assert rows and rows[0]["status"] == "external"
    finally:
        await _cleanup("vpc-external-9", "i-400")


async def test_destroy_card_impact_check_states_the_dependents(wm_schema):
    """The policy-check row the destroy card renders: failed + named dependents; passed only
    when the graph was consulted and found none; pending when the graph is unreachable."""
    from app.agents.cloudops import _world_model_impact_check

    org = str(uuid.uuid4())
    try:
        await world_model.upsert_resource(org, _payload(org, "net-5", "vpc", "vpc-500"))
        await world_model.upsert_resource(
            org, _payload(org, "web-5", "ec2", "i-500", inputs={"vpc_id": "vpc-500"}))
        check, dependents = await _world_model_impact_check(org, provider_id="vpc-500", name="net-5")
        assert check["passed"] is False and check["evaluated"] is True
        assert "web-5" in check["detail"] and len(dependents) == 1

        ok_check, none_deps = await _world_model_impact_check(org, provider_id="i-500", name="web-5")
        assert ok_check["passed"] is True and none_deps == []
    finally:
        await _cleanup("vpc-500", "i-500")


async def test_impact_check_unreachable_graph_is_pending_not_a_pass(wm_schema, monkeypatch):
    from app.agents.cloudops import _world_model_impact_check
    from app.graph_db import world_model as wm

    async def _boom(*a, **k):
        raise RuntimeError("graph down")

    monkeypatch.setattr(wm, "impact_of", _boom)
    check, deps = await _world_model_impact_check(str(uuid.uuid4()), provider_id="x", name="y")
    assert check["evaluated"] is False and check["passed"] is None and deps == []
