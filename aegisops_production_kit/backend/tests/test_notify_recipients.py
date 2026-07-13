"""P17 — notify REAL recipients: the run's initiator and (when approved) the approver are
addressed; the configured from-address is only the logged last-resort fallback, never the
default To. The approver's email now rides the approval resume payload (chat.py)."""

from __future__ import annotations

import uuid

from app.agents import notify
from app.settings import get_settings


def test_recipients_initiator_and_approver_deduped():
    s = get_settings()
    state = {"run_id": "r1",
             "user": {"email": "initiator@corp.example"},
             "approver": {"email": "approver@corp.example", "decision": "approve"}}
    assert notify._recipients(state, s) == ["initiator@corp.example", "approver@corp.example"]
    # self-approval dedupes to one
    state["approver"]["email"] = "initiator@corp.example"
    assert notify._recipients(state, s) == ["initiator@corp.example"]


def test_recipients_never_default_to_the_sender_when_a_stakeholder_exists():
    s = get_settings()
    state = {"run_id": "r2", "user": {"email": "initiator@corp.example"}}
    rec = notify._recipients(state, s)
    assert rec == ["initiator@corp.example"]
    assert s.notify_from not in rec or s.notify_from == "initiator@corp.example"


def test_recipients_fallback_only_when_nobody_is_addressable():
    s = get_settings()
    state = {"run_id": "r3", "user": {"email": ""}, "approver": {}}
    rec = notify._recipients(state, s)
    assert rec == ([s.notify_from] if s.notify_from else [])
    # junk addresses are ignored, not sent to
    state = {"run_id": "r4", "user": {"email": "not-an-email"}}
    assert notify._recipients(state, s) == ([s.notify_from] if s.notify_from else [])


def test_send_email_addresses_the_recipients(monkeypatch):
    sent = {}

    class _SMTP:
        def __init__(self, host, port, timeout=15): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self): pass
        def login(self, u, p): pass
        def send_message(self, msg): sent["to"] = msg["To"]; sent["from"] = msg["From"]

    import smtplib
    monkeypatch.setattr(smtplib, "SMTP", _SMTP)
    s = get_settings()
    notify._send_email(s, "subj", "body",
                       ["initiator@corp.example", "approver@corp.example"])
    assert sent["to"] == "initiator@corp.example, approver@corp.example"
    assert sent["from"] == s.notify_from            # From stays the sender; To never does


def test_approval_resume_carries_the_approver_email():
    """chat.py's resume payload is the ONLY way the approver's email reaches the graph —
    pin it at the source so a refactor can't silently drop P17."""
    from pathlib import Path
    src = Path(notify.__file__).resolve().parents[2] / "app" / "api" / "chat.py"
    text = src.read_text(encoding="utf-8")
    assert '"email": user.email' in text and "P17" in text


async def test_notify_persists_and_emails_stakeholders(live_db, throwaway_org, monkeypatch):
    sent = {}

    class _SMTP:
        def __init__(self, host, port, timeout=15): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self): pass
        def login(self, u, p): pass
        def send_message(self, msg): sent["to"] = msg["To"]

    import smtplib
    monkeypatch.setattr(smtplib, "SMTP", _SMTP)
    s = get_settings()
    monkeypatch.setattr(s, "smtp_host", "smtp.test.local", raising=False)
    monkeypatch.setattr(s, "smtp_user", "mailer", raising=False)

    state = {"org_id": throwaway_org, "run_id": str(uuid.uuid4()), "domain": "cloudops",
             "intent": "provision", "approval_status": "approved",
             "resolution": "applied web-01",
             "user": {"email": "initiator@corp.example"},
             "approver": {"email": "approver@corp.example"}}
    try:
        await notify.notify(state, {})
        assert sent["to"] == "initiator@corp.example, approver@corp.example"
        from app.db.models import Notification
        from app.db.session import session_scope
        from sqlalchemy import select
        async with session_scope() as db:
            rows = (await db.execute(select(Notification).where(
                Notification.org_id == uuid.UUID(throwaway_org)))).scalars().all()
            assert any("provision" in r.title for r in rows)
    finally:
        from app.db.models import Notification
        from app.db.session import session_scope
        from sqlalchemy import delete
        async with session_scope() as db:
            await db.execute(delete(Notification).where(
                Notification.org_id == uuid.UUID(throwaway_org)))
