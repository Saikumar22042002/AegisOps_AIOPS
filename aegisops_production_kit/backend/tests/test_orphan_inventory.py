"""D2 — same-transaction inventory write + orphan sweeper: a real applied resource is never
invisible.

The apply mutates real infrastructure and cannot be rolled back, so the inventory row and the
run outcome are written together, and the outcome carries a self-contained recovery payload
(`_inventory`). If the row write is interrupted (crash injected), the orphan sweeper rebuilds
the row from the run alone — no cloud read.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, select

from app.agents import inventory
from app.agents.reconciler import Reconciler
from app.db.models import Resource, Run
from app.db.session import session_scope


class _Template:
    cloud = "aws"
    resource = "ec2"
    workspace = "aws-ec2"


def _state(org_id: str, run_id: str):
    return {"org_id": org_id, "run_id": run_id, "session_id": None,
            "state_workspace": "run-" + run_id[:8],
            "parsed_inputs": {"name": "orphan-vm", "region": "us-east-1"},
            "user": {"region": "us-east-1"}}


async def _make_applied_run(org_id: str, *, with_inventory_row: bool) -> str:
    """Create an 'applied' run whose outcome carries the recovery payload. When
    with_inventory_row is False we simulate the crash-injected orphan: outcome persisted, the
    Resource row never written."""
    run_id = str(uuid.uuid4())
    state = _state(org_id, run_id)
    payload = inventory.inventory_payload(state, _Template, {"instance_id": "i-0abc123"})
    outcome = {"status": "applied", "outputs": {"instance_id": "i-0abc123"}, "_inventory": payload}
    async with session_scope() as s:
        run = Run(id=uuid.UUID(run_id), org_id=uuid.UUID(org_id), status="completed",
                  mode="apply", domain="cloudops", outcome=outcome)
        s.add(run)
        if with_inventory_row:
            await inventory.upsert_resource(s, org_id, payload)
    return run_id


async def _cleanup(org_id: str, run_id: str) -> None:
    async with session_scope() as s:
        await s.execute(delete(Resource).where(Resource.run_id == uuid.UUID(run_id)))
        await s.execute(delete(Run).where(Run.id == uuid.UUID(run_id)))


async def _active_row(org_id: str, run_id: str):
    async with session_scope() as s:
        return (await s.execute(select(Resource).where(
            Resource.run_id == uuid.UUID(run_id), Resource.status == "active"))).scalar_one_or_none()


async def test_outcome_carries_recovery_payload():
    """The payload embedded in the outcome is enough to rebuild the row from the run alone."""
    payload = inventory.inventory_payload(
        _state("11111111-1111-1111-1111-111111111111", str(uuid.uuid4())),
        _Template, {"instance_id": "i-0abc123"})
    assert payload["name"] == "orphan-vm" and payload["workspace"] == "aws-ec2"
    assert payload["provider_id"] == "i-0abc123" and payload["cloud"] == "aws"


async def test_orphan_is_recovered_by_the_sweeper(live_db, org_id):
    """Crash-inject: outcome=applied but no inventory row → sweeper rebuilds it from the payload."""
    run_id = await _make_applied_run(org_id, with_inventory_row=False)
    try:
        assert await _active_row(org_id, run_id) is None  # invisible orphan before the sweep
        summary = await Reconciler().sweep_orphans()
        assert summary["recovered"] >= 1
        row = await _active_row(org_id, run_id)
        assert row is not None and row.name == "orphan-vm" and row.provider_id == "i-0abc123"
    finally:
        await _cleanup(org_id, run_id)


async def test_sweeper_is_idempotent_and_skips_visible_resources(live_db, org_id):
    """A run whose row already exists is not re-created; a second sweep recovers nothing new."""
    run_id = await _make_applied_run(org_id, with_inventory_row=True)
    try:
        first = await Reconciler().sweep_orphans()
        # This run contributed nothing (already visible); recover it → nothing.
        second = await Reconciler().sweep_orphans()
        assert second["recovered"] == first["recovered"]  # stable — no duplicate rows created
        async with session_scope() as s:
            rows = (await s.execute(select(Resource).where(
                Resource.run_id == uuid.UUID(run_id), Resource.status == "active"))).scalars().all()
        assert len(rows) == 1  # exactly one, never duplicated
    finally:
        await _cleanup(org_id, run_id)


async def test_destroyed_resource_is_never_resurrected(live_db, org_id):
    """BUGFIX-4 (found live 2026-07-14): after a gated destroy, the apply-run's recovery
    payload must NOT read as an orphan — the guard used to match only ACTIVE rows, so the
    sweeper resurrected destroyed resources as duplicate active rows (live: accept-key /
    accept-gnet / accept-gvm each ended up destroyed + active). Recovery is only for rows
    that never got written; a row in ANY status means the lifecycle is known."""
    run_id = await _make_applied_run(org_id, with_inventory_row=True)
    try:
        async with session_scope() as s:
            await inventory.mark_destroyed_txn(s, org_id, "aws-ec2", name="orphan-vm")
        assert await _active_row(org_id, run_id) is None           # destroyed → invisible
        await Reconciler().sweep_orphans()
        assert await _active_row(org_id, run_id) is None           # STILL invisible — no ghost
        async with session_scope() as s:
            rows = (await s.execute(select(Resource).where(
                Resource.run_id == uuid.UUID(run_id)))).scalars().all()
        assert len(rows) == 1 and rows[0].status == "destroyed"    # exactly the destroyed row
    finally:
        await _cleanup(org_id, run_id)


async def test_unreachable_resource_is_never_resurrected(live_db, org_id):
    """Same guard, inventory-honesty flavor: an `unreachable` row (rotated-away sandbox
    account) must not be resurrected by the sweeper either."""
    run_id = await _make_applied_run(org_id, with_inventory_row=True)
    try:
        async with session_scope() as s:
            row = (await s.execute(select(Resource).where(
                Resource.run_id == uuid.UUID(run_id)))).scalars().one()
            row.status = "unreachable"
        await Reconciler().sweep_orphans()
        assert await _active_row(org_id, run_id) is None
    finally:
        await _cleanup(org_id, run_id)


async def test_recover_missing_noop_without_payload(live_db, org_id):
    """A legacy applied run with no recovery payload is left alone (nothing to rebuild from)."""
    run_id = str(uuid.uuid4())
    async with session_scope() as s:
        s.add(Run(id=uuid.UUID(run_id), org_id=uuid.UUID(org_id), status="completed",
                  mode="apply", outcome={"status": "applied"}))  # no _inventory
    try:
        async with session_scope() as s:
            run = await s.get(Run, uuid.UUID(run_id))
            assert await inventory.recover_missing(s, run) is False
    finally:
        await _cleanup(org_id, run_id)
