"""PR-4 RETENTION — configurable sweeper: OFF by default; deletes msgs/run_steps/
notifications beyond N days for CLOSED sessions only; compacts old run plan_json to its
summary (keeps the audit row + an honest marker); audit_log + approvals NEVER touched."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest


async def _backdate(model, ident, days):
    from sqlalchemy import update
    from app.db.session import session_scope
    async with session_scope() as s:
        await s.execute(update(model).where(model.id == ident).values(
            created_at=datetime.now(timezone.utc) - timedelta(days=days)))


async def test_retention_off_by_default_is_a_noop(live_db, throwaway_org, monkeypatch):
    from app.agents.retention import sweep_retention
    from app.settings import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "retention_messages_days", 0, raising=False)
    monkeypatch.setattr(s, "retention_notifications_days", 0, raising=False)
    monkeypatch.setattr(s, "retention_run_plan_days", 0, raising=False)
    out = await sweep_retention()
    assert out == {"messages_deleted": 0, "run_steps_deleted": 0,
                   "notifications_deleted": 0, "runs_compacted": 0}


async def test_messages_pruned_only_for_closed_sessions(live_db, throwaway_org, monkeypatch):
    from sqlalchemy import delete, select
    from app.agents.retention import sweep_retention
    from app.db.models import Message, Session as DbSession
    from app.db.session import session_scope
    from app.settings import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "retention_messages_days", 180, raising=False)
    org = throwaway_org
    closed_sid, active_sid = uuid.uuid4(), uuid.uuid4()
    old_closed, old_active = uuid.uuid4(), uuid.uuid4()
    async with session_scope() as db:
        db.add(DbSession(id=closed_sid, org_id=uuid.UUID(org), title="closed", status="closed"))
        db.add(DbSession(id=active_sid, org_id=uuid.UUID(org), title="active", status="active"))
        await db.flush()
        db.add(Message(id=old_closed, org_id=uuid.UUID(org), session_id=closed_sid,
                       role="user", content="old in closed"))
        db.add(Message(id=old_active, org_id=uuid.UUID(org), session_id=active_sid,
                       role="user", content="old in active"))
    await _backdate(Message, old_closed, 200)
    await _backdate(Message, old_active, 200)
    try:
        out = await sweep_retention()
        assert out["messages_deleted"] >= 1
        async with session_scope() as db:
            ids = {m.id for m in (await db.execute(select(Message).where(
                Message.org_id == uuid.UUID(org)))).scalars()}
        assert old_closed not in ids          # closed + old → pruned
        assert old_active in ids              # active session → NEVER pruned
    finally:
        async with session_scope() as db:
            await db.execute(delete(Message).where(Message.org_id == uuid.UUID(org)))
            await db.execute(delete(DbSession).where(DbSession.org_id == uuid.UUID(org)))


async def test_run_plan_compacted_but_row_and_audit_kept(live_db, throwaway_org, monkeypatch):
    from sqlalchemy import delete, select
    from app.agents.retention import COMPACTED_MARKER, sweep_retention
    from app.db.models import Run, Session as DbSession
    from app.db.session import session_scope
    from app.settings import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "retention_run_plan_days", 180, raising=False)
    org = throwaway_org
    sid, rid = uuid.uuid4(), uuid.uuid4()
    async with session_scope() as db:
        db.add(DbSession(id=sid, org_id=uuid.UUID(org), title="r", status="closed"))
        await db.flush()
        db.add(Run(id=rid, org_id=uuid.UUID(org), session_id=sid, status="completed",
                   mode="apply",
                   plan_json={"summary": {"add": 1, "change": 0, "destroy": 0},
                              "diff": [{"address": "aws_instance.x"}] * 50,
                              "policy_checks": [{"name": "c", "passed": True}]}))
    await _backdate(Run, rid, 200)
    try:
        out = await sweep_retention()
        assert out["runs_compacted"] >= 1
        async with session_scope() as db:
            run = await db.get(Run, rid)
            assert run is not None                       # the AUDIT row survives
            assert run.plan_json.get("_compacted") is True
            assert run.plan_json.get("_note") == COMPACTED_MARKER
            assert "diff" not in run.plan_json           # bulky payload gone
            assert run.plan_json["summary"]["add"] == 1  # summary kept
        # idempotent: a second sweep does not re-count an already-compacted run
        out2 = await sweep_retention()
        assert out2["runs_compacted"] == 0
    finally:
        async with session_scope() as db:
            await db.execute(delete(Run).where(Run.id == rid))
            await db.execute(delete(DbSession).where(DbSession.id == sid))


async def test_audit_log_and_approvals_are_never_touched(live_db, throwaway_org, monkeypatch):
    """PR-4 binding: retention is a compliance statement for audit/approvals — the sweeper
    must not even reference those tables. Assert its source names neither for deletion."""
    from pathlib import Path
    from app.agents import retention
    src = Path(retention.__file__).read_text(encoding="utf-8")
    # The sweeper must not import or reference the audit/approval models at all — retention
    # of those is a compliance statement, not an auto-delete.
    assert "AuditLog" not in src and "Approval" not in src
