"""Per-user/org persistent memory (M4) — user-editable standing context.

Facts the user sets once ("usual_region: ap-south-1", "environment: we're a fintech, always
encrypt") that survive sessions. Org-scoped under S0; a NULL user_id row is org-wide standing
context visible to every member. Threaded into EVERY LLM call as the leading block of
`build_context`, and honored deterministically where it matters even without an LLM (a request
that says "my usual region" resolves the region from this store).
"""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy import delete, or_, select

from ..db.models import UserMemory
from ..db.session import session_scope

log = structlog.get_logger(__name__)

_MAX_BLOCK_CHARS = 600  # standing context stays a small, cheap slice of every prompt


def _uuid(value: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value else None
    except ValueError:
        return None


async def set_memory(org_id: str, user_id: str | None, key: str, content: str) -> None:
    """Upsert one standing fact. `user_id=None` sets it org-wide."""
    key = (key or "").strip()
    content = (content or "").strip()
    if not key or not content:
        raise ValueError("memory key and content are required")
    oid, uid = uuid.UUID(org_id), _uuid(user_id)
    async with session_scope() as s:
        row = (await s.execute(select(UserMemory).where(
            UserMemory.org_id == oid, UserMemory.user_id == uid, UserMemory.key == key,
        ))).scalar_one_or_none()
        if row is None:
            s.add(UserMemory(org_id=oid, user_id=uid, key=key, content=content))
        else:
            row.content = content
    log.info("user_memory.set", org_id=org_id, user=bool(uid), key=key)


async def delete_memory(org_id: str, user_id: str | None, key: str) -> bool:
    oid, uid = uuid.UUID(org_id), _uuid(user_id)
    async with session_scope() as s:
        res = await s.execute(delete(UserMemory).where(
            UserMemory.org_id == oid, UserMemory.user_id == uid, UserMemory.key == key))
        return bool(res.rowcount)


async def list_memories(org_id: str, user_id: str | None) -> list[dict]:
    """The user's standing context: their own rows + the org-wide rows."""
    oid, uid = uuid.UUID(org_id), _uuid(user_id)
    async with session_scope() as s:
        cond = (UserMemory.user_id == uid) if uid else UserMemory.user_id.is_(None)
        rows = (await s.execute(select(UserMemory).where(
            UserMemory.org_id == oid, or_(cond, UserMemory.user_id.is_(None)),
        ).order_by(UserMemory.user_id.isnot(None), UserMemory.key))).scalars().all()
        return [{"key": r.key, "content": r.content,
                 "scope": "user" if r.user_id else "org"} for r in rows]


async def lookup(org_id: str, user_id: str | None, key: str) -> str | None:
    """One fact, user row winning over an org-wide row of the same key."""
    for m in await list_memories(org_id, user_id):
        if m["key"] == key and m["scope"] == "user":
            return m["content"]
    for m in await list_memories(org_id, user_id):
        if m["key"] == key:
            return m["content"]
    return None


async def render_block(org_id: str | None, user_id: str | None) -> str:
    """The standing-context block prepended to build_context (bounded, never a lie: empty
    string when nothing is set or the store is unreachable)."""
    if not org_id:
        return ""
    try:
        memories = await list_memories(org_id, user_id)
    except Exception as e:  # noqa: BLE001 — standing context is additive, never blocking
        log.warning("user_memory.render_failed", error=str(e))
        return ""
    if not memories:
        return ""
    lines = [f"- {m['key']}: {m['content']}" + (" (org-wide)" if m["scope"] == "org" else "")
             for m in memories]
    block = "Standing user memory (user-set; honor unless overridden):\n" + "\n".join(lines)
    return block[:_MAX_BLOCK_CHARS]
