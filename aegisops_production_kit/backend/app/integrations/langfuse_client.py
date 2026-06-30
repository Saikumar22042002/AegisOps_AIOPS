"""Langfuse tracing client (real SDK).

One trace per request, linked to the context-graph id; spans for intent/routing/planning/
each step/tool/RAG/approval/outcome with token usage + latency. Sanitized I/O only — never
log secrets (callers pass redacted payloads). If Langfuse is unreachable, tracing degrades
to a no-op rather than failing the request.
"""

from __future__ import annotations

from typing import Any

import structlog

from ..settings import Settings

log = structlog.get_logger(__name__)

try:
    from langfuse import Langfuse  # langfuse v2

    _HAVE_LANGFUSE = True
except Exception:  # noqa: BLE001
    _HAVE_LANGFUSE = False


class LangfuseTracer:
    def __init__(self, settings: Settings) -> None:
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

    def trace(self, name: str, *, context_id: str | None = None, user_id: str | None = None,
              metadata: dict | None = None, input: Any = None):
        """Create a trace; returns the trace handle or a no-op shim."""
        if not self.enabled:
            return _NoopSpan()
        try:
            return self._client.trace(
                name=name,
                user_id=user_id,
                metadata={**(metadata or {}), "context_id": context_id},
                input=input,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("langfuse.trace_failed", error=str(e))
            return _NoopSpan()

    def flush(self) -> None:
        if self.enabled and self._client is not None:
            try:
                self._client.flush()
            except Exception as e:  # noqa: BLE001
                log.warning("langfuse.flush_failed", error=str(e))


class _NoopSpan:
    """No-op trace/span used when Langfuse is disabled or unreachable."""

    def span(self, **_kw: Any) -> _NoopSpan:
        return self

    def generation(self, **_kw: Any) -> _NoopSpan:
        return self

    def event(self, **_kw: Any) -> _NoopSpan:
        return self

    def update(self, **_kw: Any) -> None:
        return None

    def end(self, **_kw: Any) -> None:
        return None


_tracer: LangfuseTracer | None = None


def get_tracer(settings: Settings) -> LangfuseTracer:
    global _tracer
    if _tracer is None:
        _tracer = LangfuseTracer(settings)
    return _tracer
