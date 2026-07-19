"""purge-session admin command — owner-facing on-demand wipe of ONE session's transcript:
its messages go (embeddings are in-row columns; feedback follows by FK cascade); the session
row, runs, audit_log, approvals and inventory stay; the purge itself is audited."""

from __future__ import annotations

import uuid


async def test_purge_deletes_only_target_sessions_messages(live_db, throwaway_org):
    from sqlalchemy import delete, select

    from app.admin import _purge_session
    from app.db.models import AuditLog, Feedback, Message, Run
    from app.db.models import Session as DbSession
    from app.db.session import session_scope

    org = uuid.UUID(throwaway_org)
    target_sid, other_sid, rid, mid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with session_scope() as db:
        db.add(DbSession(id=target_sid, org_id=org, title="purge me", status="active"))
        db.add(DbSession(id=other_sid, org_id=org, title="keep me", status="active"))
        await db.flush()
        db.add(Message(id=mid, org_id=org, session_id=target_sid, role="user", content="secret A"))
        db.add(Message(org_id=org, session_id=target_sid, role="assistant", content="secret B"))
        db.add(Message(org_id=org, session_id=other_sid, role="user", content="unrelated"))
        await db.flush()
        db.add(Feedback(org_id=org, message_id=mid, value="up"))
        db.add(Run(id=rid, org_id=org, session_id=target_sid, status="completed", mode="plan"))
    try:
        assert await _purge_session([str(target_sid)]) == 0
        async with session_scope() as db:
            left = list((await db.execute(
                select(Message).where(Message.org_id == org))).scalars())
            # ONLY the target session's messages are gone; the sibling session's survive.
            assert {m.session_id for m in left} == {other_sid}
            # Feedback on a purged message followed the FK cascade.
            fb = list((await db.execute(
                select(Feedback).where(Feedback.org_id == org))).scalars())
            assert fb == []
            # The session row and the run survive — the governance record is untouched.
            assert await db.get(DbSession, target_sid) is not None
            assert await db.get(Run, rid) is not None
            # The purge itself is audited (insert-only row).
            audits = list((await db.execute(select(AuditLog).where(
                AuditLog.org_id == org,
                AuditLog.action == "session.purge_messages"))).scalars())
            assert len(audits) == 1
            assert audits[0].target == str(target_sid)
            assert audits[0].detail["messages_deleted"] == 2
    finally:
        async with session_scope() as db:
            await db.execute(delete(Message).where(Message.org_id == org))
            await db.execute(delete(Run).where(Run.id == rid))
            await db.execute(delete(DbSession).where(DbSession.org_id == org))
            await db.execute(delete(AuditLog).where(AuditLog.org_id == org))


async def test_purge_is_org_scoped(live_db, throwaway_org):
    """--org names the tenant: a session id that exists but under a DIFFERENT org is refused
    (multi-tenant rule — no cross-org UUID probing deletes anything)."""
    from sqlalchemy import delete, select

    from app.admin import _purge_session
    from app.db.models import AuditLog, Message
    from app.db.models import Session as DbSession
    from app.db.session import session_scope

    org = uuid.UUID(throwaway_org)
    sid = uuid.uuid4()
    async with session_scope() as db:
        db.add(DbSession(id=sid, org_id=org, title="scoped", status="active"))
        await db.flush()
        db.add(Message(org_id=org, session_id=sid, role="user", content="keep until org matches"))
    try:
        assert await _purge_session([str(sid), "--org", str(uuid.uuid4())]) == 1
        async with session_scope() as db:
            kept = list((await db.execute(
                select(Message).where(Message.session_id == sid))).scalars())
            assert len(kept) == 1  # wrong org → nothing deleted
        assert await _purge_session([str(sid), "--org", throwaway_org]) == 0
        async with session_scope() as db:
            gone = list((await db.execute(
                select(Message).where(Message.session_id == sid))).scalars())
            assert gone == []
    finally:
        async with session_scope() as db:
            await db.execute(delete(Message).where(Message.org_id == org))
            await db.execute(delete(DbSession).where(DbSession.org_id == org))
            await db.execute(delete(AuditLog).where(AuditLog.org_id == org))


async def test_purge_usage_and_not_found_exit_codes(live_db):
    from app.admin import _purge_session

    assert await _purge_session([]) == 2                        # no session id
    assert await _purge_session(["not-a-uuid"]) == 2            # malformed id
    assert await _purge_session([str(uuid.uuid4()), "--org", "not-a-uuid"]) == 2
    assert await _purge_session([str(uuid.uuid4())]) == 1       # unknown session


def test_purge_session_registered_in_cli():
    from app.admin import _COMMANDS

    assert "purge-session" in _COMMANDS
