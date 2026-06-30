"""Kubernetes client (official SDK) — reads freely; applies only after approval."""

from __future__ import annotations

import os
from typing import Any

import anyio
import structlog

from ..settings import Settings

log = structlog.get_logger(__name__)

try:
    from kubernetes import client as k8s_client
    from kubernetes import config as k8s_config
    from kubernetes.client.rest import ApiException

    _HAVE_K8S = True
except Exception:  # noqa: BLE001
    _HAVE_K8S = False


class KubernetesError(Exception):
    pass


class KubernetesClient:
    def __init__(self, settings: Settings) -> None:
        self.kubeconfig = settings.kubeconfig
        self.enabled = bool(_HAVE_K8S and self.kubeconfig and os.path.exists(self.kubeconfig))
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return
        if not self.enabled:
            raise KubernetesError("KUBECONFIG is not configured or file missing")
        k8s_config.load_kube_config(config_file=self.kubeconfig)
        self._loaded = True

    async def _run(self, fn, *args, **kwargs):
        return await anyio.to_thread.run_sync(lambda: fn(*args, **kwargs))

    async def list_namespaces(self) -> list[str]:
        self._load()
        core = k8s_client.CoreV1Api()
        res = await self._run(core.list_namespace)
        return [ns.metadata.name for ns in res.items]

    async def list_deployments(self, namespace: str = "default") -> list[dict[str, Any]]:
        self._load()
        apps = k8s_client.AppsV1Api()
        res = await self._run(apps.list_namespaced_deployment, namespace)
        return [
            {
                "name": d.metadata.name,
                "namespace": d.metadata.namespace,
                "replicas": d.spec.replicas,
                "ready": d.status.ready_replicas or 0,
                "image": d.spec.template.spec.containers[0].image if d.spec.template.spec.containers else None,
            }
            for d in res.items
        ]

    async def list_pods(self, namespace: str = "default") -> list[dict[str, Any]]:
        self._load()
        core = k8s_client.CoreV1Api()
        res = await self._run(core.list_namespaced_pod, namespace)
        return [{"name": p.metadata.name, "phase": p.status.phase} for p in res.items]

    async def apply_deployment(self, namespace: str, manifest: dict[str, Any]) -> dict[str, Any]:
        """Create-or-replace a Deployment (post-approval only)."""
        self._load()
        apps = k8s_client.AppsV1Api()
        name = manifest["metadata"]["name"]
        try:
            await self._run(apps.read_namespaced_deployment, name, namespace)
            res = await self._run(apps.patch_namespaced_deployment, name, namespace, manifest)
        except ApiException as e:
            if e.status == 404:
                res = await self._run(apps.create_namespaced_deployment, namespace, manifest)
            else:
                raise KubernetesError(f"K8s apply failed: {e}") from e
        log.info("k8s.deployment_applied", name=name, namespace=namespace)
        return {"name": res.metadata.name, "namespace": res.metadata.namespace}

    async def ping(self) -> bool:
        await self.list_namespaces()
        return True


_client: KubernetesClient | None = None


def get_kubernetes(settings: Settings) -> KubernetesClient:
    global _client
    if _client is None:
        _client = KubernetesClient(settings)
    return _client
