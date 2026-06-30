"""Prometheus query client (PromQL) for metrics / analytics / SRE telemetry."""

from __future__ import annotations

from typing import Any

import httpx
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential

from ..settings import Settings

log = structlog.get_logger(__name__)


class PrometheusError(Exception):
    pass


class PrometheusClient:
    def __init__(self, settings: Settings) -> None:
        self.base = settings.prometheus_url.rstrip("/")
        self.enabled = bool(self.base)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=0.3, max=4), reraise=True)
    async def query(self, promql: str) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(f"{self.base}/api/v1/query", params={"query": promql})
        if resp.status_code >= 400:
            raise PrometheusError(f"PromQL query failed: {resp.status_code} {resp.text[:200]}")
        data = resp.json()
        if data.get("status") != "success":
            raise PrometheusError(f"PromQL error: {data.get('error')}")
        return data["data"]["result"]

    async def query_range(self, promql: str, start: str, end: str, step: str = "60s") -> list[dict[str, Any]]:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(
                f"{self.base}/api/v1/query_range",
                params={"query": promql, "start": start, "end": end, "step": step},
            )
        if resp.status_code >= 400:
            raise PrometheusError(f"PromQL range failed: {resp.status_code}")
        return resp.json()["data"]["result"]

    async def scalar(self, promql: str, default: float = 0.0) -> float:
        result = await self.query(promql)
        if result and "value" in result[0]:
            try:
                return float(result[0]["value"][1])
            except (ValueError, IndexError):
                return default
        return default

    async def ping(self) -> bool:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{self.base}/-/healthy")
        return resp.status_code < 400


_client: PrometheusClient | None = None


def get_prometheus(settings: Settings) -> PrometheusClient:
    global _client
    if _client is None:
        _client = PrometheusClient(settings)
    return _client
