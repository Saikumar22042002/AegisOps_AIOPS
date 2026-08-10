"""Smoke tests for liveness + metrics (no external dependencies required)."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_healthz_ok(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "governance" in body  # posture stamp rides liveness so flag drift is visible


def test_request_id_header(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.headers.get("x-request-id")


def test_metrics_exposed(client: TestClient) -> None:
    resp = client.get("/metrics")
    assert resp.status_code == 200
    # Our custom metric families are registered and exported.
    assert "aegisops_api_requests_total" in resp.text
