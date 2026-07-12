"""D3 — reconciliation engine: drift, deleted-outside, and orphan findings → notifications.

Integration (live Postgres + Redis + Neo4j). Cloud reads are faked at the reader seam — the
comparator, dedup, notification, and world-model annotation pipeline is fully real. Live cloud
sweeps are DLV items.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, select

from app.agents import drift
from app.agents.drift import MISSING, detect_drift
from app.db.models import Notification, Resource
from app.db.session import session_scope


# ── pure comparator ────────────────────────────────────────────────────────────────────────

def test_detect_drift_flags_changed_curated_fields():
    recorded = {"instance_type": "t3.micro", "state": "running", "security_groups": ["sg-1"]}
    live = {"instance_type": "t3.large", "state": "running", "security_groups": ["sg-1"]}
    diffs = detect_drift("ec2", recorded, live)
    assert diffs == [{"field": "instance_type", "recorded": "t3.micro", "live": "t3.large"}]


def test_detect_drift_lists_compare_order_insensitively():
    recorded = {"security_groups": ["sg-1", "sg-2"]}
    live = {"security_groups": ["sg-2", "sg-1"]}
    assert detect_drift("ec2", recorded, live) == []


def test_detect_drift_never_invents_unrecorded_fields():
    # We never captured instance_type at apply time → it cannot honestly be called drifted.
    assert detect_drift("ec2", {}, {"instance_type": "t3.large"}) == []


def test_detect_drift_sg_ports():
    recorded = {"ingress_ports": [22, 443]}
    live = {"ingress_ports": [22, 443, 3389]}  # someone opened RDP in the console
    diffs = detect_drift("security_group", recorded, live)
    assert diffs and diffs[0]["field"] == "ingress_ports"


# ── sweep pipeline with fake readers ───────────────────────────────────────────────────────

class FakeReader:
    def __init__(self, live_by_pid: dict, managed: list | None = None):
        self.live_by_pid = live_by_pid
        self.managed = managed or []

    async def read(self, resource):
        return self.live_by_pid.get(resource.get("provider_id"), MISSING)

    async def list_managed(self):
        return self.managed


async def _seed_resource(org_id: str, name: str, pid: str, attrs: dict) -> str:
    async with session_scope() as s:
        row = Resource(org_id=uuid.UUID(org_id), name=name, cloud="aws", resource_type="ec2",
                       workspace="aws-ec2", provider_id=pid, status="active", attributes=attrs)
        s.add(row)
        await s.flush()
        return str(row.id)


async def _notifications(org_id: str) -> list[Notification]:
    async with session_scope() as s:
        return list((await s.execute(
            select(Notification).where(Notification.org_id == uuid.UUID(org_id))
            .order_by(Notification.created_at))).scalars())


async def _cleanup(org_id: str, *row_ids: str):
    async with session_scope() as s:
        for rid in row_ids:
            await s.execute(delete(Resource).where(Resource.id == uuid.UUID(rid)))
        await s.execute(delete(Notification).where(Notification.org_id == uuid.UUID(org_id)))


@pytest.fixture
async def clean_fingerprints(live_redis):
    keys = [k async for k in live_redis.scan_iter("drift:fp:*")]
    if keys:
        await live_redis.delete(*keys)
    yield
    keys = [k async for k in live_redis.scan_iter("drift:fp:*")]
    if keys:
        await live_redis.delete(*keys)


async def test_drift_finding_notifies_the_org_once(live_db, live_redis, live_neo4j,
                                                   throwaway_org, clean_fingerprints):
    org_id = throwaway_org
    rid = await _seed_resource(org_id, "drift-vm", "i-drift1",
                               {"instance_type": "t3.micro", "security_groups": ["sg-1"]})
    readers = {("aws", "ec2"): FakeReader({"i-drift1": {"instance_type": "t3.large",
                                                        "security_groups": ["sg-1"]}})}
    try:
        first = await drift.sweep(readers, org_id=org_id)
        assert first["drift"] == 1 and first["checked"] >= 1
        notes = await _notifications(org_id)
        assert any("Drift detected: drift-vm" in n.title and "t3.large" in (n.body or "")
                   for n in notes)
        # Same finding again → deduplicated: no second notification, count 0.
        second = await drift.sweep(readers, org_id=org_id)
        assert second["drift"] == 0
        assert len(await _notifications(org_id)) == len(notes)
    finally:
        await _cleanup(org_id, rid)


async def test_deleted_outside_is_a_red_finding(live_db, live_redis, live_neo4j,
                                                throwaway_org, clean_fingerprints):
    org_id = throwaway_org
    rid = await _seed_resource(org_id, "gone-vm", "i-gone1", {"instance_type": "t3.micro"})
    readers = {("aws", "ec2"): FakeReader({})}  # reader returns MISSING for everything
    try:
        summary = await drift.sweep(readers, org_id=org_id)
        assert summary["deleted_outside"] == 1
        notes = await _notifications(org_id)
        target = next(n for n in notes if "Deleted outside AegisOps: gone-vm" in n.title)
        assert target.color == "var(--red)" and "deleted outside AegisOps" in (target.body or "")
    finally:
        await _cleanup(org_id, rid)


async def test_orphan_tagged_managed_but_untracked(live_db, live_redis, live_neo4j,
                                                   throwaway_org, clean_fingerprints):
    """P14: a live resource tagged ManagedBy=aegisops with no active inventory row is an orphan
    (it bills with nothing tracking it) — the sweep raises it."""
    org_id = throwaway_org
    rid = await _seed_resource(org_id, "tracked-vm", "i-tracked1", {"instance_type": "t3.micro"})
    readers = {("aws", "ec2"): FakeReader(
        {"i-tracked1": {"instance_type": "t3.micro"}},
        managed=[{"provider_id": "i-tracked1", "name": "tracked-vm"},
                 {"provider_id": "i-orphan9", "name": "forgotten-vm"}])}
    try:
        summary = await drift.sweep(readers, org_id=org_id)
        assert summary["orphans"] == 1
        notes = await _notifications(org_id)
        assert any("Orphaned resource: i-orphan9" in n.title for n in notes)
    finally:
        await _cleanup(org_id, rid)


async def test_types_without_a_reader_are_skipped_not_guessed(live_db, live_redis,
                                                              throwaway_org, clean_fingerprints):
    org_id = throwaway_org
    rid = await _seed_resource(org_id, "noreader-vm", "i-nr1", {"instance_type": "t3.micro"})
    try:
        summary = await drift.sweep(readers={}, org_id=org_id)  # no readers registered at all
        assert summary["skipped"] >= 1 and summary["checked"] == 0
        assert summary["drift"] == 0 and summary["deleted_outside"] == 0
        assert await _notifications(org_id) == []  # nothing fabricated
    finally:
        await _cleanup(org_id, rid)
