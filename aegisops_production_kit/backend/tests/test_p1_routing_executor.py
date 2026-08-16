"""P1.6 — deterministic routing + resilient execution pins (Redesign/04 §4.4/4.6).

The resilience matrix under test:
  retriable error   → bounded same-binding retries (backoff/Retry-After)
  failover kind     → NEXT hop in the validated chain, hop VISIBLE on ServedBy
  context_overflow  → raise immediately: never retried, never failed over (06 §7)
  invalid_request   → raise immediately
  breaker           → repeated failures open the circuit; open circuit skips the hop
  governed purpose  → empty chain by construction: can never silently fall back
  budget gate       → pre-dispatch refusal, loud metric
"""

from __future__ import annotations

import pytest

from app.llm import executor as ex
from app.llm import router
from app.llm.errors import ModelError
from app.llm.types import (
    CanonicalMessage,
    ModelRequest,
    ModelResponse,
    RoutePlan,
    ServedBy,
    StreamEvent,
    Usage,
)
from app.settings import Settings

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    monkeypatch.setattr(ex, "breaker", ex._Breaker())
    ex._budget_cache.clear()
    router.set_binding_resolver(None)
    # retries must not really sleep
    async def _nosleep(_):
        _SLEPT.append(_)
    global _SLEPT
    _SLEPT = []
    monkeypatch.setattr(ex.asyncio, "sleep", _nosleep)
    yield
    router.set_binding_resolver(None)


_SLEPT: list[float] = []


def _req(purpose="general", org=None) -> ModelRequest:
    md = {"org_id": org} if org else {}
    return ModelRequest(purpose=purpose, metadata=md,
                        messages=[CanonicalMessage(role="user", content="hi")])


def _plan(purpose="general", fallbacks=(), model="m-a", provider="google") -> RoutePlan:
    return RoutePlan(purpose=purpose, provider=provider, model=model,
                     fallbacks=list(fallbacks))


def _resp(provider: str, model: str) -> ModelResponse:
    return ModelResponse(content=f"from {provider}/{model}",
                         usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
                         served_by=ServedBy(provider=provider, model=model))


class FakeAdapter:
    """Scripted adapter: pops one behavior per generate() call."""
    def __init__(self, name, script):
        self.name, self.script, self.calls = name, list(script), 0

    async def generate(self, req, model):
        self.calls += 1
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return _resp(self.name, model)

    async def stream(self, req, model):
        self.calls += 1
        step = self.script.pop(0)
        for ev in step:
            yield ev


def _wire(monkeypatch, adapters: dict[str, FakeAdapter]):
    monkeypatch.setattr(ex, "for_provider",
                        lambda provider, cat, settings: adapters[provider])


# ── executor: generate ───────────────────────────────────────────────────────────────────────

async def test_success_carries_requested_model_and_hop_zero(monkeypatch):
    a = FakeAdapter("google", ["ok"])
    _wire(monkeypatch, {"google": a})
    r = await ex.execute(_req(), _plan(), Settings())
    assert r.served_by.fallback_hop == 0
    assert r.served_by.requested_model == "m-a"
    assert r.latency_ms >= 0 and a.calls == 1


async def test_retriable_error_retries_same_binding(monkeypatch):
    a = FakeAdapter("google", [ModelError("unavailable", "blip"), "ok"])
    _wire(monkeypatch, {"google": a})
    r = await ex.execute(_req(), _plan(), Settings())
    assert a.calls == 2 and r.served_by.fallback_hop == 0
    assert len(_SLEPT) == 1


async def test_retry_after_is_honored(monkeypatch):
    a = FakeAdapter("google", [ModelError("upstream_rate_limited", "429",
                                          retry_after_s=3.5), "ok"])
    _wire(monkeypatch, {"google": a})
    await ex.execute(_req(), _plan(), Settings())
    assert _SLEPT and _SLEPT[0] >= 3.5


