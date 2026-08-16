"""Adapter contract: canonical shapes in, canonical shapes out (05 §11).

Rules every adapter must honor:
- constructed from plain transport config `(api_key, base_url)` — resolved by
  `catalog.transport()` from yaml + Settings; adapters never read Settings;
- the SDK client is created LAZILY on first use (`_make_client`), so an uninstalled
  SDK fails as a typed ModelError at call time, and tests can inject fakes;
- raise only ModelError (never SDK exceptions) — the executor branches on `kind`;
- streams terminate with exactly one `done` (after `usage` + `served_by`) or exactly
  one `error` StreamEvent; adapters never both raise AND emit `error`;
- no retries, no fallbacks, no ledger writes, no tracing — those belong to the
  executor/service layers so every provider gets them identically.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any

from ..errors import ModelError
from ..types import ModelRequest, ModelResponse, StreamEvent


class ProviderAdapter(ABC):
    name: str = "base"   # wire family

    def __init__(self, api_key: str, base_url: str | None = None) -> None:
        self.api_key = api_key or ""
        self.base_url = base_url
        self._client: Any = None

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _require_key(self) -> None:
        if not self.configured:
            raise ModelError("auth_permanent",
                             f"no credentials configured for wire family {self.name!r}",
                             provider=self.name)

    def client(self) -> Any:
        """Lazy SDK client. Tests inject fakes by assigning `_client` directly."""
        if self._client is None:
            try:
                self._client = self._make_client()
            except ImportError as e:
                raise ModelError("unavailable",
                                 f"{self.name} SDK is not installed: {e}",
                                 provider=self.name) from e
        return self._client

    @abstractmethod
    def _make_client(self) -> Any: ...

    @abstractmethod
    async def generate(self, req: ModelRequest, model: str) -> ModelResponse: ...

    @abstractmethod
    def stream(self, req: ModelRequest, model: str) -> AsyncIterator[StreamEvent]: ...

    async def embed(self, texts: list[str], model: str, dim: int) -> list[list[float]]:
        raise ModelError("invalid_request",
                         f"wire family {self.name!r} has no embeddings support",
                         provider=self.name)

    async def ping(self) -> bool:
        """Cheap health probe (P1.7). Default: credentials present."""
        return self.configured


def status_of(e: Exception) -> int | None:
    """SDK-agnostic HTTP status extraction (works on google-genai `code`,
    anthropic/openai `status_code`, and plain fakes)."""
    for attr in ("status_code", "code", "status"):
        v = getattr(e, attr, None)
        if isinstance(v, int):
            return v
    return None
