"""Shared pytest fixtures.

Unit tests here exercise the app without external services (datastore clients are
initialised lazily, so the lifespan starts cleanly). Integration tests that need real
PostgreSQL/Redis/Neo4j spin them up via testcontainers (added in M3/M6).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as c:
        yield c
