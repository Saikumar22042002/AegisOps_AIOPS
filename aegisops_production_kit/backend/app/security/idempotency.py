"""Idempotency keys (Redis-backed) — prevent duplicate tool execution on retry/resume."""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

from ..cache.redis import get_redis

_PREFIX = "idem:"
_DEFAULT_TTL = 86400


def make_key(*parts: Any) -> str:
    raw = "|".join(str(p) for p in parts)
    return _PREFIX + hashlib.sha256(raw.encode()).hexdigest()[:32]


async def claim(key: str, ttl: int = _DEFAULT_TTL) -> bool:
    """Atomically claim a key. Returns True if newly claimed, False if already in flight/done."""
    redis = get_redis()
    return bool(await redis.set(key, json.dumps({"state": "in_progress"}), nx=True, ex=ttl))


async def get_result(key: str) -> dict[str, Any] | None:
    redis = get_redis()
    raw = await redis.get(key)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if data.get("state") == "done" else None


async def is_in_progress(key: str) -> bool:
    """True iff the key is claimed but has no stored result yet (another run is executing)."""
    redis = get_redis()
    raw = await redis.get(key)
    if not raw:
        return False
    try:
        return json.loads(raw).get("state") == "in_progress"
    except json.JSONDecodeError:
        return False


async def wait_for_result(key: str, *, deadline_s: float = 20.0,
                          interval_s: float = 0.4) -> dict[str, Any] | None:
    """Poll for a completed result up to a deadline (A1's wait side).

    Returns the stored `{"state":"done","result":…}` payload once available, or None if the
    key is still in-progress when the deadline passes (the caller must then ABORT — never
    fall through to a second execution) or the claim disappeared (released after a failure).
    """
    waited = 0.0
    while waited < deadline_s:
        done = await get_result(key)
        if done is not None:
            return done
        if not await is_in_progress(key):
            return None  # claim released (peer failed) or expired — no result will arrive
        await asyncio.sleep(interval_s)
        waited += interval_s
    return None


async def store_result(key: str, result: Any, ttl: int = _DEFAULT_TTL) -> None:
    redis = get_redis()
    await redis.set(key, json.dumps({"state": "done", "result": result}), ex=ttl)


async def release(key: str) -> None:
    """Release a claim (e.g. on failure) so a later retry can re-run."""
    redis = get_redis()
    await redis.delete(key)
