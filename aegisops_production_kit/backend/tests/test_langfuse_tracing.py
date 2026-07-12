"""Langfuse tracing — the span tree a run produces (2026-07-06 observability fix).

Regression class: the app used to send ONE flat trace per run (no spans, no generations,
no tokens/cost) — and to a project the dashboard wasn't watching. These tests pin the
contract at the SDK boundary with a fake client (every call the tracer would send is
captured and asserted), plus an optional integration test against the live Langfuse API.

Invariants:
  * one root trace per run, trace id == run id (resume re-attaches, never a second trace);
  * step spans have deterministic ids (`<run_id>:<step>`) and nest parent→child in call order;
  * the approval span survives the interrupt: closed from a "different process" (fresh stack)
    it still carries the ORIGINAL start time — the true human wait;
  * LLM generations carry token usage AND computed USD cost;
  * a failing tool records level=ERROR + the message ON the span and re-raises;
  * secrets never reach a payload (inputs/outputs/metadata all pass redaction);
  * unconfigured Langfuse ⇒ every call is a safe no-op.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.integrations import langfuse_client as lc
from app.integrations.langfuse_client import LangfuseTracer
from app.settings import Settings


class FakeLangfuse:
    """Captures every SDK call the tracer makes."""

    def __init__(self) -> None:
        self.traces: list[dict] = []
        self.spans: list[dict] = []
        self.generations: list[dict] = []
        self.events: list[dict] = []
        self.flushed = 0

    def trace(self, **kw):
        self.traces.append(kw)

    def span(self, **kw):
        self.spans.append(kw)

    def generation(self, **kw):
        self.generations.append(kw)

    def event(self, **kw):
        self.events.append(kw)

    def flush(self):
        self.flushed += 1


@pytest.fixture
def tracer(monkeypatch):
    """An enabled tracer writing to a FakeLangfuse, with clean module state."""
    settings = Settings(langfuse_public_key="pk-test", langfuse_secret_key="sk-test",
                        _env_file=None)
    t = LangfuseTracer.__new__(LangfuseTracer)
    t.settings = settings
    t.enabled = True
    t._client = FakeLangfuse()
    lc._STACKS.clear()
    lc._current_run.set(None)
    yield t
    lc._STACKS.clear()
    lc._current_run.set(None)


def _run_id() -> str:
    return str(uuid.uuid4())


# ── root trace ────────────────────────────────────────────────────────────────


def test_root_trace_id_is_run_id_and_resume_reuses_it(tracer):
    run = _run_id()
    tracer.begin_run(run, "chat-request", user_id="maya", session_id="sess-1",
                     metadata={"context_id": run}, input={"message": "hi"})
    tracer.end_run(run, name="cloudops-run", output={"status": "interrupted"})
    # resume — same run id, same trace
    tracer.begin_run(run, None, metadata={"resume": True}, input={"resume": {"decision": "approved"}})
    tracer.end_run(run, output={"status": "completed"})
    fake = tracer._client
    assert {t["id"] for t in fake.traces} == {run}, "a resumed run must NOT open a second trace"
    assert fake.traces[0]["user_id"] == "maya"
    assert fake.traces[0]["session_id"] == "sess-1"
    assert fake.traces[1]["name"] == "cloudops-run"


# ── span nesting ──────────────────────────────────────────────────────────────


async def test_step_spans_nest_in_call_order(tracer):
    run = _run_id()
    tracer.begin_run(run, "chat-request")
    tracer.step_started(run, "router")
    tracer.generation(name="gemini.generate", model="gemini-3.5-flash",
                      input={"prompt": "classify"}, output="{}")
    tracer.step_ended(run, "router")
    tracer.step_started(run, "planner", tool="terraform")
    async with tracer.tool("terraform.plan", input={"vars": {"name": "web-01"}}) as t:
        async with tracer.tool("terraform.show", input={}) as inner:
            inner.output = {"add": 1}
        t.output = {"add": 1}
    tracer.step_ended(run, "planner")
    fake = tracer._client

    router_open = next(s for s in fake.spans if s.get("name") == "router" and "end_time" not in s)
    assert router_open["id"] == f"{run}:router"
    assert router_open["parent_observation_id"] is None  # direct child of the trace

    gen = fake.generations[0]
    assert gen["trace_id"] == run
    assert gen["parent_observation_id"] == f"{run}:router", "generation must nest under its step"

    plan_span = next(s for s in fake.spans if s.get("name") == "terraform.plan" and "input" in s)
    assert plan_span["parent_observation_id"] == f"{run}:planner", "tool must nest under its step"
    show_span = next(s for s in fake.spans if s.get("name") == "terraform.show" and "input" in s)
    assert show_span["parent_observation_id"] == plan_span["id"], "nested call ⇒ nested span (A→B→C)"


def test_approval_span_survives_the_interrupt(tracer):
    """The approval span is closed by the RESUME — different task/process, fresh stack —
    and still reports the original start, so the wall-clock human wait is on the span."""
    run = _run_id()
    t0 = datetime.now(timezone.utc) - timedelta(minutes=7)
    tracer.begin_run(run, "chat-request")
    tracer.step_started(run, "approval", started_at=t0)
    # interrupt: the first process is gone (its stack with it)
    lc._STACKS.clear()
    tracer.begin_run(run)  # the resume re-attaches
    tracer.step_started(run, "approval", started_at=t0)  # re-entry passes the ORIGINAL start
    tracer.step_ended(run, "approval", started_at=t0)
    fake = tracer._client
    closes = [s for s in fake.spans if s.get("name") == "approval" and s.get("end_time")]
    assert closes, "approval span never closed"
    close = closes[-1]
    assert close["id"] == f"{run}:approval", "deterministic id lets the resume close the span"
    assert close["start_time"] == t0
    assert (close["end_time"] - t0) >= timedelta(minutes=7), "human wait must be the span duration"


# ── errors on spans ───────────────────────────────────────────────────────────


def test_failed_step_is_an_error_span(tracer):
    run = _run_id()
    tracer.begin_run(run, "chat-request")
    tracer.step_started(run, "execute")
    tracer.step_ended(run, "execute", status="failed", error="terraform apply failed: AccessDenied")
    close = [s for s in tracer._client.spans if s.get("end_time")][-1]
    assert close["level"] == "ERROR"
    assert "AccessDenied" in close["status_message"]


async def test_tool_error_lands_on_span_and_propagates(tracer):
    run = _run_id()
    tracer.begin_run(run, "chat-request")
    with pytest.raises(RuntimeError, match="boom 503"):
        async with tracer.tool("servicenow.post", input={"table": "incident"}):
            raise RuntimeError("boom 503")
    close = [s for s in tracer._client.spans if s.get("name") == "servicenow.post" and s.get("end_time")][-1]
    assert close["level"] == "ERROR"
    assert "boom 503" in close["status_message"]
    assert not lc._STACKS[run], "errored tool frame must not dangle on the stack"


# ── generations: tokens + cost ───────────────────────────────────────────────


def test_generation_carries_usage_and_computed_cost(tracer):
    run = _run_id()
    tracer.begin_run(run, "chat-request")
    tracer.generation(name="gemini.generate", model="gemini-3.5-flash",
                      input={"prompt": "hello"}, output="world",
                      usage={"input": 1_000_000, "output": 2_000_000})
    usage = tracer._client.generations[0]["usage"]
    assert usage["input"] == 1_000_000 and usage["output"] == 2_000_000
    assert usage["total"] == 3_000_000
    assert usage["unit"] == "TOKENS"
    s = tracer.settings
    expected_in = 1.0 * s.gemini_cost_per_1m_input     # 1M input tokens
    expected_out = 2.0 * s.gemini_cost_per_1m_output   # 2M output tokens
    assert usage["input_cost"] == pytest.approx(expected_in)
    assert usage["output_cost"] == pytest.approx(expected_out)
    assert usage["total_cost"] == pytest.approx(expected_in + expected_out)


def test_failed_generation_is_error_level(tracer):
    run = _run_id()
    tracer.begin_run(run, "chat-request")
    tracer.generation(name="gemini.generate", model="gemini-3.5-flash",
                      input={"prompt": "x"}, error="API key not valid")
    gen = tracer._client.generations[0]
    assert gen["level"] == "ERROR"
    assert "API key not valid" in gen["status_message"]


# ── secrets never reach Langfuse ─────────────────────────────────────────────


async def test_no_secret_survives_into_any_payload(tracer):
    run = _run_id()
    secret = "hunter2-super-secret"
    tracer.begin_run(run, "chat-request", metadata={"password": secret},
                     input={"message": f"use password={secret} please"})
    tracer.step_started(run, "execute", input={"aws_secret_access_key": secret})
    tracer.generation(name="gemini.generate", input={"prompt": f"client_secret={secret}"},
                      output=f"the token is api_key={secret}")
    async with tracer.tool("terraform.apply", input={"vars": {"db_password": secret}}) as t:
        t.output = {"connection": f"password: {secret}"}
    tracer.step_ended(run, "execute", result={"credential": secret})
    tracer.end_run(run, output={"answer": f"AWS_SESSION_TOKEN={secret}"})

    fake = tracer._client
    blob = repr(fake.traces) + repr(fake.spans) + repr(fake.generations) + repr(fake.events)
    assert secret not in blob, "a secret value reached a Langfuse payload"


# ── disabled ⇒ safe no-op ────────────────────────────────────────────────────


async def test_disabled_tracer_is_a_noop(monkeypatch):
    settings = Settings(langfuse_public_key="", langfuse_secret_key="", _env_file=None)
    t = LangfuseTracer(settings)
    assert not t.enabled
    run = _run_id()
    t.begin_run(run, "chat-request")
    t.step_started(run, "router")
    t.generation(name="g", input={"prompt": "x"})
    async with t.tool("terraform.plan") as h:
        h.output = {"ok": True}
    t.step_ended(run, "router")
    t.end_run(run)
    t.flush()  # nothing raises


# ── integration: the live Langfuse API confirms the tree ─────────────────────


async def test_live_langfuse_receives_root_trace_with_children():
    """End-to-end: emit a small tree through the REAL SDK, then read it back via the
    Langfuse public API. Skips cleanly when Langfuse/keys aren't available."""
    if os.getenv("AEGISOPS_TEST_LIVE_DATASTORES") != "1":
        pytest.skip("integration test: set AEGISOPS_TEST_LIVE_DATASTORES=1 (run via `make test`)")
    import httpx

    from app.settings import get_settings
    settings = get_settings()
    if not (settings.langfuse_public_key and settings.langfuse_secret_key):
        pytest.skip("Langfuse keys not configured")

    tracer = LangfuseTracer(settings)
    if not tracer.enabled:
        pytest.skip("Langfuse SDK not available")

    run = str(uuid.uuid4())
    tracer.begin_run(run, "chat-request", user_id="pytest", metadata={"context_id": run},
                     input={"message": "tracing self-test password=synthetic-secret-123"})
    tracer.step_started(run, "router")
    tracer.generation(name="gemini.generate", model="gemini-3.5-flash",
                      input={"prompt": "classify"}, output="{}",
                      usage={"input": 10, "output": 5})
    tracer.step_ended(run, "router")
    tracer.end_run(run, name="general-run", output={"status": "completed"})
    tracer.flush()

    auth = (settings.langfuse_public_key, settings.langfuse_secret_key)
    base = settings.langfuse_host.rstrip("/")
    async with httpx.AsyncClient(timeout=15.0, auth=auth) as client:
        resp = None
        for _ in range(10):  # ingestion is async server-side
            resp = await client.get(f"{base}/api/public/traces/{run}")
            if resp.status_code == 200:
                break
            import asyncio
            await asyncio.sleep(1)
        if resp is None or resp.status_code in (401, 403):
            pytest.skip("Langfuse API not reachable/authorized from this environment")
        assert resp.status_code == 200, f"trace {run} never arrived: {resp.status_code}"
        data = resp.json()
        assert data["id"] == run
        names = {o["name"] for o in data.get("observations", [])}
        assert "router" in names and "gemini.generate" in names
        gen = next(o for o in data["observations"] if o["name"] == "gemini.generate")
        assert (gen.get("usage") or {}).get("input") == 10, "token usage must be recorded"
        assert "synthetic-secret-123" not in resp.text, "secret leaked into the stored trace"


