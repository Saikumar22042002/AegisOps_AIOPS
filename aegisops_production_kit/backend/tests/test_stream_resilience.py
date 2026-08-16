"""Streaming resilience (Phase 7 / BUG-03) — an upstream transport truncation (the live
`TransferEncodingError` of screenshot 15) must never crash the run: retry when nothing was
emitted, finish cleanly with a visible note + retriable error event when tokens were shown.

P1 migration note: the policy under test is UNCHANGED; the fake moved one seam down —
from a fake Gemini client to a fake provider ADAPTER — so the test now exercises the real
shim → service → executor path end-to-end (P1.3). Turn-local provider failover below this
policy is pinned separately in test_p1_routing_executor.py; the route here is pinned
fallback-free so the historical call counts stay deterministic.
"""

from __future__ import annotations

import pytest

from app.agents import llm
from app.agents.events import Emitter, RunChannel
from app.integrations.gemini import GeminiError
from app.llm import executor as llm_executor
from app.llm.types import RoutePlan, StreamEvent, Usage
from app.settings import get_settings


class _TruncatingAdapter:
    """Fake adapter whose stream dies mid-response (or immediately) N times, then succeeds.
    Emits the 05 §11 StreamEvent contract — the exact upstream failure observed live
    (aiohttp ClientPayloadError text) arrives as the stream's terminal `error` event."""

    name = "google"

    def __init__(self, fail_times: int, chunks_before_fail: list[str],
                 good_chunks: list[str]):
        self.fail_times = fail_times
        self.chunks_before_fail = chunks_before_fail
        self.good_chunks = good_chunks
        self.calls = 0

    async def stream(self, req, model):
        self.calls += 1
        if self.calls <= self.fail_times:
            for c in self.chunks_before_fail:
                yield StreamEvent(kind="text_delta", payload={"text": c})
            yield StreamEvent(kind="error", payload={
                "kind": "unavailable",
                "message": "Response payload is not completed: <TransferEncodingError: "
                           "400, 'Not enough data to satisfy transfer length header.'>"})
            return
        for c in self.good_chunks:
            yield StreamEvent(kind="text_delta", payload={"text": c})
        yield StreamEvent(kind="usage", payload=Usage(total_tokens=2).model_dump())
        yield StreamEvent(kind="served_by",
                          payload={"provider": "google", "model": model})
        yield StreamEvent(kind="done")


def _events(ch: RunChannel) -> list[tuple[str, dict]]:
    return [(e["event"], e["data"]) for e in ch.history]


def _wire(monkeypatch, fake) -> None:
    async def resolve(settings, purpose, requested_model=None, org_id=None):
        # Fallback-free plan: this suite pins the SHIM retry policy, not executor hops.
        return RoutePlan(purpose=purpose, provider="google", model="fake-model")
    monkeypatch.setattr("app.llm.service.router.resolve", resolve)
    monkeypatch.setattr("app.llm.executor.for_provider",
                        lambda provider, cat, settings: fake)
    monkeypatch.setattr(llm_executor, "breaker", llm_executor._Breaker())


async def _run(monkeypatch, fake) -> tuple[str, RunChannel]:
    _wire(monkeypatch, fake)
    ch = RunChannel("stream-test")
    text = await llm.stream_answer(get_settings(), "sys", "prompt", Emitter(ch))
    return text, ch


async def test_truncation_before_any_token_retries_transparently(monkeypatch):
    fake = _TruncatingAdapter(fail_times=2, chunks_before_fail=[],
                              good_chunks=["Hello ", "world"])
    text, ch = await _run(monkeypatch, fake)
    assert text == "Hello world"
    assert fake.calls == 3                                   # two silent retries, then success
    names = [n for n, _ in _events(ch)]
    assert names.count("token") == 2 and "error" not in names  # user never saw the blips


async def test_truncation_mid_answer_finishes_cleanly_with_note(monkeypatch):
    fake = _TruncatingAdapter(fail_times=99,
                              chunks_before_fail=["The answer is", " partial"],
                              good_chunks=[])
    text, ch = await _run(monkeypatch, fake)
    assert text.startswith("The answer is partial")
    assert "ended early" in text                             # visible truncation note appended
    events = _events(ch)
    errs = [d for n, d in events if n == "error"]
    assert len(errs) == 1
    assert errs[0]["code"] == "stream_truncated" and errs[0]["retriable"] is True
    assert fake.calls == 1                                   # tokens were shown → no re-stream


async def test_persistent_failure_with_nothing_shown_raises_gemini_error(monkeypatch):
    fake = _TruncatingAdapter(fail_times=99, chunks_before_fail=[], good_chunks=[])
    _wire(monkeypatch, fake)
    ch = RunChannel("stream-test-2")
    with pytest.raises(GeminiError):                         # callers handle this without crashing
        await llm.stream_answer(get_settings(), "sys", "prompt", Emitter(ch),
                                max_attempts=2)
    assert fake.calls == 2
