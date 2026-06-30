"""Auth-boundary tests — protected endpoints reject unauthenticated callers (401).

These hit the RBAC dependency, which rejects before touching any datastore, so they run
without external services.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_integrations_requires_auth(client: TestClient) -> None:
    assert client.get("/integrations").status_code == 401


def test_modules_requires_auth(client: TestClient) -> None:
    assert client.get("/modules/admin").status_code == 401


def test_run_requires_auth(client: TestClient) -> None:
    assert client.get("/runs/abc").status_code == 401


def test_knowledge_search_requires_auth(client: TestClient) -> None:
    assert client.get("/knowledge/search?q=test").status_code == 401


def test_healthz_open(client: TestClient) -> None:
    assert client.get("/healthz").status_code == 200
