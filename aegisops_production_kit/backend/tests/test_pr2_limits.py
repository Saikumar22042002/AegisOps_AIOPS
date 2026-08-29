"""PR-2 LIMITS — concurrency (heartbeat-derived, self-healing) + per-stage subprocess
timeouts (process-group kill, honest terminal failure). No drifting counter; no run row
leaked at the limit; a sleeping runner fails within bound, never hangs a worker."""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest

from app.settings import get_settings
from app.tools.console import CommandConsole


# ── PR-2b: per-stage subprocess timeout kills the whole process group ──────────────────────

async def test_console_timeout_kills_process_group_within_bound():
    console = CommandConsole()
    started = time.monotonic()
    # a shell that spawns a child and sleeps far past the budget; the group kill must reap both
    res = await console.run(["sh", "-c", "sleep 60 & sleep 60"], timeout=2.0)
    elapsed = time.monotonic() - started
    assert res.returncode == 124                    # honest timeout code
    assert elapsed < 20                             # TERM→grace(10s)→KILL, never a hang
    assert any("timed out after 2" in ln for ln in res.stderr)


async def test_console_normal_command_unaffected():
    res = await CommandConsole().run(["sh", "-c", "echo ok"], timeout=30)
    assert res.returncode == 0 and any("ok" in ln for ln in res.stdout)


def test_runner_classifies_a_timeout_honestly():
    """rc 124 from a stage → an honest TerraformError naming the budget + the reconcile path,
    not a generic 'failed'."""
    from app.tools.terraform import TerraformError, TerraformRunner

    r = TerraformRunner("aws-ec2", get_settings())

    class _Res:
        returncode = 124
        stderr = ["irrelevant"]

    with pytest.raises(TerraformError) as ei:
        r._raise_stage_failure("apply", _Res(), 2700)
    msg = str(ei.value)
    assert "exceeded 45m" in msg and "process group killed" in msg
    assert "reconciler" in msg and "TF_FORCE_UNLOCK" in msg

    class _Fail:
        returncode = 1
        stderr = ["boom"]
    with pytest.raises(TerraformError) as ei2:
        r._raise_stage_failure("plan", _Fail(), 600)
    assert "plan failed" in str(ei2.value) and "boom" in str(ei2.value)


def test_stage_timeout_settings_exist():
    s = get_settings()
    assert s.tf_plan_timeout_s > 0 and s.tf_apply_timeout_s > s.tf_plan_timeout_s
    assert s.max_active_runs_per_org >= s.max_active_runs_per_user >= 1


# ── PR-2a: heartbeat-derived concurrency counts (self-healing, no drift) ───────────────────

async def test_active_counts_ignore_stale_rows(live_db, live_redis, throwaway_org):
    """A non-terminal run WITHOUT a fresh heartbeat is a crashed worker — it must NOT count
    (the binding self-heal: no lock-out after a crash)."""
    from app.agents.supervisor import hb_key
    from app.api.chat import _active_run_counts
    from app.cache.redis import get_redis
    from app.db.models import Run, Session as DbSession, User
    from app.db.session import session_scope
    from sqlalchemy import delete

    org = throwaway_org
    live_id, stale_id = uuid.uuid4(), uuid.uuid4()
    sid, uid = uuid.uuid4(), uuid.uuid4()
    async with session_scope() as s:
        s.add(User(id=uid, org_id=uuid.UUID(org), username="pr2-user"))
        s.add(DbSession(id=sid, org_id=uuid.UUID(org), title="pr2"))
        await s.flush()
        # Prod-hardening (2026-08-17): rows younger than HEARTBEAT_TTL count as active even
        # without a heartbeat (burst-admission fix), so a genuinely STALE row must be older
        # than the TTL — which is what a crashed worker's leftover row actually looks like.
        from datetime import UTC, datetime, timedelta
        old = datetime.now(UTC) - timedelta(seconds=120)
        s.add(Run(id=live_id, org_id=uuid.UUID(org), session_id=sid, status="running",
                  mode="apply", initiated_by=uid, created_at=old))
        s.add(Run(id=stale_id, org_id=uuid.UUID(org), session_id=sid, status="running",
                  mode="apply", initiated_by=uid, created_at=old))
    redis = get_redis()
    await redis.set(hb_key(str(live_id)), "1", ex=30)     # only the live one heartbeats
    try:
        org_active, user_active = await _active_run_counts(org, str(uid))
        assert org_active == 1 and user_active == 1       # the stale row self-heals out
    finally:
        await redis.delete(hb_key(str(live_id)))
        async with session_scope() as s:
            await s.execute(delete(Run).where(Run.org_id == uuid.UUID(org)))
            await s.execute(delete(DbSession).where(DbSession.id == sid))
            await s.execute(delete(User).where(User.id == uid))


async def test_awaiting_approval_does_not_count(live_db, live_redis, throwaway_org):
    """awaiting_approval may wait days and holds no worker — it must not consume a slot."""
    from app.agents.supervisor import hb_key
    from app.api.chat import _active_run_counts
    from app.cache.redis import get_redis
    from app.db.models import Run, Session as DbSession
    from app.db.session import session_scope
    from sqlalchemy import delete

    org = throwaway_org
    rid, sid = uuid.uuid4(), uuid.uuid4()
    async with session_scope() as s:
        s.add(DbSession(id=sid, org_id=uuid.UUID(org), title="pr2b"))
        await s.flush()
        s.add(Run(id=rid, org_id=uuid.UUID(org), session_id=sid,
                  status="awaiting_approval", mode="apply"))
    await get_redis().set(hb_key(str(rid)), "1", ex=30)
    try:
        org_active, _ = await _active_run_counts(org, None)
        assert org_active == 0
    finally:
        await get_redis().delete(hb_key(str(rid)))
        async with session_scope() as s:
            await s.execute(delete(Run).where(Run.org_id == uuid.UUID(org)))
            await s.execute(delete(DbSession).where(DbSession.id == sid))
