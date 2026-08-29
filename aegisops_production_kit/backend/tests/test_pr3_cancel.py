"""PR-3 CANCEL — user-facing cancellation with `cancelled` as a first-class TERMINAL status.
Pre-approval → cooperative stop, terminal cancelled, nothing changed; DAG → halt-after-
current-step (never mid-apply), honest partial outcome; authz = initiator or approver;
the reconciler never re-drives a cancelled run and the B5 guard never overwrites it."""

from __future__ import annotations

import uuid

import pytest


# ── cancel flag primitives ─────────────────────────────────────────────────────────────────

async def test_cancel_flag_roundtrip(live_redis):
    from app.agents.supervisor import clear_cancel, cancel_key, is_cancelled
    from app.cache.redis import get_redis

    rid = str(uuid.uuid4())
    assert await is_cancelled(rid) is False
    await get_redis().set(cancel_key(rid), "1", ex=60)
    assert await is_cancelled(rid) is True
    await clear_cancel(rid)
    assert await is_cancelled(rid) is False


# ── exec loop: halt-after-current-step, honest partial ─────────────────────────────────────

async def test_dag_cancel_halts_after_current_step(live_redis, monkeypatch):
    """Two-step DAG, cancel flag set after step 1 applies → step 2 never starts; the outcome
    is a terminal `cancelled` naming what DID apply."""
    from app.agents import exec_loop
    from app.agents.supervisor import cancel_key
    from app.cache.redis import get_redis

    rid = str(uuid.uuid4())
    applied: list[int] = []

    async def _fake_step(state, step, index, config, observations):
        applied.append(index)
        # set the cancel flag right after step 0 applies — the loop must stop before step 1
        if index == 0:
            await get_redis().set(cancel_key(rid), "1", ex=60)
        return {"status": "applied", "template": step["template_key"],
                "name": step["template_key"], "outputs": {}}

    class _Em:
        def __getattr__(self, _n):
            async def _f(*a, **k): return None
            return _f
    monkeypatch.setattr(exec_loop, "execute_governed_step", _fake_step)
    monkeypatch.setattr(exec_loop, "emitter_of", lambda cfg: _Em())
    # This test pins the LEGACY in-process loop (flag-off path); the durable engine's
    # step-boundary cancellation has its own coverage in test_p3_activation.py.
    from app.settings import get_settings
    monkeypatch.setattr(get_settings(), "aegisops_durable_engine", "off")

    state = {"run_id": rid,
             "goal_dag": [{"template_key": "aws.vpc", "inputs": {}},
                          {"template_key": "aws.ec2", "inputs": {}}]}
    try:
        out = await exec_loop.execute_goal_dag(state, {})
        assert applied == [0]                       # step 2 NEVER started
        assert out["outcome"]["status"] == "cancelled"
        assert "step 2" in out["answer"] and "applied" in out["answer"]
    finally:
        from app.agents.supervisor import clear_cancel
        await clear_cancel(rid)


async def test_dag_without_cancel_runs_to_completion(live_redis, monkeypatch):
    from app.agents import exec_loop

    async def _fake_step(state, step, index, config, observations):
        return {"status": "applied", "template": step["template_key"],
                "name": step["template_key"], "outputs": {}}

    class _Em:
        def __getattr__(self, _n):
            async def _f(*a, **k): return None
            return _f
    monkeypatch.setattr(exec_loop, "execute_governed_step", _fake_step)
    monkeypatch.setattr(exec_loop, "emitter_of", lambda cfg: _Em())
    # Legacy in-process loop (flag-off path) — see the note in the cancel test above.
    from app.settings import get_settings
    monkeypatch.setattr(get_settings(), "aegisops_durable_engine", "off")

    state = {"run_id": str(uuid.uuid4()),
             "goal_dag": [{"template_key": "aws.vpc", "inputs": {}},
                          {"template_key": "aws.ec2", "inputs": {}}]}
    out = await exec_loop.execute_goal_dag(state, {})
    assert (out.get("outcome") or {}).get("status") != "cancelled"


# ── terminal-status correctness ─────────────────────────────────────────────────────────────

async def test_force_terminal_never_overwrites_cancelled(live_db, throwaway_org):
    from app.api.chat import _force_terminal
    from app.db.models import Run, Session as DbSession
    from app.db.session import session_scope
    from sqlalchemy import delete

    org = throwaway_org
    rid, sid = uuid.uuid4(), uuid.uuid4()
    async with session_scope() as s:
        s.add(DbSession(id=sid, org_id=uuid.UUID(org), title="pr3"))
        await s.flush()
        s.add(Run(id=rid, org_id=uuid.UUID(org), session_id=sid, status="cancelled",
                  mode="apply", outcome={"status": "cancelled"}))
    try:
        await _force_terminal(str(rid), "late failure")
        async with session_scope() as s:
            run = await s.get(Run, rid)
            assert run.status == "cancelled"        # B5 guard must not clobber it
    finally:
        async with session_scope() as s:
            await s.execute(delete(Run).where(Run.id == rid))
            await s.execute(delete(DbSession).where(DbSession.id == sid))


