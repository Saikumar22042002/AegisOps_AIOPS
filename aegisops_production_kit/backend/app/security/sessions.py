"""Redis-backed server-side sessions.

A session holds the user profile + Keycloak tokens. The browser only ever sees an
opaque httpOnly session id cookie; tokens never leave the server. Sessions survive API
restarts (state lives in Redis), keeping the API stateless and horizontally scalable.
"""

from __future__ import annotations

import json
import secrets
from typing import Any

from ..cache.redis import get_redis

SESSION_PREFIX = "sess:"
OAUTH_STATE_PREFIX = "oauth_state:"
SESSION_TTL = 36000  # seconds — matches Keycloak ssoSessionMaxLifespan
OAUTH_STATE_TTL = 600


async def create_session(data: dict[str, Any]) -> str:
    sid = secrets.token_urlsafe(32)
    redis = get_redis()
    await redis.set(f"{SESSION_PREFIX}{sid}", json.dumps(data), ex=SESSION_TTL)
    return sid


async def get_session(sid: str) -> dict[str, Any] | None:
    redis = get_redis()
    raw = await redis.get(f"{SESSION_PREFIX}{sid}")
    return json.loads(raw) if raw else None


async def update_session(sid: str, data: dict[str, Any]) -> None:
    redis = get_redis()
    ttl = await redis.ttl(f"{SESSION_PREFIX}{sid}")
    ex = ttl if ttl and ttl > 0 else SESSION_TTL
    await redis.set(f"{SESSION_PREFIX}{sid}", json.dumps(data), ex=ex)


async def delete_session(sid: str) -> None:
    redis = get_redis()
    await redis.delete(f"{SESSION_PREFIX}{sid}")


async def set_oauth_state(state: str, code_verifier: str) -> None:
    redis = get_redis()
    await redis.set(f"{OAUTH_STATE_PREFIX}{state}", code_verifier, ex=OAUTH_STATE_TTL)


async def pop_oauth_state(state: str) -> str | None:
    redis = get_redis()
    key = f"{OAUTH_STATE_PREFIX}{state}"
    verifier = await redis.get(key)
    if verifier:
        await redis.delete(key)
    return verifier
