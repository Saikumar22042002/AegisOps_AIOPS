"""Liveness + readiness endpoints.

`/healthz`  — process is up (used by container HEALTHCHECK / orchestrator liveness).
`/readyz`   — every backing dependency is reachable. Core datastores (Postgres,
              Redis, Neo4j) gate readiness; auth/observability deps are reported as
              informational so the API can still come up and surface their state.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from fastapi import APIRouter, Response, status

from ..cache import redis as redis_client
from ..db import session as db
from ..graph_db import neo4j as neo4j_client
from ..logging_conf import get_logger
from ..metrics import DEP_UP
from ..settings import get_settings

log = get_logger(__name__)
router = APIRouter(tags=["ops"])

# Dependencies whose failure makes the API "not ready".
CORE_DEPS = {"postgres", "redis", "neo4j"}


async def _check(name: str, coro: Any) -> dict[str, Any]:
    try:
        ok = await asyncio.wait_for(coro, timeout=5.0)
        DEP_UP.labels(dependency=name).set(1 if ok else 0)
        return {"name": name, "status": "up" if ok else "down"}
    except Exception as exc:  # noqa: BLE001 - report any failure as down, with detail
        DEP_UP.labels(dependency=name).set(0)
        return {"name": name, "status": "down", "detail": str(exc)[:200]}


async def _http_ok(name: str, url: str) -> dict[str, Any]:
    async def _do() -> bool:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url)
            return resp.status_code < 500

    return await _check(name, _do())


async def check_dependencies() -> dict[str, Any]:
    settings = get_settings()
    results = await asyncio.gather(
        _check("postgres", db.ping()),
        _check("redis", redis_client.ping()),
        _check("neo4j", neo4j_client.ping()),
        _http_ok("keycloak", f"{settings.keycloak_realm_url}/.well-known/openid-configuration"),
        _http_ok("langfuse", f"{settings.langfuse_host.rstrip('/')}/api/public/health"),
        _http_ok("prometheus", f"{settings.prometheus_url.rstrip('/')}/-/healthy"),
    )
    deps = {r["name"]: r for r in results}
    ready = all(deps[d]["status"] == "up" for d in CORE_DEPS)
    return {"ready": ready, "dependencies": deps}


@router.get("/healthz")
async def healthz() -> dict[str, Any]:
    # P0.5 (D9/F-9): the active governance posture is visible wherever liveness is —
    # a weakened or changed flag can never again drift silently in an `.env`.
    from ..security.governance_stamp import governance_stamp

    return {"status": "ok", "governance": governance_stamp(get_settings())}


@router.get("/readyz")
async def readyz(response: Response) -> dict[str, Any]:
    result = await check_dependencies()
    # P5 hardening: the config preflight is reported alongside dependency reachability, so an
    # operator sees production-posture findings (event-bus/metrics-auth/tenancy/broker) on the
    # same readiness surface. It does not itself flip readiness (the P0 startup refusals are
    # the hard gate); a `blocked` finding is surfaced for visibility.
    from .. import preflight
    from ..settings import get_settings
    result["preflight"] = preflight.run(get_settings()).as_dict()
    if not result["ready"]:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        log.warning("readyz.not_ready", dependencies=result["dependencies"])
    return result
