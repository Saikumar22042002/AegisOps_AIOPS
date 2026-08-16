"""Provider adapters — the ONLY modules allowed to import provider SDKs (P1.9).

Each adapter translates canonical contracts (05 §11) to one wire family and back.
Adapters are pure translation + transport: no retries (executor owns them), no
observability (service owns it), no accounting (service owns the ledger calls).

Construction is data-driven: `for_provider()` resolves (wire_family, api_key,
base_url) from the catalog + Settings — a NEW provider on an existing wire family
(e.g. OpenRouter over openai_compat) needs zero code here.
"""

from __future__ import annotations

from ...settings import Settings
from ..catalog import Catalog
from ..errors import ModelError
from .anthropic_ import AnthropicAdapter
from .base import ProviderAdapter
from .google_ import GoogleAdapter
from .openai_compat import OpenAICompatAdapter

_FAMILIES: dict[str, type[ProviderAdapter]] = {
    "google": GoogleAdapter,
    "anthropic": AnthropicAdapter,
    "openai_compat": OpenAICompatAdapter,
}

# Adapter instance cache per (provider, key-fingerprint, base_url) — clients hold
# connection pools; rebuilding per call would leak sockets.
_instances: dict[tuple[str, int, str | None], ProviderAdapter] = {}


def for_provider(provider: str, catalog: Catalog, settings: Settings) -> ProviderAdapter:
    family, api_key, base_url = catalog.transport(provider, settings)
    cls = _FAMILIES.get(family)
    if cls is None:
        raise ModelError("invalid_request",
                         f"wire family {family!r} has no adapter (provider {provider!r})")
    cache_key = (provider, hash(api_key), base_url)
    inst = _instances.get(cache_key)
    if inst is None:
        inst = cls(api_key, base_url)
        _instances[cache_key] = inst
    return inst


def reset_adapter_cache() -> None:
    _instances.clear()


__all__ = ["ProviderAdapter", "for_provider", "reset_adapter_cache",
           "GoogleAdapter", "AnthropicAdapter", "OpenAICompatAdapter"]
