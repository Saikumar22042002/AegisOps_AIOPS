"""Org-level model bindings (P1.7 — Redesign/04 §4.4): who runs what.

The read path is registered into the router at startup (`register()`), so `router.py`
never references this table directly — resolution order stays pin → binding → default.
The write path validates against the catalog (capability requirements, configured
provider), refuses governed purposes for non-platform admins implicitly via the API
layer's RBAC, and lands an audit row for every change (governance: bindings move model
traffic — that is a governed act).
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import delete, select

from ..db.models import ModelBinding
from ..db.repositories import AuditRepo
from ..db.session import session_scope
from ..settings import Settings
from . import catalog as catalog_mod
from . import router
from .errors import ModelError
from .types import PURPOSES

log = structlog.get_logger(__name__)

# (org_id, purpose) -> (model|"", expires_at). Bindings change rarely; 60s keeps
# resolution off the DB hot path. Invalidated on every write.
_cache: dict[tuple[str, str], tuple[str, float]] = {}
_TTL_S = 60.0


def _invalidate() -> None:
    _cache.clear()


async def resolve(org_id: str, purpose: str) -> str | None:
    """The router's binding hook: the org's model for a purpose, or None. A `failed`
    eval_state never routes (04 §4.4). Lookup failures return None — never fail a call."""
    key = (org_id, purpose)
    hit = _cache.get(key)
    now = time.monotonic()
    if hit and hit[1] > now:
        return hit[0] or None
    model = ""
    async with session_scope() as s:
        row = (await s.execute(select(ModelBinding).where(
            ModelBinding.org_id == uuid.UUID(org_id),
            ModelBinding.purpose == purpose))).scalar_one_or_none()
        if row and row.eval_state != "failed":
            model = row.model
    _cache[key] = (model, now + _TTL_S)
    return model or None


def register() -> None:
    """Startup hook (main.py lifespan): wire the router's binding resolution."""
    router.set_binding_resolver(resolve)


async def list_bindings(org_id: str, settings: Settings) -> list[dict[str, Any]]:
    """Every purpose with its effective routing: bound model or catalog default."""
    cat = catalog_mod.load()
    async with session_scope() as s:
        rows = {r.purpose: r for r in (await s.execute(select(ModelBinding).where(
            ModelBinding.org_id == uuid.UUID(org_id)))).scalars()}
    out = []
    for p in PURPOSES:
        row = rows.get(p)
        default = cat.purposes[p].model
        out.append({
            "purpose": p,
            "governed": p in {"router", "planner", "loop.main", "judge"},
            "default_model": default,
            "bound_model": row.model if row else None,
            "effective_model": (row.model if row and row.eval_state != "failed"
                                else default),
            "eval_state": row.eval_state if row else None,
            "updated_by": row.updated_by if row else None,
            "reason": row.reason if row else None,
        })
    return out


async def set_binding(org_id: str, purpose: str, model: str, *, actor: str,
                      reason: str | None, settings: Settings) -> dict[str, Any]:
    """Bind (org, purpose) → model. Catalog-validated: the model must exist, carry the
    purpose's required capabilities, and its provider must be configured. eval_state
    starts `pending` — the eval gate (or an explicit waiver) promotes it."""
    if purpose not in PURPOSES:
        raise ModelError("invalid_request", f"unknown purpose {purpose!r}")
    cat = catalog_mod.load()
    info = cat.selectable(model, purpose)          # capability check (raises typed)
    if not cat.provider_configured(info.provider, settings):
        raise ModelError("invalid_request",
                         f"provider {info.provider!r} has no credentials configured — "
                         f"binding would route every {purpose!r} call into a dead end")
    async with session_scope() as s:
        row = (await s.execute(select(ModelBinding).where(
            ModelBinding.org_id == uuid.UUID(org_id),
            ModelBinding.purpose == purpose))).scalar_one_or_none()
        if row is None:
            row = ModelBinding(org_id=uuid.UUID(org_id), purpose=purpose, model=model)
            s.add(row)
        row.model = model
        row.eval_state = "pending"
        row.updated_by = actor
        row.reason = reason
        row.updated_at = datetime.now(UTC)
        await AuditRepo.log(s, org_id=uuid.UUID(org_id), actor=actor,
                            action="model_binding.set", target=f"{purpose}→{model}",
                            detail={"reason": reason, "provider": info.provider})
    _invalidate()
    log.info("llm.binding_set", purpose=purpose, model=model, actor=actor)
    return {"purpose": purpose, "model": model, "eval_state": "pending"}


async def clear_binding(org_id: str, purpose: str, *, actor: str,
                        settings: Settings) -> None:
    async with session_scope() as s:
        await s.execute(delete(ModelBinding).where(
            ModelBinding.org_id == uuid.UUID(org_id),
            ModelBinding.purpose == purpose))
        await AuditRepo.log(s, org_id=uuid.UUID(org_id), actor=actor,
                            action="model_binding.clear", target=purpose, detail=None)
    _invalidate()
    log.info("llm.binding_cleared", purpose=purpose, actor=actor)