async def test_exhausted_retries_fail_over_visibly(monkeypatch):
    boom = ModelError("unavailable", "down")
    a = FakeAdapter("google", [boom, boom, boom])
    b = FakeAdapter("anthropic", ["ok"])
    _wire(monkeypatch, {"google": a, "anthropic": b})
    r = await ex.execute(_req(), _plan(fallbacks=[("anthropic", "m-b")]), Settings())
    assert a.calls == 3 and b.calls == 1
    assert r.served_by.provider == "anthropic"
    assert r.served_by.fallback_hop == 1                  # VISIBLE hop (04 §4.6)
    assert r.served_by.requested_model == "m-a"           # honest requested vs served


async def test_governed_purpose_never_silently_falls_back(monkeypatch):
    boom = ModelError("unavailable", "down")
    a = FakeAdapter("google", [boom, boom, boom])
    _wire(monkeypatch, {"google": a})
    with pytest.raises(ModelError) as e:
        await ex.execute(_req(purpose="router"), _plan(purpose="router"), Settings())
    assert e.value.kind == "unavailable" and a.calls == 3


async def test_context_overflow_never_retries_never_fails_over(monkeypatch):
    a = FakeAdapter("google", [ModelError("context_overflow", "too big")])
    b = FakeAdapter("anthropic", ["ok"])
    _wire(monkeypatch, {"google": a, "anthropic": b})
    with pytest.raises(ModelError) as e:
        await ex.execute(_req(), _plan(fallbacks=[("anthropic", "m-b")]), Settings())
    assert e.value.kind == "context_overflow"
    assert a.calls == 1 and b.calls == 0


async def test_invalid_request_raises_immediately(monkeypatch):
    a = FakeAdapter("google", [ModelError("invalid_request", "bad")])
    _wire(monkeypatch, {"google": a})
    with pytest.raises(ModelError) as e:
        await ex.execute(_req(), _plan(), Settings())
    assert e.value.kind == "invalid_request" and a.calls == 1 and not _SLEPT


async def test_breaker_opens_after_threshold_and_skips_binding(monkeypatch):
    for _ in range(5):
        await ex.breaker.record_failure("google:m-a")
    assert await ex.breaker.allow("google:m-a") is False
    a = FakeAdapter("google", ["ok"])
    b = FakeAdapter("anthropic", ["ok"])
    _wire(monkeypatch, {"google": a, "anthropic": b})
    r = await ex.execute(_req(), _plan(fallbacks=[("anthropic", "m-b")]), Settings())
    assert a.calls == 0 and b.calls == 1                  # open circuit skipped hop 0
    assert r.served_by.fallback_hop == 1


async def test_budget_gate_refuses_before_dispatch(monkeypatch):
    async def spent(_org):
        return 12.0
    monkeypatch.setattr(ex, "_daily_spend_usd", spent)
    a = FakeAdapter("google", ["ok"])
    _wire(monkeypatch, {"google": a})
    s = Settings(aegisops_llm_daily_budget_usd=10.0)
    with pytest.raises(ModelError) as e:
        await ex.execute(_req(org="9b3e7c1e-0000-0000-0000-000000000001"), _plan(), s)
    assert "budget exhausted" in str(e.value) and a.calls == 0
    # gate off (default) → dispatch proceeds
    r = await ex.execute(_req(org="9b3e7c1e-0000-0000-0000-000000000001"),
                         _plan(), Settings())
    assert r.content.startswith("from google")


# ── executor: streaming ──────────────────────────────────────────────────────────────────────

def _stream_ok(provider, model, text=("he", "llo")):
    evs = [StreamEvent(kind="text_delta", payload={"text": t}) for t in text]
    evs += [StreamEvent(kind="usage", payload=Usage(total_tokens=2).model_dump()),
            StreamEvent(kind="served_by", payload={"provider": provider, "model": model}),
            StreamEvent(kind="done")]
    return evs


