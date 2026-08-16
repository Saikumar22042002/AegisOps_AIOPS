"""RoutePlan resolution (P1.6 — Redesign/04 §4.4).

Resolution order, first match wins:
  1. per-request pin (run creation only; NON-governed purposes only — a user model
     choice must never change router/planner/loop.main/judge behavior),
  2. `model_bindings` row for (org, purpose) — P1.7, eval-gated, injected via
     `set_binding_resolver()` so this module never references a table that does
     not exist yet,
  3. `models.yaml` purpose default.

Deterministic and policy-controlled: no reasoning, no scoring, no learning — that is
future harness territory and explicitly NOT P1.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import structlog

from ..settings import Settings
from . import catalog
from .types import GOVERNED_PURPOSES, RoutePlan

log = structlog.get_logger(__name__)

# P1.7 registers the real (org_id, purpose) -> model|None lookup here. Until then
# resolution is pin → default, and nothing references a not-yet-migrated table.
_binding_resolver: Callable[[str, str], Awaitable[str | None]] | None = None


def set_binding_resolver(fn: Callable[[str, str], Awaitable[str | None]] | None) -> None:
    global _binding_resolver
    _binding_resolver = fn


def _plan(purpose: str, model_id: str, pinned_by: str, cat: catalog.Catalog,
          settings: Settings) -> RoutePlan:
    info = cat.selectable(model_id, purpose)
    spec = cat.purposes[purpose]
    # Fallback targets must be selectable AND configured — an unconfigured provider is
    # never a hop (visible in the plan, not discovered mid-failover).
    fallbacks = [(cat.models[f].provider, f) for f in spec.fallbacks
                 if f != model_id and cat.provider_configured(cat.models[f].provider,
                                                              settings)]
    return RoutePlan(purpose=purpose, provider=info.provider, model=model_id,
                     fallbacks=fallbacks, pinned_by=pinned_by)  # type: ignore[arg-type]


async def resolve(settings: Settings, purpose: str, *, requested_model: str | None = None,
                  org_id: str | None = None) -> RoutePlan:
    cat = catalog.load()
    if requested_model and purpose not in GOVERNED_PURPOSES:
        return _plan(purpose, requested_model, "request", cat, settings)
    if requested_model and purpose in GOVERNED_PURPOSES:
        # Honest, visible refusal of the pin — never silent behavior drift (04 §4.4).
        log.info("llm.pin_ignored_governed_purpose", purpose=purpose,
                 requested_model=requested_model)
    if org_id and _binding_resolver is not None:
        try:
            bound = await _binding_resolver(org_id, purpose)
        except Exception as exc:  # noqa: BLE001 — resolution must never fail a call
            log.warning("llm.binding_lookup_failed", purpose=purpose, error=str(exc))
            bound = None
        if bound:
            return _plan(purpose, bound, "binding", cat, settings)
    return _plan(purpose, cat.purposes[purpose].model, "default", cat, settings)
