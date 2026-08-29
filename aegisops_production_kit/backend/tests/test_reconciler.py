"""B3 — stranded-run reconciler.

Integration (live Postgres + Redis). Proves the sweep decisions: a stranded executing run (no
heartbeat, not live) with no resumable checkpoint is marked failed honestly; a resumable one is
re-driven (through the supervisor, so A1 idempotency applies — no double apply); a live or
fresh-heartbeat run is skipped; and an awaiting_approval run is left for the human. The
kill-mid-apply case shows an in-flight `applying` run is recovered to a terminal state exactly
once — it is NOT blindly re-applied.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa

from app.agents.reconciler import Reconciler
from app.agents.supervisor import hb_key
from app.db.models import Run
from app.db.session import session_scope
from app.security import idempotency


class FakeSupervisor:
    def __init__(self, live=()):
        self._live = set(live)
        self.redriven: list[str] = []

    def is_live(self, run_id: str) -> bool:
        return run_id in self._live

    def run(self, run_id, drive):  # spy — do not actually execute the drive
        self.redriven.append(run_id)


async def _mk_run(org_id: str, status: str) -> str:
    async with session_scope() as s:
        run = Run(org_id=uuid.UUID(org_id), status=status, mode="apply")
        s.add(run)
        await s.flush()
        return str(run.id)


async def _status(run_id: str) -> str:
    async with session_scope() as s:
        return (await s.get(Run, uuid.UUID(run_id))).status


async def _cleanup(*run_ids: str):
    async with session_scope() as s:
        for rid in run_ids:
            await s.execute(sa.delete(Run).where(Run.id == uuid.UUID(rid)))


async def test_marks_stranded_nonresumable_run_failed(live_db, live_redis, org_id, monkeypatch):
    rid = await _mk_run(org_id, "running")
    await live_redis.delete(hb_key(rid))  # no heartbeat ⇒ stranded
    rec = Reconciler(supervisor=FakeSupervisor())
    monkeypatch.setattr(rec, "_is_resumable", lambda _r: _false())
    try:
        summary = await rec.sweep()
        assert await _status(rid) == "failed"
        assert summary["failed"] >= 1
    finally:
        await _cleanup(rid)


async def test_skips_live_and_heartbeat_runs(live_db, live_redis, org_id, monkeypatch):
    live_rid = await _mk_run(org_id, "running")
    hb_rid = await _mk_run(org_id, "running")
    await live_redis.set(hb_key(hb_rid), "1", ex=45)  # another worker's heartbeat
    rec = Reconciler(supervisor=FakeSupervisor(live=[live_rid]))
    # If either were (wrongly) treated as stranded, this would mark them failed.
    monkeypatch.setattr(rec, "_is_resumable", lambda _r: _false())
    try:
        await rec.sweep()
        assert await _status(live_rid) == "running", "a locally-live run must be skipped"
        assert await _status(hb_rid) == "running", "a fresh-heartbeat run must be skipped"
    finally:
        await live_redis.delete(hb_key(hb_rid))
        await _cleanup(live_rid, hb_rid)


async def test_leaves_awaiting_approval_untouched(live_db, live_redis, org_id):
    rid = await _mk_run(org_id, "awaiting_approval")
    rec = Reconciler(supervisor=FakeSupervisor())
    try:
        await rec.sweep()
        assert await _status(rid) == "awaiting_approval", "approval-paused runs are human-owned"
    finally:
        await _cleanup(rid)


async def test_resumable_stranded_run_is_redriven_not_failed(live_db, live_redis, org_id, monkeypatch):
    rid = await _mk_run(org_id, "running")
    await live_redis.delete(hb_key(rid))
    fake = FakeSupervisor()
    rec = Reconciler(supervisor=fake)
    monkeypatch.setattr(rec, "_is_resumable", lambda _r: _true())
    redriven: list[str] = []
    monkeypatch.setattr(rec, "_redrive", lambda run_id: _noop(redriven, run_id))
    try:
        summary = await rec.sweep()
        assert rid in redriven, "a resumable stranded run must be re-driven"
        assert summary["resumed"] >= 1
        assert await _status(rid) == "running", "a resumed run is NOT marked failed"
    finally:
        await _cleanup(rid)


async def test_kill_mid_apply_recovers_once_without_reapply(live_db, live_redis, org_id, monkeypatch):
    """A worker died mid-apply: the run is 'executing' (the durable engine's transient — the
    dead 'applying' literal was removed in P0/D5), its idempotency key is claimed, heartbeat
    gone. With no resumable checkpoint the reconciler marks it failed honestly — it does NOT
    re-apply. (For a resumable checkpoint, A1's wait-or-abort — tested in test_idempotency —
    guarantees the re-drive can't double-apply.)"""
    rid = await _mk_run(org_id, "executing")
    key = idempotency.make_key("tf-exec", rid, "apply")
    await idempotency.release(key)
    assert await idempotency.claim(key) is True  # the crashed worker's in-flight claim
    await live_redis.delete(hb_key(rid))
    rec = Reconciler(supervisor=FakeSupervisor())
    monkeypatch.setattr(rec, "_is_resumable", lambda _r: _false())
    try:
        await rec.sweep()
        assert await _status(rid) == "failed"
        # The reconciler never issued a second apply — the in-flight claim is untouched.
        assert await idempotency.is_in_progress(key) is True
    finally:
        await idempotency.release(key)
        await _cleanup(rid)


class ExecutingSupervisor(FakeSupervisor):
    """A fake that captures the drive coroutine so the test can execute it deterministically."""

    def __init__(self):
        super().__init__()
        self.drives: list = []

    def run(self, run_id, drive):
        self.redriven.append(run_id)
        self.drives.append(drive)


async def test_redrive_persists_the_result_and_second_sweep_is_a_noop(
        live_db, live_redis, org_id, monkeypatch):
    """Gate defect (2026-07-12): _redrive ran the graph but never persisted the result, so a
    successfully re-driven run stayed `running` and the NEXT sweep force-failed it — two
    reconciler actions for one run, and a redriven apply's outcome would have been stamped over.
    Now the redrive persists status + assistant message itself: recovery happens ONCE."""
    from sqlalchemy import delete, select as sa_select

    from app.db.models import Message, Session

    async with session_scope() as s:
        sess = Session(org_id=uuid.UUID(org_id), title="redrive-test")
        s.add(sess)
        await s.flush()
        sid = str(sess.id)
        run = Run(org_id=uuid.UUID(org_id), session_id=uuid.UUID(sid), status="running", mode="apply")
        s.add(run)
        await s.flush()
        rid = str(run.id)
    await live_redis.delete(hb_key(rid))

    async def _fake_run_graph(run_id, channel, initial=None, resume=None):
        return {"state": {"answer": "recovered heuristic answer", "domain": "general"},
                "interrupted": False, "error": None}

    from app.agents import runner as runner_mod
    monkeypatch.setattr(runner_mod, "run_graph", _fake_run_graph)

    fake = ExecutingSupervisor()
    rec = Reconciler(supervisor=fake)
    monkeypatch.setattr(rec, "_is_resumable", lambda _r: _true())
    try:
        first = await rec.sweep()
        assert rid in fake.redriven and first["resumed"] >= 1
        await fake.drives[-1]()  # execute the captured redrive drive

        # The redrive itself persisted the terminal state + the assistant message.
        assert await _status(rid) == "completed"
        async with session_scope() as s:
            msg = (await s.execute(sa_select(Message).where(
                Message.run_id == uuid.UUID(rid), Message.role == "assistant"))).scalar_one_or_none()
        assert msg is not None and "recovered heuristic answer" in msg.content

        # Second sweep: the run is terminal — NOT a candidate; no second recovery action.
        second = await rec.sweep()
        assert rid not in fake.redriven[1:], "no second redrive for the same run"
        assert await _status(rid) == "completed", "a recovered run is never re-marked failed"
    finally:
        async with session_scope() as s:
            await s.execute(delete(Message).where(Message.run_id == uuid.UUID(rid)))
            await s.execute(delete(Run).where(Run.id == uuid.UUID(rid)))
            await s.execute(delete(Session).where(Session.id == uuid.UUID(sid)))


# tiny async helpers so monkeypatched sync lambdas can return awaitables
async def _false():
    return False


async def _true():
    return True


async def _noop(sink, run_id):
    sink.append(run_id)