async def test_reconciler_ignores_cancelled_runs(live_db, live_redis, throwaway_org):
    """A cancelled run is terminal — it is NOT in the reconciler's executing scan set."""
    from app.agents.reconciler import EXECUTING_STATES
    assert "cancelled" not in EXECUTING_STATES
    # P0/D5 killed "applying"; Prompt 3 added the durable engine's transient statuses —
    # a run parked in any of these with a dead heartbeat is sweepable.
    assert set(EXECUTING_STATES) == {"running", "executing", "verifying", "scheduled"}


# ── endpoint: authz + state transitions ─────────────────────────────────────────────────────

async def test_cancel_awaiting_approval_marks_terminal(live_db, live_redis, throwaway_org,
                                                       monkeypatch):
    from app.api import chat
    from app.db.models import Run, Session as DbSession, User
    from app.db.session import session_scope
    from app.security.deps import get_current_user
    from sqlalchemy import delete

    org = throwaway_org
    rid, sid, uid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with session_scope() as s:
        s.add(User(id=uid, org_id=uuid.UUID(org), username="initiator"))
        s.add(DbSession(id=sid, org_id=uuid.UUID(org), title="pr3"))
        await s.flush()
        s.add(Run(id=rid, org_id=uuid.UUID(org), session_id=sid,
                  status="awaiting_approval", mode="apply", initiated_by=uid))

    class _U:
        user_id = str(uid); username = "initiator"; can_execute = False
        org_id = org
        def model_dump(self): return {}
    try:
        out = await chat.cancel_run(str(rid), user=_U())
        assert out["status"] == "cancelling"
        async with session_scope() as s:
            run = await s.get(Run, rid)
            assert run.status == "cancelled"        # awaiting_approval flips directly
    finally:
        async with session_scope() as s:
            await s.execute(delete(Run).where(Run.id == rid))
            await s.execute(delete(DbSession).where(DbSession.id == sid))
            await s.execute(delete(User).where(User.id == uid))


async def test_cancel_terminal_run_is_a_noop(live_db, live_redis, throwaway_org):
    from app.api import chat
    from app.db.models import Run, Session as DbSession, User
    from app.db.session import session_scope
    from sqlalchemy import delete

    org = throwaway_org
    rid, sid, uid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with session_scope() as s:
        s.add(User(id=uid, org_id=uuid.UUID(org), username="init"))
        s.add(DbSession(id=sid, org_id=uuid.UUID(org), title="pr3"))
        await s.flush()
        s.add(Run(id=rid, org_id=uuid.UUID(org), session_id=sid, status="completed",
                  mode="apply", initiated_by=uid))

    class _U:
        user_id = str(uid); username = "init"; can_execute = True
        org_id = org
        def model_dump(self): return {}
    try:
        out = await chat.cancel_run(str(rid), user=_U())
        assert out["status"] == "completed" and "no-op" in out["note"]
    finally:
        async with session_scope() as s:
            await s.execute(delete(Run).where(Run.id == rid))
            await s.execute(delete(DbSession).where(DbSession.id == sid))
            await s.execute(delete(User).where(User.id == uid))


async def test_cancel_forbidden_for_a_bystander(live_db, live_redis, throwaway_org):
    from fastapi import HTTPException

    from app.api import chat
    from app.db.models import Run, Session as DbSession, User
    from app.db.session import session_scope
    from sqlalchemy import delete

    org = throwaway_org
    rid, sid, owner, other = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with session_scope() as s:
        s.add(User(id=owner, org_id=uuid.UUID(org), username="owner"))
        s.add(User(id=other, org_id=uuid.UUID(org), username="bystander"))
        s.add(DbSession(id=sid, org_id=uuid.UUID(org), title="pr3"))
        await s.flush()
        s.add(Run(id=rid, org_id=uuid.UUID(org), session_id=sid, status="running",
                  mode="apply", initiated_by=owner))

    class _Bystander:
        user_id = str(other); username = "bystander"; can_execute = False  # not initiator, not approver
        org_id = org
        def model_dump(self): return {}
    try:
        with pytest.raises(HTTPException) as ei:
            await chat.cancel_run(str(rid), user=_Bystander())
        assert ei.value.status_code == 403
    finally:
        async with session_scope() as s:
            await s.execute(delete(Run).where(Run.id == rid))
            await s.execute(delete(DbSession).where(DbSession.id == sid))
            await s.execute(delete(User).where(User.id.in_([owner, other])))
