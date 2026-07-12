"""Langfuse tracing (real SDK, langfuse v2) — one trace per run with a nested span tree.

The trace id IS the run id, so a resumed run (the human-approval continuation) lands on the
SAME trace, and the trace/context-graph/run linkage is a single id. The tree mirrors the real
call graph:

    trace  chat-request / <domain>-run
      ├─ router                          (graph-node span, via agents.timing)
      │    └─ gemini.generate            (generation: model, tokens, cost, latency)
      ├─ cloudops_agent → planner        (sub-step spans, via agents.timing)
      │    └─ terraform.plan             (tool span; failures recorded ON the span)
      ├─ approval                        (one span across the human interrupt — real wait)
      ├─ execute → terraform.apply, verify, finalize, servicenow.*, notify …

Graph nodes/sub-steps report through `agents.timing` (start_step/end_step), which drives
`step_started`/`step_ended` here — span ids are deterministic (`<run_id>:<step>`), so the
approval span opened before the interrupt is closed by the resume, in a different task or
process, with the true wall-clock wait.

Every input/output/metadata value passes through the redaction layer — secrets never reach
Langfuse. If Langfuse is unconfigured/unreachable, everything degrades to a no-op rather
than failing the request.
"""

from __future__ import annotations

import uuid as _uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

import structlog

from ..security.redaction import redact, redact_dict
from ..settings import Settings

log = structlog.get_logger(__name__)

try:
    from langfuse import Langfuse  # langfuse v2

    _HAVE_LANGFUSE = True
except Exception:  # noqa: BLE001
    _HAVE_LANGFUSE = False


# The run id the current task tree is tracing. Set once by the runner; contextvars flow into
# every coroutine/task the graph spawns, so generations/tool spans find their trace.
_current_run: ContextVar[str | None] = ContextVar("langfuse_current_run", default=None)

