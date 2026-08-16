"""TRANSITIONAL (P1 → end of P2): the surviving surface of the retired Gemini singleton.

The `GeminiLLM` client, `get_gemini()` singleton and `usage_of()` were deleted at the end
of P1 (07 removal table) — every model call now flows through `app/llm` (canonical
contracts → router → resilient executor → adapters), and the google-genai SDK import
lives only in `app/llm/adapters/google_.py` (P1.9 boundary).

What remains here, because its CONSUMERS still import from this path:
- `GeminiError` — the exception the agent nodes (general/knowledge/sre) catch around
  streaming answers; the `agents/llm.py` shim raises it for configuration problems.
- `set_run_model`/`get_run_model` — the U3 per-run model pin (contextvar, never mutated
  onto shared state so concurrent runs stay isolated). `api/chat.py` sets it at
  admission; the shim passes it to route resolution as the per-request pin.

Removal condition (Redesign/11 T-01): dies with the shim at the end of P2, when callers
import these from `app/llm` directly.
"""

from __future__ import annotations

import contextvars

_run_model: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "aegisops_run_model", default=None)


def set_run_model(model: str | None) -> None:
    """Bind the model for the current run's asyncio context (call once per run driver)."""
    _run_model.set(model)


def get_run_model() -> str | None:
    return _run_model.get()


class GeminiError(Exception):
    """Historical name, kept for its catchers; semantically 'LLM unavailable/misused'."""
