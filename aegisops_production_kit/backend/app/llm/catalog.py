"""Model catalog + capability registry (P1.4 — Redesign/04 §4.3-4.5).

`models.yaml` says what CAN run; the `model_bindings` table (P1.7) says who runs what.
`load()` validates the yaml at import/boot and refuses startup on an invalid catalog —
a mis-declared fallback chain must fail loudly at deploy time, not at 3am mid-run.

Provider configuration is data-driven: each provider block in the yaml names the
`Settings` attribute holding its credential (`settings_field`). Code contains no
per-provider branches — adding a wire family is a yaml entry + an adapter module.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from ..settings import Settings
from .errors import ModelError
from .types import GOVERNED_PURPOSES, PURPOSES

CAPABILITIES: frozenset[str] = frozenset({
    "streaming", "structured_output", "tools_native", "tools_emulated",
    "reasoning", "vision", "prompt_cache", "embeddings",
})

# The adapter modules that exist (04 §4: six target wire families; P1 ships three —
# bedrock/azure_openai are future adapters, litellm ships disabled per ADR-14).
WIRE_FAMILIES: frozenset[str] = frozenset({"google", "anthropic", "openai_compat"})

_YAML = Path(__file__).parent / "config" / "models.yaml"


@dataclass(frozen=True)
class ModelInfo:
    id: str
    provider: str
    capabilities: frozenset[str]
    context_window: int | None = None
    embedding_dim: int | None = None

    @property
    def tools_any(self) -> bool:  # 04 §4.5
        return "tools_native" in self.capabilities or "tools_emulated" in self.capabilities


@dataclass(frozen=True)
class PurposeDefault:
    purpose: str
    model: str
    fallbacks: tuple[str, ...]       # () encodes the explicit `fallbacks: none`
    requires: tuple[str, ...] = ()


@dataclass(frozen=True)
class Catalog:
    providers: dict[str, dict]
    models: dict[str, ModelInfo]
    purposes: dict[str, PurposeDefault]

    def model(self, model_id: str) -> ModelInfo:
        try:
            return self.models[model_id]
        except KeyError:
            raise ModelError(
                "invalid_request",
                f"unknown model {model_id!r}; AegisOps serves: "
                f"{', '.join(sorted(self.models))}") from None

    def provider_configured(self, provider: str, settings: Settings) -> bool:
        """Data-driven: the yaml's `settings_field` names the credential attribute."""
        spec = self.providers.get(provider)
        if not spec:
            return False
        return bool(getattr(settings, spec["settings_field"], ""))

    def transport(self, provider: str, settings: Settings) -> tuple[str, str, str | None]:
        """(wire_family, api_key, base_url) for one provider — everything an adapter
        needs, resolved purely from yaml + Settings (no provider branches in code)."""
        spec = self.providers.get(provider)
        if not spec:
            raise ModelError("invalid_request", f"unknown provider {provider!r}")
        api_key = getattr(settings, spec["settings_field"], "") or ""
        base_url = spec.get("base_url")
        if not base_url and spec.get("base_url_field"):
            base_url = getattr(settings, spec["base_url_field"], "") or None
        return spec.get("wire_family", provider), api_key, base_url

    def configured_models(self, settings: Settings) -> list[ModelInfo]:
        return [m for m in self.models.values()
                if self.provider_configured(m.provider, settings)]

    def selectable(self, model_id: str, purpose: str) -> ModelInfo:
        """A model may serve a purpose only when it carries the purpose's required
        capabilities — the check behind bindings (P1.7) and per-request pins."""
        info = self.model(model_id)
        spec = self.purposes[purpose]
        missing = [c for c in spec.requires if c not in info.capabilities]
        if missing:
            raise ModelError(
                "invalid_request",
                f"model {model_id!r} lacks {missing} required by purpose {purpose!r}")
        return info


def _validate(raw: dict) -> list[str]:
    problems: list[str] = []
    providers = raw.get("providers", {})
    for name, spec in providers.items():
        if not isinstance(spec, dict) or "settings_field" not in spec:
            problems.append(f"provider {name}: must declare settings_field")
            continue
        family = spec.get("wire_family", name)
        if family not in WIRE_FAMILIES:
            problems.append(f"provider {name}: wire_family {family!r} has no adapter "
                            f"(known: {sorted(WIRE_FAMILIES)})")
    models = {m["id"]: m for m in raw.get("models", [])}
    for m in raw.get("models", []):
        if m["provider"] not in providers:
            problems.append(f"model {m['id']}: unknown provider {m['provider']}")
        bad = set(m.get("capabilities", [])) - CAPABILITIES
        if bad:
            problems.append(f"model {m['id']}: unknown capabilities {sorted(bad)}")
    purposes = raw.get("purposes", {})
    for p in PURPOSES:
        if p not in purposes:
            problems.append(f"purpose {p}: no default binding in models.yaml")
    for p, spec in purposes.items():
        if p not in PURPOSES:
            problems.append(f"purpose {p}: not a known purpose")
            continue
        if spec.get("model") not in models:
            problems.append(f"purpose {p}: default model {spec.get('model')!r} not in catalog")
        fb = spec.get("fallbacks")
        if fb is None:
            problems.append(f"purpose {p}: fallback chain must be a list or the explicit "
                            f"string 'none' (07 P1.4)")
        elif fb != "none":
            for f in fb:
                if f not in models:
                    problems.append(f"purpose {p}: fallback {f!r} not in catalog")
            if p in GOVERNED_PURPOSES and fb:
                problems.append(f"purpose {p}: governed purposes never silent-fallback — "
                                f"declare 'none'")
        for c in spec.get("requires", []) or []:
            if c not in CAPABILITIES:
                problems.append(f"purpose {p}: unknown required capability {c!r}")
            elif spec.get("model") in models and c not in set(
                    models[spec["model"]].get("capabilities", [])):
                problems.append(f"purpose {p}: default model {spec['model']} lacks "
                                f"required capability {c!r}")
    return problems


@lru_cache(maxsize=4)
def load(path: str | None = None) -> Catalog:
    raw = yaml.safe_load(Path(path or _YAML).read_text(encoding="utf-8"))
    problems = _validate(raw)
    if problems:
        raise ModelError("invalid_request",
                         "models.yaml invalid: " + " | ".join(problems))
    models = {m["id"]: ModelInfo(
        id=m["id"], provider=m["provider"],
        capabilities=frozenset(m.get("capabilities", [])),
        context_window=m.get("context_window"),
        embedding_dim=m.get("embedding_dim"),
    ) for m in raw["models"]}
    purposes = {p: PurposeDefault(
        purpose=p, model=spec["model"],
        fallbacks=() if spec.get("fallbacks") == "none" else tuple(spec["fallbacks"]),
        requires=tuple(spec.get("requires", []) or ()),
    ) for p, spec in raw["purposes"].items()}
    return Catalog(providers=raw["providers"], models=models, purposes=purposes)


def boot_validate(settings: Settings) -> None:
    """Startup hook (main.py lifespan): load + refuse an invalid catalog; warn when a
    declared provider has no credentials (local keeps booting — parity with today)."""
    import structlog
    log = structlog.get_logger(__name__)
    cat = load()
    unconfigured = [p for p in cat.providers if not cat.provider_configured(p, settings)]
    if unconfigured:
        log.warning("llm.providers_unconfigured", providers=unconfigured)
    log.info("llm.catalog_loaded", models=len(cat.models), purposes=len(cat.purposes),
             providers=list(cat.providers))
