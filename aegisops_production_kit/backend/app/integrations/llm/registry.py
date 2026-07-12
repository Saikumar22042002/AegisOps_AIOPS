"""Provider registry + model resolution (U3).

`get_provider(settings, model)` is the single choke point the API uses to turn an operator's
model choice into a concrete provider. `None`/empty → the default provider and its default
model; a known id → that provider bound to that id; anything else → `UnknownModelError` with a
message listing what we actually serve (never a silent fallback to the default).
"""

from __future__ import annotations

from ...settings import Settings
from .base import LLMProvider, UnknownModelError
from .gemini_provider import GeminiProvider


def _providers(settings: Settings) -> list[LLMProvider]:
    # The whole registry. One provider today; add more here as they become real.
    return [GeminiProvider(settings)]


def available_models(settings: Settings) -> list[dict]:
    """The honest catalog, for the /models endpoint and the model menu."""
    out: list[dict] = []
    for p in _providers(settings):
        for m in p.models:
            out.append({"id": m, "provider": p.name, "enabled": p.enabled,
                        "default": m == p.default_model})
    return out


def get_provider(settings: Settings, model: str | None = None) -> tuple[LLMProvider, str]:
    """Resolve (provider, model_id). Raises UnknownModelError for a model we don't serve."""
    providers = _providers(settings)
    if model is None or not str(model).strip():
        default = providers[0]
        return default, default.default_model
    for p in providers:
        if p.serves(model):
            return p, model
    known = [m for p in providers for m in p.models]
    raise UnknownModelError(
        f"Unknown model '{model}'. AegisOps serves: {', '.join(known)}.")
