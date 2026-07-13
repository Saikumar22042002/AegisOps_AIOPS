"""Notification sub-agent — stakeholder updates (email via SMTP when configured; always a
persisted in-app notification). P17: the email goes to the REAL stakeholders — the run's
initiator and (when the run was approved) the approver — never to the sender address."""

from __future__ import annotations

import smtplib
import uuid
from email.message import EmailMessage

import anyio
import structlog

from ..db.models import Notification
from ..db.session import get_sessionmaker
from ..settings import get_settings
from .state import AgentState

log = structlog.get_logger(__name__)


def _recipients(state: AgentState, settings) -> list[str]:
    """P17: initiator + approver, deduped, empty-safe. The configured from-address is the
    LAST-resort fallback only (logged), so a misconfigured run still lands somewhere."""
    out: list[str] = []
    initiator = ((state.get("user") or {}).get("email") or "").strip()
    approver = ((state.get("approver") or {}).get("email") or "").strip()
    for addr in (initiator, approver):
        if addr and "@" in addr and addr not in out:
            out.append(addr)
    if not out:
        log.warning("notify.no_stakeholder_email", run_id=state.get("run_id"))
        if settings.notify_from:
            out.append(settings.notify_from)
    return out


async def notify(state: AgentState, config) -> dict:
    settings = get_settings()
    resolution = state.get("resolution", "")
    title = f"{state.get('domain', 'run').upper()}: {state.get('intent', 'request')} — {state.get('approval_status', 'done')}"

    # Always persist an in-app notification (drives the bell + Notifications module).
    try:
        async with get_sessionmaker()() as session:
            session.add(Notification(org_id=uuid.UUID(state["org_id"]), title=title, body=resolution,
                                     level="info", color="var(--green)"))
            await session.commit()
    except Exception as e:  # noqa: BLE001
        log.warning("notify.persist_failed", error=str(e))

    # Email is best-effort and only when SMTP is configured.
    if settings.smtp_host and settings.smtp_user:
        recipients = _recipients(state, settings)
        if recipients:
            try:
                await anyio.to_thread.run_sync(_send_email, settings, title, resolution, recipients)
            except Exception as e:  # noqa: BLE001
                log.warning("notify.email_failed", error=str(e))
    return {}


def _send_email(settings, subject: str, body: str, recipients: list[str]) -> None:
    msg = EmailMessage()
    msg["From"] = settings.notify_from
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as smtp:
        smtp.starttls()
        if settings.smtp_user:
            smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(msg)
