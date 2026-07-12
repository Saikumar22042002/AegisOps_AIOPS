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

from ..cache import redis as redis_client
from ..db import session as db
from ..graph_db import neo4j as neo4j_client
from ..integrations.gemini import get_gemini
from ..integrations.servicenow import get_servicenow
from ..schemas.auth import User
from ..security.deps import require_auth
from ..settings import Settings, get_settings
from ..tools.github import get_github
from ..tools.kubernetes import get_kubernetes

router = APIRouter(tags=["integrations"])

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
    gemini = get_gemini(settings)
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
        _row("Gemini", "connected" if gemini.enabled else "not configured"),
    ]
    return {"integrations": rows}


@router.get("/models")
async def list_models(
    _user: User = Depends(require_auth), settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    """U3: the real LLM catalog — exactly the models the backend serves and validates. The
    model menu shows these and only these; anything else is rejected at /chat with a 400."""
    from ..integrations.llm import available_models

    return {"models": available_models(settings)}
