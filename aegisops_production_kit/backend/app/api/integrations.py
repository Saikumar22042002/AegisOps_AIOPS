"""GET /integrations — live health of every connected service.

Returns the design's integrations-grid shape (name/cat/mark/color/status/statusColor) with
status computed live: datastores + observability probed over the network, ServiceNow/GitHub
probed when configured, and Gemini/Terraform/Ansible/Kubernetes reported from config + binary
availability. RBAC: any authenticated user may read.
"""

from __future__ import annotations

import asyncio
import shutil
from typing import Any

import httpx
from fastapi import APIRouter, Depends

from fastapi import HTTPException, status
from pydantic import BaseModel, Field

from ..cache import redis as redis_client
from ..db import repositories as repo
from ..db import session as db
from ..db.session import session_scope
from ..graph_db import neo4j as neo4j_client
from ..llm import bindings as llm_bindings
from ..llm import catalog as llm_catalog
from ..llm.adapters import for_provider
from ..llm.errors import ModelError as LlmModelError
from ..integrations.servicenow import get_servicenow
from ..schemas.auth import User
from ..security.deps import require_auth
from ..settings import Settings, get_settings
from ..tools.github import get_github
from ..tools.kubernetes import get_kubernetes

router = APIRouter(tags=["integrations"])

_BINDING_ADMIN_ROLES = {"platform-admin", "org-admin"}


def _require_binding_admin(user: User) -> None:
    """Bindings move an org's model traffic — a governed act, gated to org admins
    (stricter than approver: cloud-architect approves plans, not routing policy)."""
    if not any(r in _BINDING_ADMIN_ROLES for r in (user.roles or [])):
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "Model bindings require Org Admin or Platform Admin.")

# Static design metadata (mark/color/cat) keyed by service name — status is live.
_META: dict[str, dict[str, str]] = {
    "Keycloak": {"cat": "Identity · SSO/SAML", "mark": "K", "color": "var(--accent-3)"},
    "LangGraph": {"cat": "Agent orchestration", "mark": "LG", "color": "var(--accent-2)"},
    "Langfuse": {"cat": "LLM observability", "mark": "Lf", "color": "var(--cyan)"},
    "OpenTelemetry": {"cat": "Traces · metrics", "mark": "OT", "color": "var(--violet)"},
    "Prometheus": {"cat": "Metrics", "mark": "Pr", "color": "var(--amber)"},
    "Grafana": {"cat": "Dashboards", "mark": "Gf", "color": "var(--amber)"},
    "PostgreSQL": {"cat": "Primary datastore", "mark": "Pg", "color": "var(--cyan)"},
    "Redis": {"cat": "Cache · queues", "mark": "Rd", "color": "var(--red)"},
    "Neo4j": {"cat": "Context graph", "mark": "N4", "color": "var(--green)"},
    "Terraform": {"cat": "Provisioning", "mark": "Tf", "color": "var(--accent-2)"},
    "Ansible": {"cat": "Configuration", "mark": "An", "color": "var(--red)"},
    "GitHub": {"cat": "SCM · Actions", "mark": "Gh", "color": "var(--text-2)"},
    "ServiceNow": {"cat": "ITSM · SR/CR/INC", "mark": "SN", "color": "var(--green)"},
    "Gemini": {"cat": "LLM provider", "mark": "Gm", "color": "var(--cyan)"},
}


