"""Async Redis client (cache, queues, ephemeral checkpoints, idempotency keys)."""

from __future__ import annotations

import redis.asyncio as aioredis

from ..logging_conf import get_logger
from ..settings import Settings

log = get_logger(__name__)

_client: aioredis.Redis | None = None


def init_redis(settings: Settings) -> aioredis.Redis:
    global _client
    if _client is None:
        _client = aioredis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
            health_check_interval=30,
            socket_connect_timeout=5,
            socket_keepalive=True,
        )
        log.info("redis.initialised")
    return _client


def get_redis() -> aioredis.Redis:
    if _client is None:
        raise RuntimeError("Redis not initialised; call init_redis() at startup.")
    return _client


async def ping() -> bool:
    if _client is None:
        return False
    return bool(await _client.ping())


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
        log.info("redis.closed")
