"""VMware vCenter read-only discovery (pyVmomi). Never provisions."""

from __future__ import annotations

import ssl
from typing import Any

import anyio
import structlog

from ..settings import Settings

log = structlog.get_logger(__name__)

try:
    from pyVim.connect import Disconnect, SmartConnect
    from pyVmomi import vim

    _HAVE_VMOMI = True
except Exception:  # noqa: BLE001
    _HAVE_VMOMI = False


class VMwareError(Exception):
    pass


class VMwareReader:
    def __init__(self, settings: Settings) -> None:
        self.host = settings.vcenter_host
        self.user = settings.vcenter_user
        self.password = settings.vcenter_password
        self.insecure = settings.vcenter_insecure
        self.enabled = bool(_HAVE_VMOMI and self.host and self.user and self.password)

    def _connect(self):
        if not self.enabled:
            raise VMwareError("vCenter credentials are not configured")
        ctx = None
        if self.insecure:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return SmartConnect(host=self.host, user=self.user, pwd=self.password, sslContext=ctx)

    async def _run(self, fn, *args, **kwargs):
        return await anyio.to_thread.run_sync(lambda: fn(*args, **kwargs))

    def _list_vms_sync(self) -> list[dict[str, Any]]:
        si = self._connect()
        try:
            content = si.RetrieveContent()
            view = content.viewManager.CreateContainerView(content.rootFolder, [vim.VirtualMachine], True)
            out = []
            for vm in view.view:
                s = vm.summary
                out.append({"name": s.config.name, "power_state": str(s.runtime.powerState), "cpu": s.config.numCpu, "memory_mb": s.config.memorySizeMB})
            view.Destroy()
            return out
        finally:
            Disconnect(si)

    async def list_vms(self) -> list[dict[str, Any]]:
        return await self._run(self._list_vms_sync)

    async def ping(self) -> bool:
        def _check() -> bool:
            si = self._connect()
            try:
                return si.RetrieveContent().about.fullName is not None
            finally:
                Disconnect(si)

        return await self._run(_check)


_reader: VMwareReader | None = None


def get_vmware(settings: Settings) -> VMwareReader:
    global _reader
    if _reader is None:
        _reader = VMwareReader(settings)
    return _reader
