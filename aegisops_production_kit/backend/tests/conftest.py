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

# P0: the unit tier runs hermetically anywhere. The shipped coordination default is now
# `redis` (the multi-replica posture, with a no-silent-fallback startup guard), so tests
# pin the explicit dev/memory mode BEFORE the app import freezes settings. The api-test
# container overrides these where the live datastores are the point.
os.environ.setdefault("AEGISOPS_EVENT_BUS", "memory")
os.environ.setdefault("AEGISOPS_ROLE", "all")
os.environ.setdefault("AEGISOPS_TELEGRAM", "off")
os.environ.setdefault("AEGISOPS_RECONCILER", "off")

from app.main import app  # noqa: E402 — must import after the env pins above


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
async def live_neo4j():
    """A Neo4j driver bound to this test's event loop (async drivers are loop-bound)."""
    if not _live_enabled():
        pytest.skip("integration test: set AEGISOPS_TEST_LIVE_DATASTORES=1 (run via `make test`)")
    from app.graph_db import neo4j as n4
    from app.settings import get_settings

    if n4._driver is not None:
        try:
            await n4._driver.close()
        except Exception:  # noqa: BLE001
            pass
        n4._driver = None
    driver = n4.init_neo4j(get_settings())
    try:
        await driver.verify_connectivity()
    except Exception:  # noqa: BLE001
        n4._driver = None
        pytest.skip("Neo4j not reachable")
    yield driver
    try:
        await driver.close()
    except Exception:  # noqa: BLE001
        pass
    n4._driver = None


@pytest.fixture
async def throwaway_org(live_db) -> str:
    """A brand-new organization row, deleted afterwards — for tests that must not touch the
    seeded org's real inventory/notifications (e.g. drift sweeps)."""
    import uuid as _uuid

    from sqlalchemy import delete

    from app.db.models import Organization
    from app.db.session import session_scope

    slug = f"itest-{_uuid.uuid4().hex[:10]}"
    async with session_scope() as s:
        org = Organization(name=f"itest {slug}", slug=slug)
        s.add(org)
        await s.flush()
        oid = str(org.id)
    yield oid
    async with session_scope() as s:
        await s.execute(delete(Organization).where(Organization.id == _uuid.UUID(oid)))


@pytest.fixture
async def org_id(live_db) -> str:
    """The seeded primary organization id (integration; requires `make seed` has run)."""
    from sqlalchemy import select

    from app.db import repositories as repo
    from app.db.models import Organization
    from app.db.session import session_scope

    async with session_scope() as s:
        org = await repo.get_org_by_slug(s, "northwind-financial")
        if not org:  # non-standard seeds: fall back to the oldest org
            org = (await s.execute(
                select(Organization).order_by(Organization.created_at).limit(1)
            )).scalar_one_or_none()
    if not org:
        pytest.skip("no seeded organization; run `make seed`")
    return str(org.id)
