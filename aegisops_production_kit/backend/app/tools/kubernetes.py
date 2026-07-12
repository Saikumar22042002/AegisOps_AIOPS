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

    async def restart_deployment(self, name: str, namespace: str = "default") -> dict[str, Any]:
        """`kubectl rollout restart` — patch a restartedAt annotation onto the pod template so
        the deployment rolls its pods (post-approval only)."""
        self._load()
        apps = k8s_client.AppsV1Api()
        from datetime import datetime, timezone
        stamp = datetime.now(timezone.utc).isoformat()
        patch = {"spec": {"template": {"metadata": {"annotations": {
            "kubectl.kubernetes.io/restartedAt": stamp}}}}}
        try:
            await self._run(apps.patch_namespaced_deployment, name, namespace, patch)
        except ApiException as e:
            raise KubernetesError(f"K8s rollout restart failed: {e}") from e
        log.info("k8s.deployment_restarted", name=name, namespace=namespace)
        return {"action": "restart", "name": name, "namespace": namespace, "restartedAt": stamp}

    async def scale_deployment(self, name: str, replicas: int, namespace: str = "default") -> dict[str, Any]:
        """Patch spec.replicas (post-approval only)."""
        self._load()
        apps = k8s_client.AppsV1Api()
        try:
            await self._run(apps.patch_namespaced_deployment_scale, name, namespace,
                            {"spec": {"replicas": int(replicas)}})
        except ApiException as e:
            raise KubernetesError(f"K8s scale failed: {e}") from e
        log.info("k8s.deployment_scaled", name=name, namespace=namespace, replicas=replicas)
        return {"action": "scale", "name": name, "namespace": namespace, "replicas": int(replicas)}

    async def rollback_deployment(self, name: str, namespace: str = "default") -> dict[str, Any]:
        """`kubectl rollout undo` — roll the deployment back to its previous revision by patching
        its pod template to the prior ReplicaSet's template (post-approval only)."""
        self._load()
        apps = k8s_client.AppsV1Api()
        try:
            dep = await self._run(apps.read_namespaced_deployment, name, namespace)
            cur_rev = int((dep.metadata.annotations or {}).get("deployment.kubernetes.io/revision", "0"))
            rs_list = await self._run(apps.list_namespaced_replica_set, namespace,
                                      label_selector=",".join(f"{k}={v}" for k, v in
                                                              (dep.spec.selector.match_labels or {}).items()))
            revs = []
            for rs in rs_list.items:
                rev = int((rs.metadata.annotations or {}).get("deployment.kubernetes.io/revision", "0"))
                revs.append((rev, rs))
            prior = sorted([r for r in revs if r[0] < cur_rev], key=lambda x: x[0])
            if not prior:
                raise KubernetesError("no previous revision to roll back to")
            target_rs = prior[-1][1]
            patch = {"spec": {"template": target_rs.spec.template.to_dict()}}
            await self._run(apps.patch_namespaced_deployment, name, namespace, patch)
        except ApiException as e:
            raise KubernetesError(f"K8s rollback failed: {e}") from e
        log.info("k8s.deployment_rolled_back", name=name, namespace=namespace, from_revision=cur_rev)
        return {"action": "rollback", "name": name, "namespace": namespace, "from_revision": cur_rev}

    async def ping(self) -> bool:
        await self.list_namespaces()
        return True


_client: KubernetesClient | None = None


def get_kubernetes(settings: Settings) -> KubernetesClient:
    global _client
    if _client is None:
        _client = KubernetesClient(settings)
    return _client
