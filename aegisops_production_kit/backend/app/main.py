"""AegisOps FastAPI application.

Wires logging, OpenTelemetry, Prometheus, the datastore clients, correlation-id +
metrics middleware, rate limiting, and graceful shutdown. Feature routers (auth, chat,
sessions, approvals, artifacts, modules, integrations, knowledge, console) are mounted
as they land in later milestones.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from . import __version__
from .agents.checkpointer import close_checkpointer, init_checkpointer
from .agents.graph import init_graph
from .api import (
    artifacts,
    auth,
    chat,
    gateways,
    health,
    integrations,
    knowledge,
    modules,
    sessions,
)
from .cache import redis as redis_client
from .db import session as db
from .graph_db import neo4j as neo4j_client
from .logging_conf import bind_correlation, clear_correlation, configure_logging, get_logger
from .metrics import API_REQUEST_DURATION, API_REQUESTS, REGISTRY
from .otel import setup_otel, shutdown_otel
from .ratelimit import limiter
from .settings import get_settings

log = get_logger(__name__)
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(settings.log_level)
    # P0 Redis policy: production/multi-replica coordination REQUIRES Redis. In-memory
    # coordination is an explicit dev/test mode — there is NO silent non-local fallback:
    # a non-local process with a memory bus, or with Redis unreachable, refuses to start
    # rather than silently losing shared-coordination guarantees.
    if settings.app_env != "local" and settings.aegisops_event_bus == "memory":
        raise RuntimeError(
            "AEGISOPS_EVENT_BUS=memory is a dev-only mode; app_env="
            f"{settings.app_env!r} requires Redis coordination (set AEGISOPS_EVENT_BUS=redis).")
    # P1.4/P1.7: an invalid model catalog refuses startup in EVERY env (it is a packaging
    # bug, not an environment condition); org bindings are wired into route resolution.
    from .llm import bindings as llm_bindings
    from .llm import catalog as llm_catalog_boot
    llm_catalog_boot.boot_validate(settings)
    llm_bindings.register()
    # P5 hardening + Prompt 4 (2026-08-17): the production-config preflight is now a HARD
    # startup gate off-local — a `block` finding refuses to serve, exactly like the P0
    # event-bus/Redis refusals above. Findings carry no secret values (KEY=PRESENT form),
    # so the refusal message is safe to raise. Local dev keeps booting with warnings.
    from . import preflight as _preflight
    _report = _preflight.run(settings)
    for _f in _report.findings:
        if _f.severity != "ok":
            log.warning("preflight.finding", check=_f.check, severity=_f.severity,
                        detail=_f.detail)
    if _report.blocked and settings.app_env != "local":
        _blocks = "; ".join(f"{f.check}: {f.detail}" for f in _report.findings
                            if f.severity == "block")
        raise RuntimeError(f"production preflight refused startup — {_blocks}")
    setup_otel(settings)
    db.init_engine(settings)
    redis_client.init_redis(settings)
    if settings.aegisops_event_bus == "redis":
        try:
            redis_ok = await redis_client.ping()
        except Exception:  # noqa: BLE001
            redis_ok = False
        if not redis_ok:
            if settings.app_env != "local":
                raise RuntimeError(
                    "Redis coordination is required (AEGISOPS_EVENT_BUS=redis) but Redis "
                    "is unreachable — refusing to start rather than degrade silently.")
            log.warning("startup.redis_unreachable",
                        detail="dev posture: continuing; coordination features will error loudly")
    neo4j_client.init_neo4j(settings)
    # D3: world-model schema constraints (idempotent; best-effort — Neo4j down degrades the
    # graph features, never blocks startup).
    try:
        from .graph_db import world_model
        await world_model.ensure_schema()
    except Exception as exc:  # noqa: BLE001
        log.warning("startup.world_model_schema_failed", error=str(exc))
    # LangGraph durable checkpointer + compiled multi-agent graph. If the DB is unreachable
    # the API still starts (degraded) and /readyz reports it; chat then errors loudly rather
    # than silently faking data.
    try:
        checkpointer = await init_checkpointer(settings)
        init_graph(checkpointer)
    except Exception as exc:  # noqa: BLE001
        log.error("startup.graph_init_failed", error=str(exc))
    # O2: verify the Langfuse keys belong to the expected project (loud warning otherwise).
    from .integrations.langfuse_client import assert_project
    await assert_project(settings)
    # MPP: re-register promoted modules from the DB (the runtime library is in-memory).
    try:
        from .agents.module_pipeline import rehydrate_promoted
        await rehydrate_promoted(settings)
    except Exception as exc:  # noqa: BLE001 — a failed rehydrate degrades, never blocks startup
        log.warning("startup.mpp_rehydrate_failed", error=str(exc))
    # P0 worker foundation (F-18): background responsibilities run only in processes whose
    # role owns them — under the api+api-b posture exactly ONE process sweeps. Role gates
    # STARTUP ownership only; it introduces no queues/schedulers (those are P3).
    _owns_background = settings.aegisops_role in ("all", "worker")
    # B3: periodic stranded-run reconciler (recovers runs abandoned by a crashed worker).
    # Gated so no background loop auto-starts in a test lifespan (AEGISOPS_RECONCILER=off).
    if settings.aegisops_reconciler == "on" and _owns_background:
        from .agents.reconciler import get_reconciler
        try:
            await get_reconciler().start()
        except Exception as exc:  # noqa: BLE001
            log.error("startup.reconciler_failed", error=str(exc))
    else:
        log.info("reconciler.disabled",
                 reason=("role" if settings.aegisops_reconciler == "on" else "flag"),
                 role=settings.aegisops_role)
    # GW-1: messaging gateways. Long-polling, so no public URL and no inbound port. Gated by
    # AEGISOPS_TELEGRAM (default off) and never able to break startup — `start_in_background`
    # catches everything and returns False rather than raising (waku's contract).
    # P0: gateway pollers are a background responsibility → role-gated like the reconciler.
    if _owns_background:
        try:
            from .gateways.telegram.poller import start_in_background as start_telegram
            if await start_telegram(settings):
                log.info("startup.telegram_gateway", detail="listening (long-poll)")
        except Exception as exc:  # noqa: BLE001 — a gateway must never take the API down
            log.error("startup.telegram_failed", error=str(exc))
    log.info("app.startup", version=__version__, env=settings.app_env)
    try:
        yield
    finally:
        # Graceful shutdown. B3: stop the reconciler, then B2: drain in-flight runs (cancel +
        # persist failed) while the datastores are still open, then close everything.
        from .agents.reconciler import get_reconciler
        from .agents.supervisor import get_supervisor
        try:
            # GW-1: stop accepting new channel turns before draining runs, so a message
            # arriving mid-shutdown doesn't start a run we are about to cancel.
            from .gateways.telegram.poller import stop_background as stop_telegram
            await stop_telegram()
        except Exception as exc:  # noqa: BLE001
            log.warning("shutdown.telegram_stop_failed", error=str(exc))
        try:
            await get_reconciler().stop()
            await get_supervisor().drain()
        except Exception as exc:  # noqa: BLE001
            log.error("shutdown.drain_failed", error=str(exc))
        await close_checkpointer()
        await redis_client.close_redis()
        await neo4j_client.close_neo4j()
        await db.dispose_engine()
        shutdown_otel()
        log.info("app.shutdown")


class CorrelationMiddleware(BaseHTTPMiddleware):
    """Assign/propagate a request id, bind it to logs, and record request metrics."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        clear_correlation()
        bind_correlation(request_id=request_id)
        # Route template (e.g. /runs/{runId}) keeps metric cardinality bounded.
        route = request.scope.get("route")
        path_label = getattr(route, "path", request.url.path)
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            API_REQUESTS.labels(request.method, request.url.path, "500").inc()
            log.exception("request.unhandled", method=request.method, path=request.url.path)
            raise
        finally:
            clear_correlation()
        elapsed = time.perf_counter() - start
        API_REQUEST_DURATION.labels(request.method, path_label).observe(elapsed)
        API_REQUESTS.labels(request.method, path_label, str(response.status_code)).inc()
        response.headers["x-request-id"] = request_id
        return response


