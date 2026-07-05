"""Shared pytest fixtures.

Two tiers of tests live here:

* **Unit** — exercise app logic with no external services (routing, params, schemas, the SSE
  event contract, redaction, RBAC). The lifespan starts cleanly because datastore clients are
  lazily initialised, so `TestClient(app)` works without Postgres/Redis/Neo4j.
* **Integration** — need the REAL datastores. They use the live compose services (reachable by
  service name inside the `api-test` container, which sets `AEGISOPS_TEST_LIVE_DATASTORES=1`).
  Each integration fixture re-creates the relevant global client in the test's own event loop
  (async clients are loop-bound) and disposes it on teardown. When the datastore is unreachable
  — or the flag is unset (e.g. a bare host run) — the test SKIPS cleanly, so the unit tier stays
  green anywhere. Testcontainers remains available (declared in dev deps) for host-based runs
  that prefer ephemeral datastores; set AEGISOPS_TEST_USE_TESTCONTAINERS=1 to opt in.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from app.main import app


def _live_enabled() -> bool:
    return os.getenv("AEGISOPS_TEST_LIVE_DATASTORES") == "1"


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as c:
        yield c


@pytest.fixture
async def live_db():
    """Yield nothing; guarantees the async SQLAlchemy engine is live for this test's loop."""
    if not _live_enabled():
        pytest.skip("integration test: set AEGISOPS_TEST_LIVE_DATASTORES=1 (run via `make test`)")
    from app.db import session as dbs
    from app.settings import get_settings

    # Recreate the engine in this test's event loop (async engines are loop-bound).
    await dbs.dispose_engine()
    dbs.init_engine(get_settings())
    try:
        ok = await dbs.ping()
    except Exception:  # noqa: BLE001
        ok = False
    if not ok:
        await dbs.dispose_engine()
        pytest.skip("Postgres not reachable")
    yield
    await dbs.dispose_engine()


@pytest.fixture
async def live_redis():
    """Yield a Redis client bound to this test's event loop."""
    if not _live_enabled():
        pytest.skip("integration test: set AEGISOPS_TEST_LIVE_DATASTORES=1 (run via `make test`)")
    from app.cache import redis as rc
    from app.settings import get_settings

    rc._client = None  # drop any client bound to a previous loop (no aclose across loops)
    client = rc.init_redis(get_settings())
    try:
        ok = await client.ping()
    except Exception:  # noqa: BLE001
        ok = False
    if not ok:
        rc._client = None
        pytest.skip("Redis not reachable")
    yield client
    try:
        await rc.close_redis()
    except Exception:  # noqa: BLE001
        rc._client = None


@pytest.fixture
async def org_id(live_db) -> str:
    """The seeded default organization id (integration; requires `make seed` has run)."""
    from app.db import repositories as repo
    from app.db.session import session_scope

    async with session_scope() as s:
        org = await repo.get_default_org(s)
    if not org:
        pytest.skip("no seeded organization; run `make seed`")
    return str(org.id)
