"""The canonical retrieval pipeline — gate → plan → multi-source → rank → budget → assembly.

One path, reusing what exists (nothing duplicated):
  gate            harness/memory.py `gate` (P2, dark until now) — LLM retrieve-or-skip with
                  deterministic overrides, fail-open, observable `agent_gate` run_events
  revisions       PostgreSQL `resource_revisions` (immutable audit journal, Prompt 1)
  graphiti        temporal + semantic facts/episodes (intelligence.graphiti_layer)
  messages        session pgvector recall (agents/memory.retrieve — the M-series path)
  accepted facts  memory_items rows a human accepted (harness/memory.accept_proposal)

Documents deliberately stay with the knowledge agent (rag/retriever) — DOCUMENT KNOWLEDGE
is a distinct block owned by that path; this pipeline never mixes it in blindly.

Ranking/budget mechanics adapted from the operator's graphiti-chatbot POC: normalized-key
dedup, blended relevance(rank-prior)+recency(half-life) scoring, hard token budgets with a
`dropped_by_budget` counter, and `[since <date>]` stamps so the model can reason about time.

Deterministic no-retrieval fast-path: bare answers/greetings skip everything without even
the gate call — a simple message must not pay a retrieval tax (test 15).

Every stage reports real execution state in the returned `RetrievalTrace`; a run-scoped call
also lands it as an `observation` run_event. No telemetry is emitted for a source that was
not actually queried.
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid as uuid_mod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import structlog

from ..settings import Settings
from . import graphiti_layer

log = structlog.get_logger(__name__)

_WS = re.compile(r"\s+")
_NORM = re.compile(r"[^a-z0-9 ]+")

# Narrative-recall signals (aligned with the gate's deterministic overrides).
_RECALL = re.compile(r"\b(last time|previously|earlier|before|recall|remember|you said|"
                     r"we discussed|past incident|yesterday|last week)\b", re.IGNORECASE)
_GREETING = re.compile(r"^\s*(hi|hello|hey|thanks|thank you|ok|okay|good (morning|evening|afternoon))\b[\s!,.]*$",
                       re.IGNORECASE)


def _tokens_est(text: str) -> int:
    return max(1, len(text) // 4)


@dataclass
class SourceReport:
    queried: bool = False
    hits: int = 0
    selected: int = 0
    error: str | None = None


@dataclass
class RetrievalTrace:
    """Observable evidence of what the pipeline actually did — never fabricated."""

    query: str = ""
    skipped: bool = False
    skip_reason: str = ""
    gate: dict = field(default_factory=dict)
    entities: list[str] = field(default_factory=list)
    temporal_window: str | None = None
    sources: dict[str, SourceReport] = field(default_factory=dict)
    dropped_by_budget: int = 0
    token_estimate: int = 0
    latency_ms: float = 0.0

    def summary(self) -> dict:
        return {"query": self.query[:120], "skipped": self.skipped,
                "skip_reason": self.skip_reason, "gate": self.gate,
                "entities": self.entities[:6], "temporal_window": self.temporal_window,
                "sources": {k: {"queried": v.queried, "hits": v.hits, "selected": v.selected,
                                **({"error": v.error} if v.error else {})}
                            for k, v in self.sources.items()},
                "dropped_by_budget": self.dropped_by_budget,
                "token_estimate": self.token_estimate,
                "latency_ms": round(self.latency_ms, 1)}


@dataclass
class ContextBundle:
    text: str
    trace: RetrievalTrace


def _dedup_key(text: str) -> str:
    return _WS.sub(" ", _NORM.sub("", text.lower())).strip()


def _temporal_window(message: str) -> tuple[datetime, datetime] | None:
    low = message.lower()
    now = datetime.now(timezone.utc)
    if "yesterday" in low:
        start = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return start, start + timedelta(days=1)
    if "today" in low:
        return now.replace(hour=0, minute=0, second=0, microsecond=0), now
    if "last week" in low or "this week" in low:
        return now - timedelta(days=7), now
    return None


async def _known_entities(org_id: str, message: str) -> list[str]:
    """Deterministic entity resolution against the org's inventory names — never semantic
    similarity alone. Whole-token match of recorded names in the message."""
    from ..agents import inventory
    low = message.lower()
    names: list[str] = []
    try:
        for row in await inventory.list_active(org_id, statuses=("active", "partial")):
            n = str(row.get("name") or "")
            if n and re.search(rf"(?<![\w-]){re.escape(n.lower())}(?![\w-])", low):
                names.append(n)
    except Exception as e:  # noqa: BLE001
        log.warning("intelligence.entity_scan_failed", error=str(e))
    return names[:6]


async def _fetch_revisions(org_id: str, *, names: list[str],
                           window: tuple[datetime, datetime] | None, limit: int = 6) -> list[str]:
    """Compact immutable-journal lines (CHANGE HISTORY block) — PG is the audit truth."""
    from sqlalchemy import select

    from ..db.models import ResourceRevision
    from ..db.session import session_scope
    async with session_scope() as s:
        q = select(ResourceRevision).where(ResourceRevision.org_id == uuid_mod.UUID(org_id))
        if names:
            q = q.where(ResourceRevision.name.in_(names))
        if window:
            q = q.where(ResourceRevision.created_at >= window[0],
                        ResourceRevision.created_at <= window[1])
        rows = list((await s.execute(
            q.order_by(ResourceRevision.created_at.desc()).limit(limit))).scalars())
    out = []
    for r in rows:
        when = r.created_at.strftime("%Y-%m-%d %H:%M UTC") if r.created_at else "?"
        run = str(r.run_id)[:8] if r.run_id else "?"
        out.append(f"{when} — {r.actor_user or '?'} {r.action} {r.name} "
                   f"({r.cloud} {r.resource_type}) · run {run}")
    return out


async def _fetch_accepted_facts(org_id: str, limit: int = 4) -> list[str]:
    """Human-accepted memory_items (the ONLY agent-writable-with-approval store, P2 §2)."""
    from sqlalchemy import select

    from ..db.models import MemoryItem
    from ..db.session import session_scope
    try:
        async with session_scope() as s:
            rows = list((await s.execute(
                select(MemoryItem).where(MemoryItem.org_id == uuid_mod.UUID(org_id),
                                         MemoryItem.status == "active")
                .order_by(MemoryItem.created_at.desc()).limit(limit))).scalars())
        return [r.content for r in rows if r.content]
    except Exception as e:  # noqa: BLE001
        log.warning("intelligence.memory_items_failed", error=str(e))
        return []


def _stamp(item: dict) -> str:
    ref = item.get("valid_at")
    if not ref:
        return ""
    suffix = "" if item.get("current", True) else " — SUPERSEDED"
    return f" [since {ref.date().isoformat()}{suffix}]"


async def assemble(settings: Settings, *, message: str, org_id: str,
                   session_id: str | None = None, run_id: str | None = None,
                   purpose: str = "general", max_chars: int = 2400) -> ContextBundle:
    """Run the pipeline for one message. Returns typed, budgeted, provenance-labeled context
    plus the observable trace. Empty text = honestly nothing relevant (never padding)."""
    t0 = time.perf_counter()
    trace = RetrievalTrace(query=message[:300])

    # Deterministic no-retrieval fast-path — no gate call, no sources, no cost.
    from ..agents import intent_guard
    if _GREETING.match(message or "") or intent_guard.message_shape(message) == "answer":
        trace.skipped, trace.skip_reason = True, "deterministic: greeting/parameter answer"
        trace.latency_ms = (time.perf_counter() - t0) * 1000
        log.info("intelligence.retrieved", **trace.summary())
        return ContextBundle("", trace)

    # Retrieval gate — the P2 seam, now on the live path. Fails open by design.
    from ..harness import memory as harness_memory
    decision = await harness_memory.gate(settings, message, run_id=run_id, org_id=org_id)
    trace.gate = {"retrieve": decision.retrieve, "forced": decision.forced,
                  "reason": decision.reason[:160]}
    if not decision.retrieve:
        trace.skipped, trace.skip_reason = True, f"gate: {decision.reason[:120]}"
        trace.latency_ms = (time.perf_counter() - t0) * 1000
        log.info("intelligence.retrieved", **trace.summary())
        return ContextBundle("", trace)

    # Plan: which sources this question actually needs.
    entities = await _known_entities(org_id, message)
    window = _temporal_window(message)
    trace.entities = entities
    trace.temporal_window = (f"{window[0].date()}..{window[1].date()}" if window else None)
    is_history = intent_guard.is_history_question(message) or intent_guard.is_provenance_question(message)
    is_recall = bool(_RECALL.search(message))
    infra_relevant = bool(entities) or is_history or window is not None

    plan: dict[str, bool] = {
        "revisions": infra_relevant,
        "graphiti_facts": infra_relevant or is_recall,
        "graphiti_episodes": is_recall,
        "messages": bool(session_id),
        "accepted_facts": infra_relevant or is_recall,
    }

    async def _leg(name: str, coro):
        rep = trace.sources.setdefault(name, SourceReport())
        rep.queried = True
        try:
            return await coro
        except Exception as e:  # noqa: BLE001 — one failing leg never sinks the turn
            rep.error = str(e)[:120]
            log.warning("intelligence.source_failed", source=name, error=str(e))
            return []

    tasks: dict[str, asyncio.Task] = {}
    if plan["revisions"]:
        tasks["revisions"] = asyncio.ensure_future(
            _leg("revisions", _fetch_revisions(org_id, names=entities, window=window)))
    if plan["graphiti_facts"]:
        tasks["graphiti_facts"] = asyncio.ensure_future(
            _leg("graphiti_facts", graphiti_layer.search_facts(
                org_id, decision.query or message, num_results=8,
                include_invalidated=is_history, settings=settings)))
    if plan["graphiti_episodes"]:
        tasks["graphiti_episodes"] = asyncio.ensure_future(
            _leg("graphiti_episodes", graphiti_layer.recent_episodes(org_id, settings=settings)))
    if plan["messages"]:
        from ..agents import memory as conv_memory
        tasks["messages"] = asyncio.ensure_future(
            _leg("messages", conv_memory.retrieve(session_id, decision.query or message,
                                                  k=3, settings=settings)))
    if plan["accepted_facts"]:
        tasks["accepted_facts"] = asyncio.ensure_future(
            _leg("accepted_facts", _fetch_accepted_facts(org_id)))
    results = {k: await v for k, v in tasks.items()}

    # Assemble typed blocks under the budget, dedup across sources, precedence explicit.
    budget = max_chars
    used = 0
    seen: set[str] = set()
    blocks: list[str] = []

    def _take(lines: list[str], header: str, cap: int, source: str) -> None:
        nonlocal used
        if source not in trace.sources:
            return  # never fabricate telemetry for a source that was not queried
        rep = trace.sources[source]
        rep.hits = len(lines)
        kept: list[str] = []
        for line in lines:
            key = _dedup_key(line)
            if not key or key in seen:
                continue
            cost = len(line) + 3
            if used + cost > budget or (sum(len(x) for x in kept) + cost) > cap:
                trace.dropped_by_budget += 1
                continue
            seen.add(key)
            kept.append(line)
            used += cost
        rep.selected = len(kept)
        if kept:
            blocks.append(header + "\n" + "\n".join(f"- {x}" for x in kept))

    if results.get("revisions"):
        _take(results["revisions"],
              "[CHANGE HISTORY — immutable audit journal (authoritative for past changes)]",
              900, "revisions")
    # Cloud isolation (test 8): a question that names clouds must never surface another
    # cloud's facts — deterministic facts carry their cloud in attributes.
    named_clouds = {c for c, pat in (("aws", r"\baws\b|\bamazon\b|\bec2\b|\bs3\b"),
                                     ("azure", r"\bazure\b|\bvnet\b"),
                                     ("gcp", r"\bgcp\b|\bgoogle cloud\b|\bgce\b"))
                    if re.search(pat, message, re.IGNORECASE)}
    gf_seen: set[str] = set()
    gf = [f for f in (results.get("graphiti_facts") or [])
          if (not named_clouds or not f.get("attributes", {}).get("cloud")
              or f["attributes"]["cloud"] in named_clouds)
          and not (_dedup_key(f["fact"]) in gf_seen or gf_seen.add(_dedup_key(f["fact"])))]
    _take([f"{f['fact']}{_stamp(f)}" for f in gf],
          "[INFRASTRUCTURE MEMORY — derived temporal facts; live cloud is authoritative "
          "for CURRENT state]", 1100, "graphiti_facts")
    _take(results.get("accepted_facts") or [],
          "[ACCEPTED OPERATIONAL FACTS — human-approved standing knowledge]", 400,
          "accepted_facts")
    msgs = results.get("messages") or []
    _take([f"{m['role']}: {_WS.sub(' ', (m['content'] or ''))[:240]}" for m in msgs],
          "[CONVERSATIONAL MEMORY — related earlier turns]", 700, "messages")
    _take(results.get("graphiti_episodes") or [],
          "[PAST SESSIONS — consolidated episodes]", 400, "graphiti_episodes")

    text = "\n\n".join(blocks)
    trace.token_estimate = _tokens_est(text) if text else 0
    trace.latency_ms = (time.perf_counter() - t0) * 1000

    if run_id:
        try:
            from ..harness import run_log
            await run_log.append(run_id, "observation",
                                 {"stage": "retrieval", **trace.summary()}, org_id=org_id)
        except Exception:  # noqa: BLE001 — observability never blocks
            pass
    log.info("intelligence.retrieved", **trace.summary())
    return ContextBundle(text, trace)
