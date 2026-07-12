"""Conversational memory (Phase 8 / N-03).

Screenshots 16/18: the assistant claimed "this is the beginning of our conversation" / "my
context window is currently blank" because every LLM call received ONLY the current message —
the full transcript sat unused in the `messages` table. This module threads it back in:

  build_transcript(session_id, …)   → the session so far, formatted for a prompt. Short
    threads are included verbatim; long threads get a two-part rendering that always fits the
    char budget: a digest of EVERY older user turn (so early facts survive) + the most recent
    turns in full. Never returns a lie like "no history" — "" simply means a fresh session.
  prior_user_questions(session_id)  → ordered user turns (deterministic recall answers).

Deterministic and DB-backed — no LLM in the memory path itself, so recall can't hallucinate.
"""

from __future__ import annotations

import re
import uuid

import structlog
from sqlalchemy import func, select

from ..db.models import Message
from ..db.session import session_scope

log = structlog.get_logger(__name__)

_ROLE_LABEL = {"user": "User", "assistant": "Assistant", "system": "System"}

# M2: deterministic positional-recall detector. "what was my 20th question?", "the 3rd message",
# "what did I ask first", "what did I say in turn 20". Ordinal words + digits; maps to a
# 1-based turn index.
_ORDINAL_WORDS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6,
                  "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10, "last": -1}
_RECALL_RE = re.compile(
    r"\b(?:my|the)\s+(\d+)(?:st|nd|rd|th)?\s+(question|message|prompt|request|thing)\b"
    r"|\b(?:the\s+)?(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|last)\s+"
    r"(question|message|prompt|request|thing)\b"
    # Noun-first numeric shape: "turn 20", "in turn 20", "message #7" (found at the Phase-2
    # gate: the natural "what did I say in turn 20?" phrasing didn't match either noun-last
    # form). Deliberately ONLY turn/message — "request 3"/"question 5" would false-positive on
    # ordinary sentences ("I request 3 VMs").
    r"|\b(turn|message)\s*#?\s*(\d+)\b",
    re.IGNORECASE,
)


async def load_history(session_id: str, limit: int = 400) -> list[dict]:
    """Chronological session messages: [{role, content}]. Empty on any lookup problem."""
    if not session_id:
        return []
    try:
        sid = uuid.UUID(str(session_id))
    except ValueError:
        return []
    try:
        async with session_scope() as s:
            rows = (await s.execute(
                select(Message.role, Message.content, Message.created_at, Message.id)
                .where(Message.session_id == sid)
                .order_by(Message.created_at.asc(), Message.id.asc())
                .limit(limit)
            )).all()
        return [{"role": r.role, "content": r.content or ""} for r in rows]
    except Exception as e:  # noqa: BLE001 - memory must never break a run
        log.warning("memory.load_failed", session_id=session_id, error=str(e))
        return []


async def prior_user_questions(session_id: str) -> list[str]:
    return [m["content"] for m in await load_history(session_id) if m["role"] == "user"]


# ── M2: positional recall (exact) ───────────────────────────────────────────────────────────


async def get_turn(session_id: str, ordinal: int, role: str | None = "user") -> dict | None:
    """The Nth turn (1-based) verbatim — no truncation, no LLM. `role="user"` counts only user
    turns (so "my 20th question" = the 20th thing the user asked); role=None counts all messages.
    ordinal == -1 returns the most recent matching turn. Returns {role, content, ordinal} or None."""
    history = await load_history(session_id)
    turns = [m for m in history if role is None or m["role"] == role]
    if not turns:
        return None
    idx = len(turns) - 1 if ordinal == -1 else ordinal - 1
    if idx < 0 or idx >= len(turns):
        return None
    return {**turns[idx], "ordinal": (len(turns) if ordinal == -1 else ordinal)}


def detect_recall(message: str) -> tuple[int, str] | None:
    """If the message asks for a specific earlier turn by position, return (ordinal, role);
    else None. Deterministic — this is what makes "what was my 20th question?" exact."""
    m = _RECALL_RE.search(message or "")
    if not m:
        return None
    if m.group(1):  # numeric, noun-last: "my 20th question"
        ordinal, noun = int(m.group(1)), m.group(2)
    elif m.group(3):  # ordinal word: "the first message"
        ordinal, noun = _ORDINAL_WORDS.get(m.group(3).lower(), 0), m.group(4)
    else:  # numeric, noun-first: "turn 20", "message #7"
        ordinal, noun = int(m.group(6)), m.group(5)
    if not ordinal:
        return None
    role = "user" if noun.lower() in ("question", "prompt", "request") else None
    return ordinal, role or "user"


