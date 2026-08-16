"""Memory lifecycle: retrieval gate + consolidation-to-proposals (P2.3/P2.6).

Two governed seams from Redesign/06:

- **Retrieval gate (§4):** before spending a retrieval, one cheapest-tier decision says
  retrieve-or-skip (+ optional query rewrite). It FAILS OPEN (a gate error ⇒ retrieve),
  honors deterministic always-retrieve overrides, and emits an observable `agent_gate`
  event so skip-rate is measurable (the C-05 event kind).

- **Consolidation (§4):** after a run, a cheapest-tier pass proposes candidate facts +
  one episode with provenance + evidence refs. It produces PROPOSALS only — no agent ever
  writes semantic/episodic memory directly (§2 write-path security); a human accepts, and
  acceptance SUPERSEDES a contradicted item rather than letting both coexist.

Both build on the P1 model layer (`retrieval_gate` / `consolidation` purposes) and never
block a reply: any failure degrades to the safe default and is logged.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog

from ..llm import service
from ..llm.errors import ModelError
from ..settings import Settings
from . import run_log

log = structlog.get_logger(__name__)

_GATE_SCHEMA = {
    "type": "object",
    "properties": {
        "retrieve": {"type": "boolean"},
        "query": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["retrieve"],
}

# Deterministic always-retrieve overrides (06 §4): recall-shaped asks never get gated out.
_ALWAYS_RETRIEVE = ("last time", "previously", "earlier", "before", "recall", "remember",
                    "you said", "we discussed", "past incident")


@dataclass
class GateDecision:
    retrieve: bool
    query: str
    reason: str
    forced: bool = False


async def gate(settings: Settings, message: str, *, run_id: str | None = None,
               org_id: str | None = None) -> GateDecision:
    """Decide retrieve-or-skip. Fails open; deterministic overrides win; observable."""
    low = message.lower()
    if any(k in low for k in _ALWAYS_RETRIEVE):
        d = GateDecision(retrieve=True, query=message, reason="deterministic override",
                         forced=True)
        await _emit_gate(run_id, org_id, d, skipped=False)
        return d
    try:
        raw = await service.classify_json(
            settings,
            "You gate memory retrieval. Retrieve only when recalling prior context would "
            "materially help answer THIS message. Respond with the schema.",
            message, purpose="retrieval_gate", response_schema=_GATE_SCHEMA, org_id=org_id)
        d = GateDecision(retrieve=bool(raw.get("retrieve")),
                         query=str(raw.get("query") or message),
                         reason=str(raw.get("reason", "")))
    except (ModelError, Exception) as e:  # noqa: BLE001 — FAIL OPEN (06 §4)
        log.warning("memory.gate_failed_open", error=str(e))
        d = GateDecision(retrieve=True, query=message, reason="gate error → fail open",
                         forced=True)
    await _emit_gate(run_id, org_id, d, skipped=not d.retrieve)
    return d


async def _emit_gate(run_id, org_id, d: GateDecision, *, skipped: bool) -> None:
    if not run_id:
        return
    try:
        await run_log.append(run_id, "agent_gate",
                             {"retrieve": d.retrieve, "skipped": skipped,
                              "forced": d.forced, "reason": d.reason}, org_id=org_id)
    except Exception:  # noqa: BLE001 — observability never blocks
        pass


# ── consolidation → proposals ────────────────────────────────────────────────────────────────

_CONSOLIDATE_SCHEMA = {
    "type": "object",
    "properties": {
        "facts": {"type": "array", "items": {
            "type": "object",
            "properties": {"subject": {"type": "string"}, "content": {"type": "string"}},
            "required": ["content"]}},
        "episode": {"type": "string"},
    },
    "required": ["facts"],
}


@dataclass
class MemoryProposal:
    kind: str                 # fact | episode
    subject: str | None
    content: str
    origin_run_id: str | None
    confidence: float = 0.7


async def consolidate(settings: Settings, *, run_id: str, transcript: str,
                      org_id: str | None = None) -> list[MemoryProposal]:
    """Cheapest-tier pass → candidate facts + one episode, as PROPOSALS (never written
    directly). Returns [] on any failure — consolidation must never fail a run (06 §2)."""
    try:
        raw = await service.classify_json(
            settings,
            "Extract durable, reusable operational facts from this run (stable truths an "
            "operator would want remembered), plus one one-sentence episode summary. Omit "
            "ephemeral detail. Respond with the schema.",
            transcript[:12000], purpose="consolidation",
            response_schema=_CONSOLIDATE_SCHEMA, org_id=org_id)
    except (ModelError, Exception) as e:  # noqa: BLE001
        log.warning("memory.consolidation_failed", run_id=run_id, error=str(e))
        return []
    proposals: list[MemoryProposal] = []
    for f in (raw.get("facts") or [])[:8]:
        content = str(f.get("content", "")).strip()
        if content:
            proposals.append(MemoryProposal(kind="fact", subject=f.get("subject"),
                                            content=content, origin_run_id=run_id))
    episode = str(raw.get("episode", "")).strip()
    if episode:
        proposals.append(MemoryProposal(kind="episode", subject=None, content=episode,
                                        origin_run_id=run_id))
    return proposals


async def accept_proposal(settings: Settings, proposal: MemoryProposal, *, org_id: str,
                          accepted_by: str, supersedes: int | None = None) -> int:
    """Human acceptance is the ONLY write path into memory_items (06 §2). Contradiction ⇒
    supersede (the old item goes `superseded`), never coexist. Returns the new row id."""
    from sqlalchemy import update

    from ..db.models import MemoryItem
    from ..db.session import session_scope
    async with session_scope() as s:
        if supersedes is not None:
            await s.execute(update(MemoryItem).where(MemoryItem.id == supersedes)
                            .values(status="superseded"))
        row = MemoryItem(
            org_id=uuid.UUID(org_id), kind=proposal.kind, subject=proposal.subject,
            content=proposal.content, provenance="consolidation_accepted",
            origin_run_id=uuid.UUID(proposal.origin_run_id) if proposal.origin_run_id else None,
            confidence=proposal.confidence, status="active", supersedes=supersedes,
            created_by=accepted_by, created_at=datetime.now(UTC))
        s.add(row)
        await s.flush()
        return int(row.id)
