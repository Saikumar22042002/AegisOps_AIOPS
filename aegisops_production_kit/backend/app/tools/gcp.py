"""GCP read-only discovery (google-cloud-*). Never provisions."""

from __future__ import annotations

from typing import Any

import anyio
import structlog

from ..settings import Settings

log = structlog.get_logger(__name__)

try:
    from google.cloud import compute_v1, resourcemanager_v3

    _HAVE_GCP = True
except Exception:  # noqa: BLE001
    _HAVE_GCP = False


class GCPError(Exception):
    pass


class GCPReader:
    def __init__(self, settings: Settings) -> None:
        self.project = settings.google_cloud_project
        # Credentials are picked up from GOOGLE_APPLICATION_CREDENTIALS by the SDK.
        self.enabled = bool(_HAVE_GCP and self.project and settings.google_application_credentials)

    def _require(self) -> None:
        if not self.enabled:
            raise GCPError("GCP project / credentials are not configured")

    async def _run(self, fn, *args, **kwargs):
        return await anyio.to_thread.run_sync(lambda: fn(*args, **kwargs))

    async def list_networks(self) -> list[dict[str, Any]]:
        self._require()
        client = compute_v1.NetworksClient()
        return await self._run(lambda: [{"name": n.name, "auto_create_subnetworks": n.auto_create_subnetworks} for n in client.list(project=self.project)])

    async def list_instances(self, zone: str) -> list[dict[str, Any]]:
        self._require()
        client = compute_v1.InstancesClient()
        return await self._run(lambda: [{"name": i.name, "status": i.status, "machine_type": i.machine_type.split("/")[-1]} for i in client.list(project=self.project, zone=zone)])

    async def ping(self) -> bool:
        self._require()
        client = resourcemanager_v3.ProjectsClient()
        await self._run(lambda: client.get_project(name=f"projects/{self.project}"))
        return True


_reader: GCPReader | None = None


def get_gcp(settings: Settings) -> GCPReader:
    global _reader
    if _reader is None:
        _reader = GCPReader(settings)
    return _reader
