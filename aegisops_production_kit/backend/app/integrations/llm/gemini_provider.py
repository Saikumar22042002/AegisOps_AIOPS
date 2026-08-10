"""GeminiProvider — the one LLM provider AegisOps ships with today (U3).

Thin adapter over the existing GeminiLLM singleton (reuses its google-genai client, retry, and
Langfuse instrumentation). It advertises an honest catalog: the configured default model plus
the fallback ids the resolver already knows how to substitute when the key can't list the
default — all Google Gemini, all served by this one provider.
"""

from __future__ import annotations

from ...settings import Settings
from ..gemini import GeminiLLM, get_gemini


class GeminiProvider:
    name = "google-gemini"

    def __init__(self, settings: Settings) -> None:
        self._llm: GeminiLLM = get_gemini(settings)
        self._default = settings.gemini_model
        # De-duped, order-preserving: default first, then the resolver's known-good fallbacks.
        catalog: list[str] = []
        for m in (settings.gemini_model, "gemini-3.5-flash", "gemini-flash-latest",
                  "gemini-2.5-flash"):
            if m and m not in catalog:
                catalog.append(m)
        self._models = catalog

    @property
    def enabled(self) -> bool:
        return self._llm.enabled

    @property
    def models(self) -> list[str]:
        return list(self._models)

    @property
    def default_model(self) -> str:
        return self._default

    def serves(self, model: str) -> bool:
        return model in self._models

    # P0/D7: the astream/agenerate/aembed passthroughs were deleted — they had ZERO callers
    # (the validate-only seam dispatches nothing; all inference goes through the GeminiLLM
    # singleton). This class is the honest catalog/validation surface for /models and /chat
    # admission until the P1 provider layer replaces the whole seam.