# Per-run stack of open observations. Graph nodes and sub-steps are strictly sequential
# within one run, so a plain per-run list yields correct parent→child nesting even when
# LangGraph hops tasks (a contextvar stack would fork per task).
_STACKS: dict[str, list[dict[str, str]]] = {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _clean(value: Any) -> Any:
    """Redact any payload shape before it leaves the process."""
    if value is None:
        return None
    if isinstance(value, dict):
        return redact_dict(value)
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


def _step_id(run_id: str, name: str) -> str:
    return f"{run_id}:{name}"


class ToolResult:
    """Mutable holder a `tool(...)` block fills in; `.output` becomes the span output."""

    output: Any = None
    metadata: dict | None = None


class LangfuseTracer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.enabled = bool(
            _HAVE_LANGFUSE and settings.langfuse_public_key and settings.langfuse_secret_key
        )
        self._client: Any = None
        if self.enabled:
            try:
                self._client = Langfuse(
                    public_key=settings.langfuse_public_key,
                    secret_key=settings.langfuse_secret_key,
                    host=settings.langfuse_host,
                )
            except Exception as e:  # noqa: BLE001
                log.warning("langfuse.init_failed", error=str(e))
                self.enabled = False

    # ── run/trace lifecycle ──────────────────────────────────────────────────
    def begin_run(self, run_id: str, name: str | None = None, *, user_id: str | None = None,
                  session_id: str | None = None, metadata: dict | None = None,
                  input: Any = None, tags: list[str] | None = None) -> None:
        """Open (or, on resume, re-attach to) the run's trace and mark it current."""
        _current_run.set(run_id)
        _STACKS.setdefault(run_id, [])
        if not self.enabled:
            return
        try:
            self._client.trace(id=run_id, name=name, user_id=user_id, session_id=session_id,
                               metadata=_clean(metadata) if metadata else None,
                               input=_clean(input), tags=tags)
        except Exception as e:  # noqa: BLE001
            log.warning("langfuse.trace_failed", error=str(e))

    def end_run(self, run_id: str, *, name: str | None = None, output: Any = None,
                metadata: dict | None = None, tags: list[str] | None = None,
                user_id: str | None = None, session_id: str | None = None) -> None:
        """Final trace upsert (name/tags/metadata become known only at the end) + cleanup."""
        if self.enabled:
            try:
                self._client.trace(id=run_id, name=name, user_id=user_id, session_id=session_id,
                                   output=_clean(output),
                                   metadata=_clean(metadata) if metadata else None, tags=tags)
            except Exception as e:  # noqa: BLE001
                log.warning("langfuse.trace_failed", error=str(e))
        _STACKS.pop(run_id, None)

    # ── graph-step spans (driven by agents.timing) ───────────────────────────
    def step_started(self, run_id: str, name: str, *, tool: str | None = None,
                     started_at: datetime | None = None, input: Any = None) -> None:
        """Open the span for a graph node / sub-step. Idempotent across a resume re-entry:
        the caller passes the ORIGINAL started_at (preserved in run_steps), so re-posting
        the same deterministic span id never shifts the true start."""
        stack = _STACKS.setdefault(run_id, [])
        parent = stack[-1]["id"] if stack else None
        oid = _step_id(run_id, name)
        if not any(f["id"] == oid for f in stack):
            stack.append({"name": name, "id": oid})
        if not self.enabled:
            return
        try:
            self._client.span(id=oid, trace_id=run_id, parent_observation_id=parent, name=name,
                              start_time=started_at or _now(),
                              metadata={"tool": tool} if tool else None, input=_clean(input))
        except Exception as e:  # noqa: BLE001
            log.warning("langfuse.span_failed", step=name, error=str(e))

    def step_ended(self, run_id: str, name: str, *, status: str = "done",
                   error: str | None = None, result: Any = None,
                   started_at: datetime | None = None, ended_at: datetime | None = None) -> None:
        """Close a step span (upsert by deterministic id — works even when the opener ran in
        another process, e.g. the approval span across the human interrupt). Failures are
        recorded ON the span (level=ERROR + message), never swallowed."""
        stack = _STACKS.get(run_id) or []
        for i in range(len(stack) - 1, -1, -1):  # pop this frame + anything left dangling above it
            if stack[i]["name"] == name:
                del stack[i:]
                break
        if not self.enabled:
            return
        try:
            self._client.span(id=_step_id(run_id, name), trace_id=run_id, name=name,
                              start_time=started_at, end_time=ended_at or _now(),
                              level="ERROR" if status == "failed" else None,
                              status_message=redact(error) if error else None,
                              output=_clean(result))
        except Exception as e:  # noqa: BLE001
            log.warning("langfuse.span_failed", step=name, error=str(e))

    # ── LLM generations ──────────────────────────────────────────────────────
    def generation(self, *, name: str, model: str | None = None, input: Any = None,
                   output: Any = None, usage: dict | None = None,
                   start_time: datetime | None = None, end_time: datetime | None = None,
                   error: str | None = None, metadata: dict | None = None) -> None:
        """Record one LLM call (tokens + cost + latency) under the current step span."""
        run_id = _current_run.get()
        if not self.enabled or not run_id:
            return
        stack = _STACKS.get(run_id) or []
        parent = stack[-1]["id"] if stack else None
        try:
            self._client.generation(
                trace_id=run_id, parent_observation_id=parent, name=name, model=model,
                input=_clean(input), output=_clean(output), usage=self._usage(usage),
                start_time=start_time, end_time=end_time or _now(),
                level="ERROR" if error else None,
                status_message=redact(error) if error else None,
                metadata=_clean(metadata) if metadata else None,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("langfuse.generation_failed", error=str(e))

    def _usage(self, usage: dict | None) -> dict | None:
        """Token counts → Langfuse usage incl. USD cost (self-hosted v2 has no price table
        for Gemini models, so cost is computed from the configured per-1M-token prices)."""
        if not usage:
            return None
        inp, out = usage.get("input"), usage.get("output")
        total = usage.get("total") or ((inp or 0) + (out or 0)) or None
        u: dict[str, Any] = {"input": inp, "output": out, "total": total, "unit": "TOKENS"}
        pin = self.settings.gemini_cost_per_1m_input
        pout = self.settings.gemini_cost_per_1m_output
        if inp is not None:
            u["input_cost"] = round(inp / 1_000_000 * pin, 8)
        if out is not None:
            u["output_cost"] = round(out / 1_000_000 * pout, 8)
        if inp is not None or out is not None:
            u["total_cost"] = round(u.get("input_cost", 0) + u.get("output_cost", 0), 8)
        return u

    # ── tool / integration spans ─────────────────────────────────────────────
    @asynccontextmanager
    async def tool(self, name: str, *, input: Any = None, metadata: dict | None = None):
        """Span around one external call (Terraform, cloud SDK, ServiceNow, RAG, …).
        Set `.output` on the yielded holder; an exception is recorded on the span
        (level=ERROR + message) and re-raised — errors are surfaced, never swallowed."""
        holder = ToolResult()
        run_id = _current_run.get()
        if not self.enabled or not run_id:
            yield holder
            return
        stack = _STACKS.setdefault(run_id, [])
        parent = stack[-1]["id"] if stack else None
        oid = str(_uuid.uuid4())
        frame = {"name": f"tool:{name}", "id": oid}
        start = _now()
        try:
            self._client.span(id=oid, trace_id=run_id, parent_observation_id=parent, name=name,
                              start_time=start, input=_clean(input),
                              metadata=_clean(metadata) if metadata else None)
        except Exception as e:  # noqa: BLE001
            log.warning("langfuse.span_failed", step=name, error=str(e))
        stack.append(frame)
        try:
            yield holder
        except BaseException as e:
            self._end_span(oid, run_id, name, end=_now(), error=str(e))
            raise
        else:
            self._end_span(oid, run_id, name, end=_now(), output=holder.output,
                           metadata=holder.metadata)
        finally:
            if frame in stack:
                stack.remove(frame)

    def _end_span(self, oid: str, run_id: str, name: str, *, end: datetime,
                  output: Any = None, error: str | None = None,
                  metadata: dict | None = None) -> None:
        try:
            self._client.span(id=oid, trace_id=run_id, name=name, end_time=end,
                              output=_clean(output),
                              level="ERROR" if error else None,
                              status_message=redact(error) if error else None,
                              metadata=_clean(metadata) if metadata else None)
        except Exception as e:  # noqa: BLE001
            log.warning("langfuse.span_failed", step=name, error=str(e))

    def event(self, name: str, *, input: Any = None, metadata: dict | None = None,
              level: str | None = None) -> None:
        """Point-in-time marker on the current trace (e.g. approval requested)."""
        run_id = _current_run.get()
        if not self.enabled or not run_id:
            return
        stack = _STACKS.get(run_id) or []
        parent = stack[-1]["id"] if stack else None
        try:
            self._client.event(trace_id=run_id, parent_observation_id=parent, name=name,
                               input=_clean(input),
                               metadata=_clean(metadata) if metadata else None, level=level)
        except Exception as e:  # noqa: BLE001
            log.warning("langfuse.event_failed", error=str(e))

    def flush(self) -> None:
        if self.enabled and self._client is not None:
            try:
                self._client.flush()
            except Exception as e:  # noqa: BLE001
                log.warning("langfuse.flush_failed", error=str(e))


async def assert_project(settings: Settings) -> str:
    """O2: verify the configured Langfuse keys resolve to the expected project, loudly.

    The historical "0 traces" regression was keys belonging to a DIFFERENT project in the same
    instance — `auth_check` wouldn't catch that. Query the public projects API with the keys and
    warn if the expected project isn't among them. Best-effort: never blocks startup (Langfuse is
    non-critical), but the warning makes a silent misconfiguration impossible to miss.

    Returns a status string ("ok" | "wrong_project" | "not_configured" | "check_failed" |
    "error") — logged as a side effect and returned so it is testable without a live Langfuse.
    """
    if not (settings.langfuse_public_key and settings.langfuse_secret_key):
        log.warning("langfuse.not_configured", detail="no keys set; tracing is a no-op")
        return "not_configured"
    import httpx

    url = f"{(settings.langfuse_host or '').rstrip('/')}/api/public/projects"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url, auth=(settings.langfuse_public_key, settings.langfuse_secret_key))
        if resp.status_code != 200:
            log.warning("langfuse.project_check_failed", status=resp.status_code,
                        detail="could not verify the project for these keys")
            return "check_failed"
        names = [p.get("name") for p in resp.json().get("data", [])]
        if settings.langfuse_expected_project in names:
            log.info("langfuse.project_ok", project=settings.langfuse_expected_project)
            return "ok"
        log.warning("langfuse.wrong_project",
                    expected=settings.langfuse_expected_project, found=names,
                    detail="these keys do NOT belong to the expected project — traces will "
                           "land elsewhere and the dashboard will read empty")
        return "wrong_project"
    except Exception as e:  # noqa: BLE001 — never block startup on a telemetry check
        log.warning("langfuse.project_check_error", error=str(e))
        return "error"


_tracer: LangfuseTracer | None = None


def get_tracer(settings: Settings) -> LangfuseTracer:
    global _tracer
    if _tracer is None:
        _tracer = LangfuseTracer(settings)
    return _tracer