# ═══ O2 — startup project-key assertion (the "0 traces / wrong project" regression guard) ═════


class _FakeResp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


class _FakeAsyncClient:
    """Minimal httpx.AsyncClient stand-in returning a canned /projects response."""
    _resp = _FakeResp(200, {"data": [{"id": "p1", "name": "aegisops"}]})

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, auth=None):
        return type(self)._resp


async def test_assert_project_ok_when_keys_match(monkeypatch):
    import httpx

    from app.integrations import langfuse_client as lcmod

    _FakeAsyncClient._resp = _FakeResp(200, {"data": [{"id": "p1", "name": "aegisops"}]})
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    s = Settings(langfuse_public_key="pk", langfuse_secret_key="sk",
                 langfuse_expected_project="aegisops", _env_file=None)
    assert await lcmod.assert_project(s) == "ok"


async def test_assert_project_warns_on_wrong_project(monkeypatch):
    import httpx

    from app.integrations import langfuse_client as lcmod

    _FakeAsyncClient._resp = _FakeResp(200, {"data": [{"id": "p9", "name": "someone-elses-project"}]})
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    s = Settings(langfuse_public_key="pk", langfuse_secret_key="sk",
                 langfuse_expected_project="aegisops", _env_file=None)
    assert await lcmod.assert_project(s) == "wrong_project", \
        "a key that belongs to another project must be flagged (the 0-traces regression)"


async def test_assert_project_noop_without_keys():
    from app.integrations import langfuse_client as lcmod

    s = Settings(langfuse_public_key="", langfuse_secret_key="", _env_file=None)
    assert await lcmod.assert_project(s) == "not_configured"