async def _http_status(url: str, label: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            r = await client.get(url)
        return label if r.status_code < 500 else "down"
    except Exception:  # noqa: BLE001
        return "down"


async def _safe(coro) -> bool:
    try:
        return bool(await asyncio.wait_for(coro, timeout=4.0))
    except Exception:  # noqa: BLE001
        return False


def _row(name: str, status: str) -> dict[str, Any]:
    meta = _META.get(name, {"cat": "", "mark": name[:2], "color": "var(--text-2)"})
    down = status in {"down", "not configured", "unavailable"}
    return {"name": name, "cat": meta["cat"], "mark": meta["mark"], "color": meta["color"],
            "status": status, "statusColor": "var(--red)" if down else "var(--green)"}


@router.get("/integrations")
async def list_integrations(
    _user: User = Depends(require_auth), settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    snow = get_servicenow(settings)
    github = get_github(settings)
    k8s = get_kubernetes(settings)

    (pg, rd, n4, kc, lf, prom, graf) = await asyncio.gather(
        _safe(db.ping()),
        _safe(redis_client.ping()),
        _safe(neo4j_client.ping()),
        _http_status(f"{settings.keycloak_realm_url}/.well-known/openid-configuration", "connected"),
        _http_status(f"{settings.langfuse_host.rstrip('/')}/api/public/health", "tracing"),
        _http_status(f"{settings.prometheus_url.rstrip('/')}/-/healthy", "scraping"),
        _http_status(f"{settings.grafana_url.rstrip('/')}/api/health", "connected"),
    )

    snow_status = ("syncing" if await _safe(snow.ping()) else "down") if snow.enabled else "not configured"
    gh_status = ("connected" if await _safe(github.ping()) else "down") if github.enabled else "not configured"

    rows = [
        _row("Keycloak", kc),
        _row("LangGraph", "active"),  # in-process orchestration
        _row("Langfuse", lf),
        _row("OpenTelemetry", "connected" if settings.otel_exporter_otlp_endpoint else "not configured"),
        _row("Prometheus", prom),
        _row("Grafana", graf),
        _row("PostgreSQL", "healthy" if pg else "down"),
        _row("Redis", "healthy" if rd else "down"),
        _row("Neo4j", "connected" if n4 else "down"),
        _row("Terraform", "connected" if shutil.which(settings.terraform_bin) else "unavailable"),
        _row("Ansible", "connected" if shutil.which(settings.ansible_bin) else "unavailable"),
        _row("GitHub", gh_status),
        _row("ServiceNow", snow_status),
    ]
    # One honest row per LLM provider in the catalog (multi-provider substrate, P1):
    cat = llm_catalog.load()
    rows += [_row(f"LLM: {name}",
                  "connected" if cat.provider_configured(name, settings)
                  else "not configured")
             for name in cat.providers]
    return {"integrations": rows}


@router.get("/models")
async def list_models(
    _user: User = Depends(require_auth), settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    """U3: the real LLM catalog — exactly the models the backend serves and validates. The
    model menu shows these and only these; anything else is rejected at /chat with a 400."""
    cat = llm_catalog.load()
    default = cat.purposes["general"].model
    return {"models": [
        {"id": m.id, "provider": m.provider,
         "enabled": cat.provider_configured(m.provider, settings),
         "default": m.id == default}
        for m in cat.models.values() if "embeddings" not in m.capabilities]}


@router.get("/capabilities")
async def list_capabilities(
    _user: User = Depends(require_auth), settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    """P4 capability packs: the provider-neutral view of what AegisOps can do across
    AWS/Azure/GCP/K8s/GitHub — read/mutation families per pack, whether the provider is
    configured, and the approved templates/day-2 verbs it owns. This is the multi-cloud
    parity matrix the UI renders; an unconfigured provider lists honestly (no fake support)."""
    from ..packs import registry as pack_registry
    from ..security.governance_stamp import governance_stamp
    g = governance_stamp(settings)
    return {"packs": pack_registry.capability_catalog(settings),
            "packs_enabled": getattr(settings, "aegisops_capability_packs", "off") == "on",
            # P5 posture — the operator-visible governance flags for this deployment.
            "posture": {"permission_mode": g["permission_mode"],
                        "credential_broker": g["credential_broker"],
                        "durable_engine": g["durable_engine"],
                        "approval_model": g["approval_model"]}}


@router.get("/models/providers")
async def list_providers(
    _user: User = Depends(require_auth), settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    """P1.7 live provider health: configured (credentials present) + a cheap adapter
    probe. Unconfigured providers list honestly rather than disappearing."""
    cat = llm_catalog.load()
    out = []
    for name in cat.providers:
        configured = cat.provider_configured(name, settings)
        healthy = False
        if configured:
            try:
                healthy = await for_provider(name, cat, settings).ping()
            except Exception:  # noqa: BLE001 — a probe failure is a health answer
                healthy = False
        out.append({"name": name, "configured": configured, "healthy": healthy,
                    "models": [m.id for m in cat.models.values() if m.provider == name]})
    return {"providers": out}


class BindingRequest(BaseModel):
    model: str = Field(min_length=1, max_length=120)
    reason: str | None = Field(default=None, max_length=500)


@router.get("/models/bindings")
async def get_bindings(
    user: User = Depends(require_auth), settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    """Every purpose with its effective routing (default vs org-bound). Read is open to
    any authenticated user — routing posture is governance-visible, like /healthz."""
    async with session_scope() as s:
        org = await repo.org_for(s, user)
        org_id = str(org.id)
    return {"bindings": await llm_bindings.list_bindings(org_id, settings)}


@router.put("/models/bindings/{purpose}")
async def put_binding(
    purpose: str, body: BindingRequest,
    user: User = Depends(require_auth), settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    _require_binding_admin(user)
    async with session_scope() as s:
        org = await repo.org_for(s, user)
        org_id = str(org.id)
    try:
        return await llm_bindings.set_binding(org_id, purpose, body.model,
                                              actor=user.username or user.sub,
                                              reason=body.reason, settings=settings)
    except LlmModelError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None


@router.delete("/models/bindings/{purpose}")
async def delete_binding(
    purpose: str,
    user: User = Depends(require_auth), settings: Settings = Depends(get_settings)
) -> dict[str, str]:
    _require_binding_admin(user)
    async with session_scope() as s:
        org = await repo.org_for(s, user)
        org_id = str(org.id)
    await llm_bindings.clear_binding(org_id, purpose, actor=user.username or user.sub,
                                     settings=settings)
    return {"status": "cleared", "purpose": purpose}
