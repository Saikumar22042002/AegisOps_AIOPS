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

import uuid

import structlog
from sqlalchemy import select

from ..db.models import Message
from ..db.session import session_scope

log = structlog.get_logger(__name__)

_ROLE_LABEL = {"user": "User", "assistant": "Assistant", "system": "System"}


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
