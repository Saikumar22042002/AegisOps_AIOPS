"""Resilient execution over a RoutePlan (P1.6 — Redesign/04 §4.6, 07 P1.6).

What lives here — and ONLY here, so every wire family gets it identically:
- bounded retry with exponential backoff + jitter, honoring provider Retry-After;
- a Redis-backed circuit breaker per (provider, model) binding — availability
  state, never a record (ADR-03); in-memory fallback when Redis is absent;
- two-stage failover: same-binding retries first, then the next hop in the plan's
  validated fallback chain — turn-local and VISIBLE (`ServedBy.fallback_hop`,
  `aegisops_llm_failover_total`); governed purposes have no hops by construction,
  so they can never silently fall back;
- the org daily budget gate (checked before dispatch; refusal is loud);
- `context_overflow` never retries and never fails over (06 §7 — that is a
  compact-and-retry signal for the P2 context engine, not a routing problem).

This is selection + resilience ONLY. No reasoning, no alternative-approach retries,
no tool loops — P1 routing is deterministic substrate for the P2 harness.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import structlog

from ..metrics import LLM_BUDGET_REFUSALS, LLM_FAILOVER
from ..settings import Settings
from . import catalog as catalog_mod
from .adapters import for_provider
from .errors import ModelError
from .types import ModelRequest, ModelResponse, StreamEvent

log = structlog.get_logger(__name__)

_MAX_ATTEMPTS_PER_HOP = 3
_BACKOFF_BASE_S = 0.5
_BACKOFF_MAX_S = 6.0
_BREAKER_WINDOW_S = 60
_BREAKER_THRESHOLD = 5
_BREAKER_OPEN_S = 30


class _Breaker:
    """Failure-rate breaker per (provider, model). Redis-shared across workers when
    available (key prefix `llm:breaker:`); process-local dict otherwise. Breaker
    state is availability data — losing it is safe (ADR-03)."""

    def __init__(self) -> None:
        self._local_fail: dict[str, list[float]] = {}
        self._local_open: dict[str, float] = {}

    @staticmethod
    def _redis():
        try:
            from ..cache.redis import get_redis
            return get_redis()
        except Exception:  # noqa: BLE001 — unit tests / local without Redis
            return None

    async def allow(self, key: str) -> bool:
        r = self._redis()
        if r is not None:
            try:
                return not bool(await r.exists(f"llm:breaker:open:{key}"))
            except Exception:  # noqa: BLE001
                pass
        return self._local_open.get(key, 0.0) < time.monotonic()

    async def record_failure(self, key: str) -> None:
        r = self._redis()
        if r is not None:
            try:
                count_key = f"llm:breaker:fails:{key}"
                n = await r.incr(count_key)
                await r.expire(count_key, _BREAKER_WINDOW_S)
                if int(n) >= _BREAKER_THRESHOLD:
                    await r.set(f"llm:breaker:open:{key}", "1", ex=_BREAKER_OPEN_S)
                    log.warning("llm.breaker_open", binding=key)
                return
            except Exception:  # noqa: BLE001
                pass
        now = time.monotonic()
        fails = [t for t in self._local_fail.get(key, []) if t > now - _BREAKER_WINDOW_S]
        fails.append(now)
        self._local_fail[key] = fails
        if len(fails) >= _BREAKER_THRESHOLD:
            self._local_open[key] = now + _BREAKER_OPEN_S
            log.warning("llm.breaker_open", binding=key)

    async def record_success(self, key: str) -> None:
        r = self._redis()
        if r is not None:
            try:
                await r.delete(f"llm:breaker:fails:{key}")
                return
            except Exception:  # noqa: BLE001
                pass
        self._local_fail.pop(key, None)


breaker = _Breaker()

# ── org daily budget gate (P1.6; discoverable config — the F-19 lesson) ──────────────────────

_budget_cache: dict[str, tuple[float, float]] = {}   # org_id -> (spent_usd, expires_at)
_BUDGET_TTL_S = 60.0


async def _daily_spend_usd(org_id: str) -> float:
    hit = _budget_cache.get(org_id)
    now = time.monotonic()
    if hit and hit[1] > now:
        return hit[0]
    spent = 0.0
    try:
        import uuid as _uuid

        from sqlalchemy import func, select

        from ..db.models import LlmUsage
        from ..db.session import session_scope
        day_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        async with session_scope() as s:
            spent = float((await s.execute(
                select(func.coalesce(func.sum(LlmUsage.cost_usd), 0.0)).where(
                    LlmUsage.org_id == _uuid.UUID(org_id), LlmUsage.ts >= day_start)
            )).scalar_one() or 0.0)
    except Exception as exc:  # noqa: BLE001 — the gate fails OPEN; accounting is elsewhere
        log.warning("llm.budget_check_failed", error=str(exc))
    _budget_cache[org_id] = (spent, now + _BUDGET_TTL_S)
    return spent


async def _budget_gate(req: ModelRequest, settings: Settings) -> None:
    limit = float(getattr(settings, "aegisops_llm_daily_budget_usd", 0.0) or 0.0)
    org_id = req.metadata.get("org_id")
    if limit <= 0 or not org_id:
        return
    spent = await _daily_spend_usd(str(org_id))
    if spent >= limit:
        LLM_BUDGET_REFUSALS.inc()
        raise ModelError(
            "rate_limited",
            f"org daily LLM budget exhausted ({spent:.2f}/{limit:.2f} USD) — "
            f"raise AEGISOPS_LLM_DAILY_BUDGET_USD or wait for the UTC day to roll")


# ── execution ────────────────────────────────────────────────────────────────────────────────

def _chain(plan) -> list[tuple[str, str]]:
    return [(plan.provider, plan.model), *[tuple(f) for f in plan.fallbacks]]


async def _sleep_before_retry(attempt: int, err: ModelError) -> None:
    delay = err.retry_after_s if err.retry_after_s else min(
        _BACKOFF_BASE_S * (2 ** (attempt - 1)), _BACKOFF_MAX_S)
    await asyncio.sleep(delay + random.uniform(0, 0.25))


async def execute(req: ModelRequest, plan, settings: Settings) -> ModelResponse:
    """Non-streaming execution: retries on the binding, then visible failover."""
    await _budget_gate(req, settings)
    cat = catalog_mod.load()
    last: ModelError | None = None
    for hop, (provider, model) in enumerate(_chain(plan)):
        key = f"{provider}:{model}"
        if not await breaker.allow(key):
            last = ModelError("unavailable", f"circuit open for {key}", provider=provider)
            log.warning("llm.breaker_skipping", binding=key, hop=hop)
            continue
        adapter = for_provider(provider, cat, settings)
        for attempt in range(1, _MAX_ATTEMPTS_PER_HOP + 1):
            t0 = time.monotonic()
            try:
                resp = await adapter.generate(req, model)
                await breaker.record_success(key)
                resp.served_by.requested_model = plan.model
                resp.served_by.fallback_hop = hop
                if resp.latency_ms == 0:
                    resp.latency_ms = int((time.monotonic() - t0) * 1000)
                if hop:
                    log.info("llm.served_by_fallback", purpose=req.purpose,
                             binding=key, hop=hop)
                return resp
            except ModelError as e:
                last = e
                if e.kind == "context_overflow":
                    raise                       # compact-and-retry, NEVER failover (06 §7)
                if e.retriable and attempt < _MAX_ATTEMPTS_PER_HOP:
                    await _sleep_before_retry(attempt, e)
                    continue
                await breaker.record_failure(key)
                if e.failover:
                    LLM_FAILOVER.labels(provider=provider, model=model, kind=e.kind).inc()
                    break                       # next hop (empty chain for governed purposes)
                raise
    assert last is not None
    raise last


async def execute_stream(req: ModelRequest, plan,
                         settings: Settings) -> AsyncIterator[StreamEvent]:
    """Streaming execution. Failover is TURN-LOCAL and only before the first token —
    once text reached the caller, an upstream failure surfaces as the stream's error
    event (the compatibility shim keeps today's partial-answer semantics on top)."""
    await _budget_gate(req, settings)
    cat = catalog_mod.load()
    chain = _chain(plan)
    last_error: dict | None = None
    for hop, (provider, model) in enumerate(chain):
        key = f"{provider}:{model}"
        if not await breaker.allow(key):
            last_error = {"kind": "unavailable", "message": f"circuit open for {key}"}
            log.warning("llm.breaker_skipping", binding=key, hop=hop)
            continue
        adapter = for_provider(provider, cat, settings)
        emitted_text = False
        failed_pre_token = False
        async for ev in adapter.stream(req, model):
            if ev.kind == "error":
                await breaker.record_failure(key)
                if not emitted_text and hop < len(chain) - 1:
                    LLM_FAILOVER.labels(provider=provider, model=model,
                                        kind=ev.payload.get("kind", "unavailable")).inc()
                    last_error = ev.payload
                    failed_pre_token = True
                    break                       # try the next hop, nothing was shown
                yield ev
                return
            if ev.kind == "text_delta":
                emitted_text = True
            if ev.kind == "served_by":
                ev = StreamEvent(kind="served_by", payload={
                    **ev.payload, "requested_model": plan.model, "fallback_hop": hop})
            if ev.kind == "done":
                await breaker.record_success(key)
            yield ev
            if ev.kind == "done":
                return
        if not failed_pre_token:
            return  # stream ended (adapter contract guarantees done/error, but be safe)
    yield StreamEvent(kind="error", payload=last_error
                      or {"kind": "unavailable", "message": "no usable binding in plan"})