def create_app() -> FastAPI:
    configure_logging(settings.log_level)

    app = FastAPI(
        title="AegisOps API",
        version=__version__,
        description="Agentic AIOps platform — CloudOps · DevOps · SRE.",
        lifespan=lifespan,
    )

    app.state.limiter = limiter

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["x-request-id"],
    )
    app.add_middleware(CorrelationMiddleware)
    app.add_middleware(SlowAPIMiddleware)  # enforces the per-IP default rate limit

    @app.exception_handler(RateLimitExceeded)
    async def _rate_limit_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
        return JSONResponse(
            status_code=429,
            content={"message": "Rate limit exceeded", "code": "rate_limited", "retriable": True},
        )

    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(integrations.router)
    app.include_router(chat.router)
    app.include_router(sessions.router)
    app.include_router(artifacts.router)
    app.include_router(modules.router)
    app.include_router(knowledge.router)
    app.include_router(gateways.router)

    @app.get("/metrics", include_in_schema=False)
    async def metrics(request: Request) -> Response:
        # P0/F-16: /metrics exposed run counts, envs, and API path cardinality to anyone
        # who could reach the port. Posture: token set → bearer required; token unset →
        # open ONLY in app_env=local (keeps the compose Prometheus scrape working);
        # unset + non-local → 403 (fail secure, with an actionable detail).
        token = settings.aegisops_metrics_token
        if token:
            supplied = (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
            if supplied != token:
                return PlainTextResponse("metrics: invalid or missing bearer token",
                                         status_code=401)
        elif settings.app_env != "local":
            return PlainTextResponse(
                "metrics: set AEGISOPS_METRICS_TOKEN (required outside app_env=local)",
                status_code=403)
        return PlainTextResponse(generate_latest(REGISTRY).decode("utf-8"), media_type=CONTENT_TYPE_LATEST)

    # OTel auto-instrumentation for incoming requests (health/metrics excluded from spans).
    FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,readyz,metrics")

    return app


app = create_app()
