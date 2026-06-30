"""Real ServiceNow REST client.

Creates/updates/closes incidents, service requests (sc_request), and change requests
(change_request); attaches artifact links via work notes. Credentials come only from env;
the password leaked in the source ServiceNow doc must be rotated by the operator and never
hard-coded. Retries + timeouts on every call.
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential

from ..settings import Settings

log = structlog.get_logger(__name__)


class ServiceNowError(Exception):
    pass


class ServiceNowClient:
    def __init__(self, settings: Settings) -> None:
        self.base = settings.servicenow_instance.rstrip("/")
        self.user = settings.servicenow_user
        self.password = settings.servicenow_password
        self.enabled = bool(self.base and self.user and self.password)

    def _client(self) -> httpx.AsyncClient:
        if not self.enabled:
            raise ServiceNowError("ServiceNow credentials are not configured")
        return httpx.AsyncClient(
            base_url=self.base,
            auth=(self.user, self.password),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=20.0,
        )

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=0.5, max=8), reraise=True)
    async def _post(self, table: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._client() as client:
            resp = await client.post(f"/api/now/table/{table}", json=payload)
        if resp.status_code >= 300:
            raise ServiceNowError(f"ServiceNow POST {table} failed: {resp.status_code} {resp.text[:300]}")
        return resp.json()["result"]

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=0.5, max=8), reraise=True)
    async def _patch(self, table: str, sys_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._client() as client:
            resp = await client.patch(f"/api/now/table/{table}/{sys_id}", json=payload)
        if resp.status_code >= 300:
            raise ServiceNowError(f"ServiceNow PATCH {table}/{sys_id} failed: {resp.status_code} {resp.text[:300]}")
        return resp.json()["result"]

    async def get(self, table: str, sys_id: str) -> dict[str, Any]:
        async with self._client() as client:
            resp = await client.get(f"/api/now/table/{table}/{sys_id}")
        if resp.status_code >= 300:
            raise ServiceNowError(f"ServiceNow GET {table}/{sys_id} failed: {resp.status_code}")
        return resp.json()["result"]

    # ── Incidents (SRE) ──
    async def create_incident(self, short_description: str, description: str = "", urgency: str = "2",
                              extra: dict | None = None) -> dict[str, Any]:
        res = await self._post("incident", {"short_description": short_description,
                                            "description": description, "urgency": urgency, **(extra or {})})
        log.info("servicenow.incident_created", number=res.get("number"), sys_id=res.get("sys_id"))
        return res

    # ── Service requests (CloudOps provisioning) ──
    async def create_service_request(self, short_description: str, description: str = "",
                                     extra: dict | None = None) -> dict[str, Any]:
        res = await self._post("sc_request", {"short_description": short_description,
                                             "description": description, **(extra or {})})
        log.info("servicenow.sr_created", number=res.get("number"), sys_id=res.get("sys_id"))
        return res

    # ── Change requests (production changes) ──
    async def create_change_request(self, short_description: str, description: str = "",
                                    risk: str = "moderate", extra: dict | None = None) -> dict[str, Any]:
        res = await self._post("change_request", {"short_description": short_description,
                                                 "description": description, "risk": risk, **(extra or {})})
        log.info("servicenow.cr_created", number=res.get("number"), sys_id=res.get("sys_id"))
        return res

    async def update(self, table: str, sys_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        return await self._patch(table, sys_id, fields)

    async def add_work_note(self, table: str, sys_id: str, note: str) -> dict[str, Any]:
        return await self._patch(table, sys_id, {"work_notes": note})

    async def attach_artifact_link(self, table: str, sys_id: str, label: str, url: str) -> dict[str, Any]:
        return await self.add_work_note(table, sys_id, f"[Artifact] {label}: {url}")

    async def close(self, table: str, sys_id: str, close_notes: str = "Resolved by AegisOps",
                    close_code: str = "Solved (Permanently)") -> dict[str, Any]:
        if table == "incident":
            fields = {"state": "6", "close_code": close_code, "close_notes": close_notes}
        elif table == "change_request":
            fields = {"state": "3", "close_code": "successful", "close_notes": close_notes}
        else:
            fields = {"state": "3", "close_notes": close_notes}
        res = await self._patch(table, sys_id, fields)
        log.info("servicenow.closed", table=table, sys_id=sys_id)
        return res

    async def ping(self) -> bool:
        """Health check: list one incident."""
        async with self._client() as client:
            resp = await client.get("/api/now/table/incident", params={"sysparm_limit": 1})
        return resp.status_code < 400


_client: ServiceNowClient | None = None


def get_servicenow(settings: Settings) -> ServiceNowClient:
    global _client
    if _client is None:
        _client = ServiceNowClient(settings)
    return _client
