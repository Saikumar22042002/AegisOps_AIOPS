"""B2 — RunSupervisor: tracked run tasks + Redis heartbeat + graceful drain.

Integration (live Redis + Postgres). Proves: a supervised run is `is_live` with a heartbeat key
present; a normal completion deregisters (heartbeat removed); and a shutdown drain cancels an
in-flight run and persists it `failed` (never silently dropped).
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from app.agents.supervisor import RunSupervisor, hb_key


async def test_supervised_run_is_live_with_heartbeat_then_deregisters(live_redis):
    sup = RunSupervisor()
    run_id = f"itest-sup-{uuid.uuid4()}"
    started = asyncio.Event()
    release = asyncio.Event()

    async def _drive():
        started.set()
        await release.wait()

    sup.run(run_id, _drive)
    await asyncio.wait_for(started.wait(), timeout=5)
    await asyncio.sleep(0.1)  # let the heartbeat task set its key

    assert sup.is_live(run_id)
    assert await live_redis.get(hb_key(run_id)) == "1", "a live run must have a heartbeat key"

    release.set()  # let the drive finish normally
    await asyncio.sleep(0.2)
    assert not sup.is_live(run_id)
    assert await live_redis.get(hb_key(run_id)) is None, "heartbeat must be cleared on deregister"


async def test_drain_cancels_and_marks_inflight_run_failed(live_db, live_redis, org_id):
    from sqlalchemy import delete

    from app.db.models import Run
    from app.db.session import session_scope

    async with session_scope() as s:
        run = Run(org_id=uuid.UUID(org_id), status="running", mode="apply")
        s.add(run)
        await s.flush()
        run_id = str(run.id)

    sup = RunSupervisor()
    started = asyncio.Event()

    async def _never_ends():
        started.set()
        await asyncio.sleep(3600)  # simulate a run in flight at shutdown

    sup.run(run_id, _never_ends)
    await asyncio.wait_for(started.wait(), timeout=5)
    assert sup.is_live(run_id)

    await sup.drain()  # shutdown

    assert not sup.is_live(run_id)
    assert await live_redis.get(hb_key(run_id)) is None
    try:
        async with session_scope() as s:
            row = await s.get(Run, uuid.UUID(run_id))
            assert row.status == "failed", "an in-flight run must persist as failed on drain"
            assert (row.outcome or {}).get("status") == "failed"
    finally:
        async with session_scope() as s:
            await s.execute(delete(Run).where(Run.id == uuid.UUID(run_id)))
