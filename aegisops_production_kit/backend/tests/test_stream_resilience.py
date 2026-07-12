"""Streaming resilience (Phase 7 / BUG-03) — an upstream transport truncation (the live
`TransferEncodingError` of screenshot 15) must never crash the run: retry when nothing was
emitted, finish cleanly with a visible note + retriable error event when tokens were shown."""

from __future__ import annotations

import pytest

from app.agents import llm
from app.agents.events import Emitter, RunChannel
from app.integrations.gemini import GeminiError
from app.settings import get_settings


class _Chunk:
    """Shape of a google-genai stream chunk: `.text` + `.usage_metadata`."""

    def __init__(self, text: str):
        self.text = text
        self.usage_metadata = None


class _TruncatingGemini:
    """Fake Gemini whose stream dies mid-response (or immediately) N times, then succeeds.
    Implements `astream` (raw chunks) — the interface `stream_answer` consumes since the
    tracing fix, which reads token usage off the final chunk."""

    def __init__(self, fail_times: int, chunks_before_fail: list[str], good_chunks: list[str]):
        self.enabled = True
        self.model = "fake-model"
        self.fail_times = fail_times
        self.chunks_before_fail = chunks_before_fail
        self.good_chunks = good_chunks
        self.calls = 0

    async def astream(self, system, prompt, tools=None):
        self.calls += 1
        if self.calls <= self.fail_times:
            for c in self.chunks_before_fail:
                yield _Chunk(c)
            # The exact upstream failure observed live (aiohttp ClientPayloadError text).
            raise RuntimeError("Response payload is not completed: <TransferEncodingError: 400, "
                               "'Not enough data to satisfy transfer length header.'>")
        for c in self.good_chunks:
            yield _Chunk(c)


def _events(ch: RunChannel) -> list[tuple[str, dict]]:
    return [(e["event"], e["data"]) for e in ch.history]


async def _run(monkeypatch, fake) -> tuple[str, RunChannel]:
    monkeypatch.setattr(llm, "get_gemini", lambda _s: fake)
    ch = RunChannel("stream-test")
    text = await llm.stream_answer(get_settings(), "sys", "prompt", Emitter(ch))
    return text, ch


async def test_truncation_before_any_token_retries_transparently(monkeypatch):
    fake = _TruncatingGemini(fail_times=2, chunks_before_fail=[], good_chunks=["Hello ", "world"])
    text, ch = await _run(monkeypatch, fake)
    assert text == "Hello world"
    assert fake.calls == 3                                   # two silent retries, then success
    names = [n for n, _ in _events(ch)]
    assert names.count("token") == 2 and "error" not in names  # user never saw the blips


async def test_truncation_mid_answer_finishes_cleanly_with_note(monkeypatch):
    fake = _TruncatingGemini(fail_times=99, chunks_before_fail=["The answer is", " partial"],
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
    fake = _TruncatingGemini(fail_times=99, chunks_before_fail=[], good_chunks=[])
    monkeypatch.setattr(llm, "get_gemini", lambda _s: fake)
    ch = RunChannel("stream-test-2")
    with pytest.raises(GeminiError):                         # callers handle this without crashing
        await llm.stream_answer(get_settings(), "sys", "prompt", Emitter(ch), max_attempts=2)
    assert fake.calls == 2
