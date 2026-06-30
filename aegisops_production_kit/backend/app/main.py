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
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address
from starlette.middleware.base import BaseHTTPMiddleware

from . import __version__
from .agents.checkpointer import close_checkpointer, init_checkpointer
from .agents.graph import init_graph
from .api import artifacts, auth, chat, health, integrations, knowledge, modules, sessions
from .cache import redis as redis_client
from .db import session as db
from .graph_db import neo4j as neo4j_client
from .logging_conf import bind_correlation, clear_correlation, configure_logging, get_logger
from .metrics import API_REQUEST_DURATION, API_REQUESTS, REGISTRY
from .otel import setup_otel, shutdown_otel
from .settings import get_settings

log = get_logger(__name__)
settings = get_settings()

limiter = Limiter(key_func=get_remote_address, default_limits=[f"{settings.rate_limit_per_minute}/minute"])


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(settings.log_level)
    setup_otel(settings)
    db.init_engine(settings)
    redis_client.init_redis(settings)
    neo4j_client.init_neo4j(settings)
    # LangGraph durable checkpointer + compiled multi-agent graph. If the DB is unreachable
    # the API still starts (degraded) and /readyz reports it; chat then errors loudly rather
    # than silently faking data.
    try:
        checkpointer = await init_checkpointer(settings)
        init_graph(checkpointer)
    except Exception as exc:  # noqa: BLE001
        log.error("startup.graph_init_failed", error=str(exc))
    log.info("app.startup", version=__version__, env=settings.app_env)
    try:
        yield
    finally:
        # Graceful shutdown: close datastore clients and flush telemetry.
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

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        return PlainTextResponse(generate_latest(REGISTRY).decode("utf-8"), media_type=CONTENT_TYPE_LATEST)

    # OTel auto-instrumentation for incoming requests (health/metrics excluded from spans).
    FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,readyz,metrics")

    return app


app = create_app()
