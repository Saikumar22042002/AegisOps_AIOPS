"""Azure read-only discovery (azure-identity + azure-mgmt-*). Never provisions."""

from __future__ import annotations

from typing import Any

import anyio
import structlog

from ..settings import Settings

log = structlog.get_logger(__name__)

try:
    from azure.identity import ClientSecretCredential
    from azure.mgmt.compute import ComputeManagementClient
    from azure.mgmt.network import NetworkManagementClient

    # azure-mgmt-resource ≥ 23 no longer re-exports ResourceManagementClient from the package
    # root (the top level is a namespace with only the versioned `resources` subpackage) — it lives
    # at azure.mgmt.resource.resources. Import it there so discovery works with the pinned SDK.
    from azure.mgmt.resource.resources import ResourceManagementClient

    _HAVE_AZURE = True
except Exception:  # noqa: BLE001
    _HAVE_AZURE = False


class AzureError(Exception):
    pass


class AzureReader:
    def __init__(self, settings: Settings) -> None:
        self.sub = settings.azure_subscription_id
        self.enabled = bool(
            _HAVE_AZURE
            and settings.azure_tenant_id
            and settings.azure_client_id
            and settings.azure_client_secret
            and self.sub
        )
        self._cred = None
        if self.enabled:
            self._cred = ClientSecretCredential(
                tenant_id=settings.azure_tenant_id,
                client_id=settings.azure_client_id,
                client_secret=settings.azure_client_secret,
            )

    def _require(self) -> None:
        if not self.enabled:
            raise AzureError("Azure credentials are not configured")

    async def _run(self, fn, *args, **kwargs):
        return await anyio.to_thread.run_sync(lambda: fn(*args, **kwargs))

    async def list_resource_groups(self) -> list[dict[str, Any]]:
        self._require()
        rmc = ResourceManagementClient(self._cred, self.sub)
        return await self._run(lambda: [{"name": g.name, "location": g.location} for g in rmc.resource_groups.list()])

    async def list_vnets(self) -> list[dict[str, Any]]:
        self._require()
        nmc = NetworkManagementClient(self._cred, self.sub)
        return await self._run(
            lambda: [{"name": v.name, "location": v.location, "address_space": v.address_space.address_prefixes if v.address_space else []} for v in nmc.virtual_networks.list_all()]
        )

    async def list_vms(self) -> list[dict[str, Any]]:
        self._require()
        cmc = ComputeManagementClient(self._cred, self.sub)
        return await self._run(lambda: [{"name": vm.name, "location": vm.location, "size": vm.hardware_profile.vm_size if vm.hardware_profile else None} for vm in cmc.virtual_machines.list_all()])

    async def ping(self) -> bool:
        self._require()
        rmc = ResourceManagementClient(self._cred, self.sub)
        await self._run(lambda: next(iter(rmc.resource_groups.list()), None))
        return True


_reader: AzureReader | None = None


def get_azure(settings: Settings) -> AzureReader:
    global _reader
    if _reader is None:
        _reader = AzureReader(settings)
    return _reader