# ── M2: semantic recall (fuzzy) with a keyword fallback ─────────────────────────────────────


async def retrieve(session_id: str, query: str, *, k: int = 3, settings=None) -> list[dict]:
    """Top-k earlier turns relevant to `query` — semantic over message embeddings when available,
    else pg_trgm keyword similarity (mirrors rag/store). Returns [{role, content}]."""
    if not session_id or not query:
        return []
    try:
        sid = uuid.UUID(str(session_id))
    except ValueError:
        return []
    try:
        vec = None
        if settings is not None:
            from ..rag.embeddings import embed_texts
            vecs = await embed_texts(settings, [query])
            vec = vecs[0] if vecs else None
        async with session_scope() as s:
            if vec is not None:
                rows = (await s.execute(
                    select(Message.role, Message.content)
                    .where(Message.session_id == sid, Message.embedding.isnot(None))
                    .order_by(Message.embedding.cosine_distance(vec))
                    .limit(k)
                )).all()
            else:
                sim = func.similarity(Message.content, query)
                rows = (await s.execute(
                    select(Message.role, Message.content)
                    .where(Message.session_id == sid)
                    .order_by(sim.desc())
                    .limit(k)
                )).all()
        return [{"role": r.role, "content": r.content or ""} for r in rows]
    except Exception as e:  # noqa: BLE001 — recall must never break a run
        log.warning("memory.retrieve_failed", session_id=session_id, error=str(e))
        return []


# ── M5: context offloading — large payloads live in the store, referenced (not inlined) ────────


def plan_ref_line(run_id: str, plan_json: dict | None) -> str:
    """A short REFERENCE to a plan that lives in the store — never the full plan JSON inlined into
    an LLM prompt. Agents fetch the full plan on demand via `fetch_plan`."""
    summ = (plan_json or {}).get("summary", {}) if isinstance(plan_json, dict) else {}
    return (f"[plan run {run_id}: +{summ.get('add', 0)} ~{summ.get('change', 0)} "
            f"-{summ.get('destroy', 0)} — full plan available on request]")


async def fetch_plan(run_id: str) -> dict | None:
    """On-demand fetch of a run's stored plan JSON (M5 offloading). Deterministic, store-grounded."""
    try:
        from ..db.models import Run
        async with session_scope() as s:
            run = await s.get(Run, uuid.UUID(str(run_id)))
            return run.plan_json if run else None
    except Exception as e:  # noqa: BLE001
        log.warning("memory.fetch_plan_failed", run_id=run_id, error=str(e))
        return None


async def embed_message(message_id: str, content: str, settings) -> None:
    """Embed one message on write (M2). Best-effort: no Gemini key ⇒ leaves NULL (keyword recall)."""
    if not content or not content.strip():
        return
    try:
        from ..rag.embeddings import embed_texts
        vecs = await embed_texts(settings, [content])
        if not vecs:
            return
        async with session_scope() as s:
            row = await s.get(Message, uuid.UUID(str(message_id)))
            if row is not None:
                row.embedding = vecs[0]
    except Exception as e:  # noqa: BLE001 — embedding write is best-effort
        log.warning("memory.embed_failed", message_id=message_id, error=str(e))


def _fmt(m: dict, clip: int | None = None) -> str:
    text = (m["content"] or "").strip()
    if clip and len(text) > clip:
        text = text[: clip - 1] + "…"
    return f"{_ROLE_LABEL.get(m['role'], m['role'])}: {text}"


