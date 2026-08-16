"""P1.3 — LLM service facade + byte-compatible shim parity (07 P1.3).

The service must record EXACTLY one ledger row + one Langfuse generation per call —
success and error paths, honest served-vs-requested models. The shim must keep the
historical `agents/llm.py` semantics its callers depend on: `GeminiError` for
configuration problems, transparent retry when nothing streamed, truncation note +
retriable error event when tokens were already shown.
"""

from __future__ import annotations

import pytest

from app.agents import llm as shim
from app.integrations.gemini import GeminiError
from app.llm import service
from app.llm.errors import ModelError
from app.llm.types import (
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


class Recorder:
    def __init__(self, monkeypatch):
        self.ledger: list[dict] = []
        self.traces: list[dict] = []

        def record_usage(settings, **kw):
            self.ledger.append(kw)
            return {}

        class _Tracer:
            def generation(inner, **kw):
                self.traces.append(kw)

        monkeypatch.setattr("app.llm.service.record_usage", record_usage)
        monkeypatch.setattr("app.llm.service.get_tracer", lambda s: _Tracer())


def _plan(model="gemini-3.5-flash") -> RoutePlan:
    return RoutePlan(purpose="general", provider="google", model=model)


def _resp(model="gemini-3.5-flash", provider="google", content="hello",
          hop=0) -> ModelResponse:
    return ModelResponse(content=content,
                         usage=Usage(input_tokens=3, output_tokens=2, total_tokens=5),
                         served_by=ServedBy(provider=provider, model=model,
                                            requested_model="gemini-3.5-flash",
                                            fallback_hop=hop))


@pytest.fixture
def rec(monkeypatch) -> Recorder:
    async def resolve(settings, purpose, requested_model=None, org_id=None):
        return _plan()
    monkeypatch.setattr("app.llm.service.router", type("R", (), {
        "resolve": staticmethod(resolve)}))
    return Recorder(monkeypatch)


# ── service.generate ─────────────────────────────────────────────────────────────────────────

async def test_generate_records_ledger_and_trace_once(rec, monkeypatch):
    async def execute(req, plan, settings):
        return _resp()
    monkeypatch.setattr("app.llm.service.executor.execute", execute)
    r = await service.generate(Settings(), purpose="general", system="s", prompt="p")
    assert r.content == "hello"
    assert len(rec.ledger) == 1 and len(rec.traces) == 1
    row = rec.ledger[0]
    assert row["purpose"] == "general" and row["provider"] == "google"
    assert row["model"] == "gemini-3.5-flash"
    assert row["requested_model"] == "gemini-3.5-flash"
    assert row["outcome"] == "ok" and row["usage"] == {"input": 3, "output": 2, "total": 5}
    assert rec.traces[0]["name"] == "llm.general"


async def test_generate_error_path_still_lands_on_the_ledger(rec, monkeypatch):
    async def execute(req, plan, settings):
        raise ModelError("unavailable", "down", provider="google")
    monkeypatch.setattr("app.llm.service.executor.execute", execute)
    with pytest.raises(ModelError):
        await service.generate(Settings(), purpose="general", system=None, prompt="p")
    assert len(rec.ledger) == 1
    assert rec.ledger[0]["outcome"].startswith("error:")
    assert rec.traces[0]["error"]


async def test_generate_records_honest_served_vs_requested(rec, monkeypatch):
    async def execute(req, plan, settings):
        return _resp(model="claude-sonnet-5", provider="anthropic", hop=1)
    monkeypatch.setattr("app.llm.service.executor.execute", execute)
    await service.generate(Settings(), purpose="general", system=None, prompt="p")
    row = rec.ledger[0]
    assert row["model"] == "claude-sonnet-5"                 # served (truth)
    assert row["requested_model"] == "gemini-3.5-flash"      # requested (plan)
    assert row["provider"] == "anthropic"


# ── service.stream ───────────────────────────────────────────────────────────────────────────

def _ok_stream():
    return [StreamEvent(kind="text_delta", payload={"text": "he"}),
            StreamEvent(kind="text_delta", payload={"text": "llo"}),
            StreamEvent(kind="usage",
                        payload=Usage(input_tokens=1, output_tokens=2,
                                      total_tokens=3).model_dump()),
            StreamEvent(kind="served_by",
                        payload={"provider": "google", "model": "gemini-3.5-flash",
                                 "requested_model": "gemini-3.5-flash",
                                 "fallback_hop": 0}),
            StreamEvent(kind="done")]


def _wire_stream(monkeypatch, events):
    async def execute_stream(req, plan, settings):
        for ev in events:
            yield ev
    monkeypatch.setattr("app.llm.service.executor.execute_stream", execute_stream)


async def test_stream_records_once_at_done(rec, monkeypatch):
    _wire_stream(monkeypatch, _ok_stream())
    out = [e async for e in service.stream(Settings(), purpose="knowledge",
                                           system="s", prompt="p")]
    assert [e.kind for e in out][-1] == "done"
    assert len(rec.ledger) == 1
    assert rec.ledger[0]["purpose"] == "knowledge"
    assert rec.ledger[0]["usage"] == {"input": 1, "output": 2, "total": 3}
    assert rec.traces[0]["output"] == "hello"


async def test_stream_records_error_outcome(rec, monkeypatch):
    _wire_stream(monkeypatch, [
        StreamEvent(kind="text_delta", payload={"text": "par"}),
        StreamEvent(kind="error", payload={"kind": "unavailable", "message": "cut"})])
    out = [e async for e in service.stream(Settings(), purpose="general",
                                           system=None, prompt="p")]
    assert out[-1].kind == "error"
    assert len(rec.ledger) == 1 and rec.ledger[0]["outcome"].startswith("error:")


# ── classify + extract ───────────────────────────────────────────────────────────────────────

async def test_classify_json_schema_path_and_fallback_parser(rec, monkeypatch):
    async def execute(req, plan, settings):
        assert req.response_schema == {"type": "object"}
        return _resp(content='{"intent": "read"}')
    monkeypatch.setattr("app.llm.service.executor.execute", execute)
    out = await service.classify_json(Settings(), "s", "p",
                                      response_schema={"type": "object"})
    assert out == {"intent": "read"}

    async def execute_fenced(req, plan, settings):
        return _resp(content='prose ```json\n{"a": 1}\n``` more prose')
    monkeypatch.setattr("app.llm.service.executor.execute", execute_fenced)
    assert await service.classify_json(Settings(), "s", "p") == {"a": 1}


def test_extract_json_is_byte_compatible_with_the_eval_runner():
    # rule zero: the eval runner replays through this exact function via the shim alias
    assert shim._extract_json is service.extract_json
    assert service.extract_json('x {"k": [1, 2]} y') == {"k": [1, 2]}
    with pytest.raises(ValueError):
        service.extract_json("no json here")


# ── shim parity ──────────────────────────────────────────────────────────────────────────────

class FakeEmitter:
    def __init__(self):
        self.tokens: list[str] = []
        self.errors: list[dict] = []
        self.served: list[dict] = []

    async def token(self, text):
        self.tokens.append(text)

    async def served_by(self, data):
        self.served.append(data)

    async def error(self, message, code=None, retriable=False, **kw):
        self.errors.append({"message": message, "code": code, "retriable": retriable})


def _shim_stream(monkeypatch, scripts: list[list[StreamEvent]]):
    """Each call to service.stream consumes one script (per-attempt behavior)."""
    calls = {"n": 0}

    async def stream(settings, **kw):
        script = scripts[min(calls["n"], len(scripts) - 1)]
        calls["n"] += 1
        for ev in script:
            yield ev
    monkeypatch.setattr("app.llm.service.stream", stream)
    return calls


async def test_shim_clean_stream(monkeypatch):
    _shim_stream(monkeypatch, [_ok_stream()])
    em = FakeEmitter()
    out = await shim.stream_answer(Settings(), "s", "p", em)
    assert out == "hello" and em.tokens == ["he", "llo"] and not em.errors
    # P1.7: the shim forwards the honest serving metadata as the served_by SSE event.
    assert em.served and em.served[0]["model"] == "gemini-3.5-flash"
    assert em.served[0]["fallback_hop"] == 0


async def test_shim_retries_when_nothing_was_emitted(monkeypatch):
    calls = _shim_stream(monkeypatch, [
        [StreamEvent(kind="error", payload={"kind": "unavailable", "message": "blip"})],
        _ok_stream()])
    em = FakeEmitter()
    out = await shim.stream_answer(Settings(), "s", "p", em)
    assert out == "hello" and calls["n"] == 2 and not em.errors


async def test_shim_truncates_cleanly_after_tokens(monkeypatch):
    _shim_stream(monkeypatch, [[
        StreamEvent(kind="text_delta", payload={"text": "partial"}),
        StreamEvent(kind="error", payload={"kind": "unavailable", "message": "cut"})]])
    em = FakeEmitter()
    out = await shim.stream_answer(Settings(), "s", "p", em)
    assert out.startswith("partial") and out.endswith(shim._TRUNCATION_NOTE)
    assert em.errors and em.errors[0]["retriable"] is True
    assert em.errors[0]["code"] == "stream_truncated"


async def test_shim_config_errors_raise_gemini_error_immediately(monkeypatch):
    calls = _shim_stream(monkeypatch, [[
        StreamEvent(kind="error", payload={"kind": "auth_permanent",
                                           "message": "no credentials configured"})]])
    with pytest.raises(GeminiError):
        await shim.stream_answer(Settings(), "s", "p", FakeEmitter())
    assert calls["n"] == 1                                   # never retried


async def test_shim_exhausted_retries_raise_gemini_error(monkeypatch):
    err = [StreamEvent(kind="error", payload={"kind": "unavailable", "message": "down"})]
    calls = _shim_stream(monkeypatch, [list(err), list(err), list(err)])
    with pytest.raises(GeminiError, match="after 3 attempt"):
        await shim.stream_answer(Settings(), "s", "p", FakeEmitter())
    assert calls["n"] == 3


async def test_shim_classify_maps_model_errors_to_gemini_error(monkeypatch):
    async def classify(settings, system, prompt, **kw):
        raise ModelError("auth_permanent", "no credentials configured for 'google'")
    monkeypatch.setattr("app.llm.service.classify_json", classify)
    with pytest.raises(GeminiError):
        await shim.classify_json(Settings(), "s", "p")
