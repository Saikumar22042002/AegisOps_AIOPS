"""Prompt registry (P2.8 — Redesign/05 §9).

`PromptRef(name, version)` resolves to a stored, content-hashed prompt. `register()` is
idempotent by content hash (re-registering identical content is a no-op; changed content
must bump the version — prompts are versioned artifacts, changed only via PR + eval gate,
not string edits). `resolve()` returns the content + a ref stamp for the ledger's
`prompt_version` and Langfuse. This is the FOUNDATION: the kernel still accepts inline
system prompts in P2; PromptRef indirection is opt-in here and adopted broadly in P4.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from sqlalchemy import select

from ..db.models import PromptRegistry
from ..db.session import session_scope


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PromptRef:
    name: str
    version: int
    content: str
    content_hash: str

    @property
    def stamp(self) -> str:
        """The `prompt_version` value recorded on ledger rows / generations."""
        return f"{self.name}@{self.version}"


async def register(name: str, content: str, *, owner: str | None = None,
                   changelog: str | None = None) -> PromptRef:
    """Register a prompt version. Idempotent by content hash: identical content returns
    the existing ref; changed content creates the next version."""
    h = content_hash(content)
    async with session_scope() as s:
        rows = (await s.execute(select(PromptRegistry).where(
            PromptRegistry.name == name).order_by(PromptRegistry.version.desc()))).scalars().all()
        for r in rows:
            if r.content_hash == h:
                return PromptRef(name=name, version=r.version, content=r.content,
                                 content_hash=h)
        version = (rows[0].version + 1) if rows else 1
        row = PromptRegistry(name=name, version=version, content=content, content_hash=h,
                             owner=owner, changelog=changelog, eval_state="pending")
        s.add(row)
        return PromptRef(name=name, version=version, content=content, content_hash=h)


async def resolve(name: str, version: int | None = None) -> PromptRef | None:
    """Latest (or a specific) version. None when unregistered — callers fall back to the
    inline prompt, so the registry is additive, never a hard dependency in P2."""
    async with session_scope() as s:
        q = select(PromptRegistry).where(PromptRegistry.name == name)
        q = (q.where(PromptRegistry.version == version) if version is not None
             else q.order_by(PromptRegistry.version.desc()).limit(1))
        row = (await s.execute(q)).scalars().first()
    if row is None:
        return None
    return PromptRef(name=row.name, version=row.version, content=row.content,
                     content_hash=row.content_hash)
