"""Durable run log: append + replay (P2.5 — Redesign/06 §8.2, ADR-16).

`append()` assigns a gapless per-run `seq` (the 10 §0 invariant), redacts the payload
BEFORE the write, persists to `run_events`, and mirrors to the Redis live feed when a bus
is configured. `replay()` reconstructs the ordered event list for harness loop resumption
— the checkpointer stays the graph-spine resume authority (ADR-16: two records, one
owner each). The kernel calls these; nothing here knows what a "loop" is.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import func, select

from ..db.models import RunEvent
from ..db.session import session_scope
from ..security.redaction import redact_dict

log = structlog.get_logger(__name__)

# The 18 kinds (06 §8.2 + agent_gate, C-05). Kept here as the writer's guard so a typo
# is a loud ValueError, not a silent CHECK-constraint 500 at commit.
KINDS: frozenset[str] = frozenset({
    "iteration_started", "assistant_turn", "tool_call", "observation", "policy_verdict",
    "approval_requested", "approval_resolved", "step_started", "step_finished",
    "deviation", "verification", "compaction", "steering", "budget",
    "subagent_spawned", "subagent_result", "run_finished", "agent_gate",
})

_REDIS_MAXLEN = 2000
_REDIS_TTL_S = 3600


@dataclass(frozen=True)
class LoggedEvent:
    seq: int
    kind: str
    payload: dict[str, Any]
    at: datetime


def _redis():
    try:
        from ..cache.redis import get_redis
        return get_redis()
    except Exception:  # noqa: BLE001 — memory-bus / unit tier
        return None


async def append(run_id: str, kind: str, payload: dict[str, Any], *,
                 org_id: str | None = None) -> int:
    """Append one event; returns its seq. Redaction-clean and gapless by construction.
    Best-effort mirror to Redis; the DB row is the durable record."""
    if kind not in KINDS:
        raise ValueError(f"unknown run-event kind {kind!r}")
    clean = redact_dict(payload or {})
    rid = uuid.UUID(run_id)
    async with session_scope() as s:
        nxt = (await s.execute(
            select(func.coalesce(func.max(RunEvent.seq), -1) + 1).where(
                RunEvent.run_id == rid))).scalar_one()
        row = RunEvent(run_id=rid, org_id=uuid.UUID(org_id) if org_id else None,
                       seq=int(nxt), kind=kind, payload=clean, at=datetime.now(UTC))
        s.add(row)
    r = _redis()
    if r is not None:
        try:
            await r.xadd(f"run:{run_id}:log",
                         {"seq": str(nxt), "kind": kind, "data": json.dumps(clean)},
                         maxlen=_REDIS_MAXLEN, approximate=True)
            await r.expire(f"run:{run_id}:log", _REDIS_TTL_S)
        except Exception as exc:  # noqa: BLE001 — live feed is not the record
            log.warning("run_log.redis_mirror_failed", run_id=run_id, error=str(exc))
    return int(nxt)


async def replay(run_id: str) -> list[LoggedEvent]:
    """Ordered event list for this run — the harness resume source (ADR-16)."""
    rid = uuid.UUID(run_id)
    async with session_scope() as s:
        rows = (await s.execute(
            select(RunEvent).where(RunEvent.run_id == rid)
            .order_by(RunEvent.seq))).scalars().all()
    return [LoggedEvent(seq=r.seq, kind=r.kind, payload=r.payload, at=r.at) for r in rows]


async def last_seq(run_id: str) -> int:
    async with session_scope() as s:
        return int((await s.execute(
            select(func.coalesce(func.max(RunEvent.seq), -1)).where(
                RunEvent.run_id == uuid.UUID(run_id)))).scalar_one())