async def build_transcript(session_id: str, max_chars: int = 8000,
                           exclude_last_user: str | None = None) -> str:
    """The conversation so far, fitted to `max_chars`.

    `exclude_last_user` drops the trailing user turn when it IS the current message (it's
    persisted before the run starts, and the agent already has it in the prompt).
    """
    history = await load_history(session_id)
    if exclude_last_user and history and history[-1]["role"] == "user" \
            and history[-1]["content"].strip() == exclude_last_user.strip():
        history = history[:-1]
    if not history:
        return ""

    full = "\n".join(_fmt(m) for m in history)
    if len(full) <= max_chars:
        return full

    # Long thread: newest turns verbatim (~70% of budget, from the end), plus a digest of
    # every OLDER user turn so early facts (names, codenames, decisions) stay recallable.
    recent: list[str] = []
    used = 0
    recent_budget = int(max_chars * 0.7)
    split = len(history)
    for i in range(len(history) - 1, -1, -1):
        line = _fmt(history[i], clip=600)
        if used + len(line) + 1 > recent_budget:
            break
        recent.append(line)
        used += len(line) + 1
        split = i
    recent.reverse()

    older_users = [m for m in history[:split] if m["role"] == "user"]
    digest_budget = max_chars - used - 200
    digest_lines: list[str] = []
    dused = 0
    for i, m in enumerate(older_users, 1):
        line = f"{i}. {(m['content'] or '').strip()[:160]}"
        if dused + len(line) + 1 > digest_budget:
            digest_lines.append(f"… and {len(older_users) - i + 1} more earlier questions")
            break
        digest_lines.append(line)
        dused += len(line) + 1

    parts = []
    if digest_lines:
        parts.append("Earlier in this conversation the user asked/said (digest):\n" + "\n".join(digest_lines))
    parts.append("Most recent turns:\n" + "\n".join(recent))
    return "\n\n".join(parts)


def _ordinal_label(n: int) -> str:
    if n == -1:
        return "most recent"
    suffix = "th" if 11 <= (n % 100) <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


# Rough char budget per purpose (chars ≈ 4·tokens). Router needs a small slice; answering agents
# get the full transcript. This replaces the router's fixed last-8-turn window (M3).
_PURPOSE_BUDGET = {"router": 1600, "cloudops": 3000, "devops": 3000, "sre": 3000,
                   "general": 8000, "knowledge": 4000, "loop": 4000}


async def build_context(session_id: str, *, purpose: str = "general", budget_tokens: int | None = None,
                        current_message: str | None = None, settings=None) -> str:
    """M1/M3 — the session context slice for a purpose, threaded into EVERY LLM call.

    Always returns: (a) a RELEVANT-EARLIER-TURNS slot — the exact earlier turn when the current
    message asks for one by position (M2 positional recall, verbatim), plus top-k semantic/keyword
    hits for fuzzy references; and (b) the transcript (recent verbatim + older-user digest / rolling
    summary), fitted to the purpose's budget. Never returns a lie about "no history"."""
    max_chars = (budget_tokens * 4) if budget_tokens else _PURPOSE_BUDGET.get(purpose, 3000)
    slot: list[str] = []
    if current_message:
        rec = detect_recall(current_message)
        if rec:
            ordinal, role = rec
            turn = await get_turn(session_id, ordinal, role=role)
            if turn:
                slot.append(f"[Exact recall] The user's {_ordinal_label(turn['ordinal'])} {role} "
                            f"turn was, verbatim:\n{turn['content']}")
        for hit in await retrieve(session_id, current_message, k=3, settings=settings):
            text = (hit["content"] or "").strip()
            if text and text != (current_message or "").strip():
                slot.append(f"[Related earlier {hit['role']} turn] {text[:600]}")
    base = await build_transcript(session_id, max_chars=max_chars, exclude_last_user=current_message)
    parts: list[str] = []
    if slot:
        parts.append("Relevant earlier turns:\n" + "\n\n".join(slot))
    if base:
        parts.append(base)
    return "\n\n".join(parts)


async def classification_context(session_id: str, max_chars: int = 1500) -> str:
    """Compact recent-turn context for the router — helps resolve "do it again", "the
    previous one", "same but in gcp" against what was actually said."""
    history = await load_history(session_id)
    if not history:
        return ""
    lines: list[str] = []
    used = 0
    for m in reversed(history[-8:]):
        line = _fmt(m, clip=180)
        if used + len(line) + 1 > max_chars:
            break
        lines.append(line)
        used += len(line) + 1
    lines.reverse()
    return "\n".join(lines)
