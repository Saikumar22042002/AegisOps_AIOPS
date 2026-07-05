"""Provisioned-resource inventory + reference resolution (6.1 / 6.2 day-2 categories).

Covers persistence after apply and every resolution kind a day-2 request exercises:
exact name ("test-vm"), context ("the instance I created"), ambiguous (→ ask), not-found
(→ ask, never guess), and destroyed (no longer resolvable). Uses live Postgres; rows are
namespaced with an `itest-` prefix and cleaned up before/after.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa

from app.agents import inventory, templates
from app.db.models import Resource
from app.db.session import session_scope

_PREFIX = "itest-inv-"


async def _cleanup(org: str) -> None:
    async with session_scope() as s:
        await s.execute(sa.delete(Resource).where(
            Resource.org_id == uuid.UUID(org), Resource.name.like(f"{_PREFIX}%")))


@pytest.fixture
async def clean_inventory(org_id):
    await _cleanup(org_id)
    yield org_id
    await _cleanup(org_id)


async def _record(org: str, name: str, outputs: dict, inputs: dict | None = None) -> None:
    t = templates.select("aws", "ec2")  # a real template
    state = {"org_id": org, "session_id": None, "run_id": None,
             "parsed_inputs": {"name": name, "instance_type": "t3.micro", **(inputs or {})}}
    await inventory.record_from_apply(state, t, outputs)


async def test_record_then_resolve_by_exact_name(clean_inventory):
    org = clean_inventory
    name = _PREFIX + "web"
    await _record(org, name, {"instance_id": "i-0itest01", "vpc_id": "vpc-0itest",
                              "subnet_id": "subnet-0itest", "public_ip": "203.0.113.10"})
    matches, kind = await inventory.resolve(org, name)
    assert kind == "name"
    assert len(matches) == 1
    r = matches[0]
    assert r["provider_id"] == "i-0itest01"
    assert r["attributes"]["vpc_id"] == "vpc-0itest"          # real recorded value, not a discovery
    assert r["attributes"]["subnet_id"] == "subnet-0itest"
    assert r["cloud"] == "aws" and r["resource_type"] == "ec2"


async def test_resolve_by_context_recent(clean_inventory):
    org = clean_inventory
    name = _PREFIX + "recent-vm"
    await _record(org, name, {"instance_id": "i-0recent"})
    matches, kind = await inventory.resolve(org, "the instance I just created")
    assert kind == "recent"
    assert matches and matches[0]["resource_type"] == "ec2"


async def test_ambiguous_reference_returns_all_matches(clean_inventory):
    org = clean_inventory
    await _record(org, _PREFIX + "api-a", {"instance_id": "i-a"})
    await _record(org, _PREFIX + "api-b", {"instance_id": "i-b"})
    matches, kind = await inventory.resolve(org, _PREFIX + "api")   # substring hits both
    assert kind == "name"
    assert {m["name"] for m in matches} == {_PREFIX + "api-a", _PREFIX + "api-b"}
    assert len(matches) == 2                                        # caller must disambiguate


async def test_not_found_returns_none(clean_inventory):
    org = clean_inventory
    matches, kind = await inventory.resolve(org, _PREFIX + "ghost-server-nope")
    assert matches == [] and kind == "none"


async def test_destroyed_no_longer_resolvable(clean_inventory):
    org = clean_inventory
    name = _PREFIX + "doomed"
    await _record(org, name, {"instance_id": "i-0doomed"})
    assert (await inventory.resolve(org, name))[0]                  # resolvable while active
    await inventory.mark_destroyed(org, "aws-ec2", name=name)
    matches, kind = await inventory.resolve(org, name)
    assert matches == [] and kind == "none"                         # not offered for day-2 ops


async def test_upsert_keeps_single_active_row(clean_inventory):
    org = clean_inventory
    name = _PREFIX + "upsert"
    await _record(org, name, {"instance_id": "i-old", "public_ip": "1.1.1.1"})
    await _record(org, name, {"instance_id": "i-new", "public_ip": "2.2.2.2"})  # re-apply
    matches, _ = await inventory.resolve(org, name)
    assert len(matches) == 1                                        # upsert, not duplicate
    assert matches[0]["provider_id"] == "i-new"                     # latest values win


def test_name_from_inputs_precedence():
    assert inventory.name_from_inputs({"name": "vm1"}, "ec2") == "vm1"
    assert inventory.name_from_inputs({"bucket_name": "b1"}, "s3") == "b1"
    assert inventory.name_from_inputs({"identifier": "db1"}, "rds") == "db1"
    assert inventory.name_from_inputs({}, "vpc") == "vpc"           # falls back to the type


# ═══════════════════════════════════════════════════════════════════════════════════════════
# Phase 7 / BUG-04 — broad inventory queries + type-safe context recall.
# ═══════════════════════════════════════════════════════════════════════════════════════════

def test_broad_reference_detection():
    for ref in ("all resources", "any resources", "all", "everything", "my resources", "*",
                "all the resources i created"):
        assert inventory._is_broad(ref), f"should be broad: {ref!r}"
    for ref in ("test-vm", "the instance I created", "the resource group rg-app", "sai-test"):
        assert not inventory._is_broad(ref), f"should NOT be broad: {ref!r}"


async def test_broad_reference_lists_everything(clean_inventory):
    # Screenshot 14/16: "Did I create any resources…" (target "all resources") must list ALL
    # active resources across clouds — never the "couldn't find 'all resources'" refusal.
    org = clean_inventory
    await _record(org, _PREFIX + "vm-a", {"instance_id": "i-a"})
    await _record(org, _PREFIX + "vm-b", {"instance_id": "i-b"})
    matches, kind = await inventory.resolve(org, "all resources")
    assert kind == "all"
    ours = [m for m in matches if m["name"].startswith(_PREFIX)]
    assert {m["name"] for m in ours} == {_PREFIX + "vm-a", _PREFIX + "vm-b"}
    # Broad with an EMPTY inventory is still kind="all" (renders an honest "nothing yet",
    # not a not-found refusal).
    await inventory.mark_destroyed(org, "aws-ec2", name=_PREFIX + "vm-a")
    await inventory.mark_destroyed(org, "aws-ec2", name=_PREFIX + "vm-b")
    matches2, kind2 = await inventory.resolve(org, "everything")
    assert kind2 == "all"
    assert not [m for m in matches2 if m["name"].startswith(_PREFIX)]


async def test_context_recall_is_type_safe(clean_inventory):
    # Screenshot 5: "did the previous S3 BUCKET get created?" was answered with the EC2.
    # A typed descriptive reference must only match that type — or nothing (honest).
    org = clean_inventory
    await _record(org, _PREFIX + "only-vm", {"instance_id": "i-vm"})  # an ec2, no buckets
    matches, kind = await inventory.resolve(org, "the s3 bucket I created earlier")
    assert matches == [] and kind == "none"     # never the EC2
    matches2, _ = await inventory.resolve(org, "the instance I created earlier")
    assert matches2 and matches2[0]["name"] == _PREFIX + "only-vm"   # typed match still works


async def test_list_active_filters_by_cloud(clean_inventory):
    org = clean_inventory
    await _record(org, _PREFIX + "aws-vm", {"instance_id": "i-x"})
    mine = await inventory.list_active(org, clouds=["aws"])
    assert any(m["name"] == _PREFIX + "aws-vm" for m in mine)
    assert await inventory.list_active(org, clouds=["azure"]) == [] or all(
        m["cloud"] == "azure" for m in await inventory.list_active(org, clouds=["azure"]))
