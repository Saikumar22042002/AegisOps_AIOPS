"""PR-4 RETENTION — configurable data-growth sweeper. Extends the reconciler's periodic
loop (never a parallel scheduler). Everything is OFF by default (retention_days <= 0);
documented prod defaults live in .env.example.

Guarantees:
- messages / run_steps / notifications: delete beyond N days for CLOSED sessions only
  (message embeddings cascade via the messages FK). runs keep the AUDIT row.
- runs: keep the row; compact bulky plan_json beyond N days to just its summary — the
  Traces/Terraform tabs then show an honest "compacted per retention policy" marker.
- audit_log + approvals: NEVER touched here — retention of those is a compliance
  statement (partitioning guidance in the runbook), not an auto-delete.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import delete, func, select, update

from ..db.models import Message, Notification, Run, RunStep, Session as DbSession
from ..db.session import session_scope
from ..settings import get_settings

log = structlog.get_logger(__name__)

COMPACTED_MARKER = "compacted per retention policy"


async def sweep_retention() -> dict[str, int]:
    """One retention pass. Returns per-category counts (observability + tests). A category
    with retention_days <= 0 is skipped entirely (the dev default — nothing is deleted)."""
    s = get_settings()
    out = {"messages_deleted": 0, "run_steps_deleted": 0, "notifications_deleted": 0,
           "runs_compacted": 0}

    msg_days = int(getattr(s, "retention_messages_days", 0) or 0)
    run_days = int(getattr(s, "retention_run_plan_days", 0) or 0)
    notif_days = int(getattr(s, "retention_notifications_days", 0) or 0)
    if msg_days <= 0 and run_days <= 0 and notif_days <= 0:
        return out  # retention disabled (dev default) — a cheap no-op

    now = datetime.now(timezone.utc)
    async with session_scope() as db:
        if msg_days > 0:
            cutoff = now - timedelta(days=msg_days)
            # CLOSED sessions only — an active conversation is never pruned.
            closed = select(DbSession.id).where(DbSession.status == "closed")
            # run_steps hang off runs whose session is closed + old.
            old_runs = select(Run.id).where(Run.session_id.in_(closed), Run.created_at < cutoff)
            rs = await db.execute(delete(RunStep).where(RunStep.run_id.in_(old_runs)))
            out["run_steps_deleted"] = rs.rowcount or 0
            # messages (embeddings cascade with the row).
            mr = await db.execute(delete(Message).where(
                Message.session_id.in_(closed), Message.created_at < cutoff))
            out["messages_deleted"] = mr.rowcount or 0

        if notif_days > 0:
            cutoff = now - timedelta(days=notif_days)
            nr = await db.execute(delete(Notification).where(Notification.created_at < cutoff))
            out["notifications_deleted"] = nr.rowcount or 0

        if run_days > 0:
            cutoff = now - timedelta(days=run_days)
            # Compact bulky plan_json to just its summary; keep the audit row + a marker.
            rows = (await db.execute(select(Run).where(
                Run.created_at < cutoff, Run.plan_json.is_not(None)))).scalars().all()
            for run in rows:
                pj = run.plan_json or {}
                if pj.get("_compacted"):
                    continue
                if "diff" in pj or "policy_checks" in pj:
                    run.plan_json = {"summary": pj.get("summary", {}), "_compacted": True,
                                     "_note": COMPACTED_MARKER}
                    out["runs_compacted"] += 1

    if any(out.values()):
        log.info("retention.swept", **out)
    return out
