"""Context compaction — a long session's tail is replaced by durable state, not truncated.

Compaction is NOT "keep the last N messages". When the transcript would overflow its budget,
the older half is replaced by a deterministic SESSION STATE block rebuilt from durable
stores (never from an LLM summary, so it cannot hallucinate):

  active resources        this session's rows in `resources` (+ their current state)
  change history          this session's `resource_revisions` lines (run ids preserved)
  decisions               `approvals` rows for this session's runs
  pending action          the params/pending record (unanswered collection or failed-apply
                          continuity — the exact values the user already provided)
  unresolved failures     the last failed run outcome in this session

Everything points back to durable sources (run ids / revision journal), so nothing is lost —
compaction changes what is IN CONTEXT, never what is KNOWN. Emitted as a `compaction`
run_event when run-scoped, so the shrink is observable, never silent.
"""

from __future__ import annotations

import uuid as uuid_mod

import structlog
from sqlalchemy import select

from ..db.session import session_scope

log = structlog.get_logger(__name__)


async def session_state_block(org_id: str, session_id: str | None,
                              *, run_id: str | None = None, max_chars: int = 1600) -> str:
    """The deterministic compacted state for one session ('' when nothing durable exists)."""
    if not session_id:
        return ""
    try:
        sid = uuid_mod.UUID(str(session_id))
        oid = uuid_mod.UUID(str(org_id))
    except ValueError:
        return ""
    from ..db.models import Approval, Resource, ResourceRevision, Run

    lines: list[str] = []
    try:
        async with session_scope() as s:
            resources = list((await s.execute(
                select(Resource).where(Resource.org_id == oid, Resource.session_id == sid)
                .order_by(Resource.created_at.desc()).limit(6))).scalars())
            revisions = list((await s.execute(
                select(ResourceRevision).where(ResourceRevision.org_id == oid,
                                               ResourceRevision.session_id == sid)
                .order_by(ResourceRevision.created_at.desc()).limit(8))).scalars())
            run_ids = [r.run_id for r in revisions if r.run_id]
            approvals = list((await s.execute(
                select(Approval).where(Approval.run_id.in_(run_ids)))).scalars()) if run_ids else []
            failed = list((await s.execute(
                select(Run).where(Run.org_id == oid, Run.session_id == sid,
                                  Run.status.in_(("failed",)))
                .order_by(Run.created_at.desc()).limit(2))).scalars())
    except Exception as e:  # noqa: BLE001 — compaction must never break context assembly
        log.warning("compaction.state_read_failed", error=str(e))
        return ""

    if resources:
        lines.append("Active resources this session:")
        for r in resources:
            attrs = r.attributes or {}
            extra = ", ".join(f"{k}={attrs[k]}" for k in ("instance_id", "vpc_id", "public_ip",
                                                          "ingress_ports") if attrs.get(k))
            lines.append(f"- {r.name} ({r.cloud} {r.resource_type}, {r.status})"
                         + (f" · {extra}" if extra else "")
                         + (f" · id {r.provider_id}" if r.provider_id else ""))
    if revisions:
        lines.append("Changes this session (immutable journal):")
        for rv in revisions:
            when = rv.created_at.strftime("%H:%M UTC") if rv.created_at else "?"
            lines.append(f"- {when} {rv.action} {rv.name} · run {str(rv.run_id)[:8] if rv.run_id else '?'}")
    if approvals:
        lines.append("Decisions:")
        for a in approvals[:4]:
            lines.append(f"- {a.decision} by {a.actor_user} ({a.ts.strftime('%H:%M UTC') if a.ts else '?'})")
    if failed:
        lines.append("Unresolved failures:")
        for f in failed:
            err = ((f.outcome or {}).get("error") or "")[:120]
            lines.append(f"- run {str(f.id)[:8]}: {err}")
    try:
        from ..agents import params
        pending = await params.load_pending(session_id)
        if pending and pending.get("collected"):
            vals = ", ".join(f"{k}={v}" for k, v in list(pending["collected"].items())[:8])
            lines.append(f"Pending {pending.get('template') or 'workflow'} parameters "
                         f"(already provided — do not re-ask): {vals}")
    except Exception:  # noqa: BLE001
        pass

    if not lines:
        return ""
    block = "[SESSION STATE — compacted from durable records; run ids link to the full history]\n" \
            + "\n".join(lines)
    block = block[:max_chars]
    if run_id:
        try:
            from ..harness import run_log
            await run_log.append(run_id, "compaction",
                                 {"stage": "session_state", "chars": len(block),
                                  "resources": len(resources), "revisions": len(revisions)},
                                 org_id=org_id)
        except Exception:  # noqa: BLE001
            pass
    return block
