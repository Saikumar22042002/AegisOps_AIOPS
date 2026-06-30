"""Structured JSON logging with correlation ids.

A contextvar carries correlation ids (trace_id, context_id, session_id, run_id) so
every log line emitted while handling a request/graph step is automatically tagged.
Secrets are never logged here; redaction of payloads lives in `security/redaction.py`.
"""

from __future__ import annotations

import logging
import sys
from contextvars import ContextVar
from typing import Any

import structlog

# Per-request / per-run correlation context. Set in middleware and graph nodes.
_correlation: ContextVar[dict[str, str]] = ContextVar("correlation", default={})


def bind_correlation(**ids: str) -> None:
    """Merge ids (trace_id, context_id, session_id, run_id, agent, step) into context."""
    current = dict(_correlation.get())
    current.update({k: v for k, v in ids.items() if v is not None})
    _correlation.set(current)


def clear_correlation() -> None:
    _correlation.set({})


def get_correlation() -> dict[str, str]:
    return dict(_correlation.get())


def _inject_correlation(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    for k, v in _correlation.get().items():
        event_dict.setdefault(k, v)
    return event_dict


def configure_logging(level: str = "INFO") -> None:
    """Configure structlog to emit JSON to stdout, routing stdlib logs through it too."""
    log_level = getattr(logging, level.upper(), logging.INFO)

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        _inject_correlation,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=[*shared_processors, structlog.processors.JSONRenderer()],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    # Route stdlib logging (uvicorn, sqlalchemy, etc.) through structlog's JSON renderer.
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processor=structlog.processors.JSONRenderer(),
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(log_level)

    for noisy in ("uvicorn.access", "uvicorn.error", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(max(log_level, logging.INFO))


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