async def test_stream_failover_before_first_token(monkeypatch):
    a = FakeAdapter("google", [[StreamEvent(kind="error",
                                            payload={"kind": "unavailable", "message": "x"})]])
    b = FakeAdapter("anthropic", [_stream_ok("anthropic", "m-b")])
    _wire(monkeypatch, {"google": a, "anthropic": b})
    events = [ev async for ev in ex.execute_stream(
        _req(), _plan(fallbacks=[("anthropic", "m-b")]), Settings())]
    kinds = [e.kind for e in events]
    assert kinds[-1] == "done" and "error" not in kinds
    served = next(e for e in events if e.kind == "served_by").payload
    assert served["fallback_hop"] == 1 and served["requested_model"] == "m-a"


async def test_stream_error_after_tokens_is_surfaced_not_failed_over(monkeypatch):
    a = FakeAdapter("google", [[
        StreamEvent(kind="text_delta", payload={"text": "partial"}),
        StreamEvent(kind="error", payload={"kind": "unavailable", "message": "cut"})]])
    b = FakeAdapter("anthropic", [_stream_ok("anthropic", "m-b")])
    _wire(monkeypatch, {"google": a, "anthropic": b})
    events = [ev async for ev in ex.execute_stream(
        _req(), _plan(fallbacks=[("anthropic", "m-b")]), Settings())]
    assert [e.kind for e in events] == ["text_delta", "error"]
    assert b.calls == 0                                    # duplicated answers forbidden


async def test_stream_exhausted_chain_yields_single_error(monkeypatch):
    err = [StreamEvent(kind="error", payload={"kind": "unavailable", "message": "x"})]
    a = FakeAdapter("google", [list(err)])
    b = FakeAdapter("anthropic", [list(err)])
    _wire(monkeypatch, {"google": a, "anthropic": b})
    events = [ev async for ev in ex.execute_stream(
        _req(), _plan(fallbacks=[("anthropic", "m-b")]), Settings())]
    assert [e.kind for e in events] == ["error"]


# ── router ───────────────────────────────────────────────────────────────────────────────────

async def test_router_default_binding_and_pin(monkeypatch):
    s = Settings(gemini_api_key="k")
    plan = await router.resolve(s, "knowledge")
    assert plan.model == "gemini-3.5-flash" and plan.pinned_by == "default"
    assert plan.fallbacks == [("google", "gemini-flash-latest")]
    pinned = await router.resolve(s, "knowledge", requested_model="gemini-2.5-flash")
    assert pinned.model == "gemini-2.5-flash" and pinned.pinned_by == "request"


async def test_router_governed_purposes_ignore_user_pins():
    s = Settings(gemini_api_key="k")
    plan = await router.resolve(s, "router", requested_model="gemini-2.5-flash")
    assert plan.model == "gemini-3.5-flash" and plan.pinned_by == "default"
    assert plan.fallbacks == []                            # never silent-fallback


async def test_router_fallbacks_exclude_unconfigured_providers():
    plan = await router.resolve(Settings(gemini_api_key=""), "knowledge")
    # google unconfigured: the same-family fallback disappears from the plan —
    # a hop to a keyless provider would be a guaranteed mid-turn failure.
    assert plan.fallbacks == []


async def test_router_binding_resolver_wins_and_failure_falls_back():
    s = Settings(gemini_api_key="k")

    async def bound(org, purpose):
        return "gemini-2.5-flash"
    router.set_binding_resolver(bound)
    plan = await router.resolve(s, "knowledge", org_id="org-1")
    assert plan.model == "gemini-2.5-flash" and plan.pinned_by == "binding"

    async def broken(org, purpose):
        raise RuntimeError("db down")
    router.set_binding_resolver(broken)
    plan2 = await router.resolve(s, "knowledge", org_id="org-1")
    assert plan2.pinned_by == "default"                    # lookup failure never fails a call


async def test_router_rejects_incapable_pin():
    with pytest.raises(ModelError):
        await router.resolve(Settings(gemini_api_key="k"), "extract",
                             requested_model="gemini-embedding-001")
