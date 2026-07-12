"""LLM provider seam (U3).

A minimal, honest abstraction over the reasoning engine. Today AegisOps runs exactly one
provider — Google Gemini — so the registry has one entry; the point of the seam is that (a)
model selection is *real* (the model the operator picks is the model the run uses) and (b) an
unknown model fails loudly with a clear error instead of being silently ignored. Add a new
provider by implementing this protocol and registering it in `registry._providers`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable


class UnknownModelError(ValueError):
    """The requested model is not served by any configured provider."""


@runtime_checkable
class LLMProvider(Protocol):
    """What the agents need from a reasoning backend. Kept intentionally small."""

    name: str

    @property
    def enabled(self) -> bool:
        """True when the provider is configured (has credentials)."""

    @property
    def models(self) -> list[str]:
        """The model ids this provider serves, most-preferred first."""

    @property
    def default_model(self) -> str:
        """The model used when the caller does not pick one."""

    def serves(self, model: str) -> bool:
        """True when `model` is one of this provider's ids."""

    async def astream(self, system: str | None, contents: Any, tools: list | None = None,
                      model: str | None = None) -> AsyncIterator[Any]:
        """Stream raw response chunks for `model` (or the default)."""

    async def agenerate(self, system: str | None, contents: Any, tools: list | None = None,
                        model: str | None = None) -> Any:
        """One-shot generation for `model` (or the default)."""

    async def aembed(self, texts: list[str]) -> list[list[float]]:
        """Embed texts (uses the provider's embedding model, not the chat model)."""
