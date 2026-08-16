"""sreops.k8s pack (P4). Read tools (namespaces/deployments/pods) + Prometheus telemetry
for the harness INV loop. K8s MUTATION (apply/restart/scale/rollback) is DECLARED as day-2
verbs, executed only by the governed approval/engine path — never as a read tool."""

from __future__ import annotations

from ...settings import Settings
from ...tools.kubernetes import get_kubernetes
from ...tools.prometheus import get_prometheus
from ..base import CapabilityPack, ToolSpec


def build(settings: Settings) -> CapabilityPack:
    k8s = get_kubernetes(settings)
    prom = get_prometheus(settings)

    async def list_namespaces():
        return await k8s.list_namespaces()

    async def list_deployments(namespace: str = "default"):
        return await k8s.list_deployments(namespace)

    async def list_pods(namespace: str = "default"):
        return await k8s.list_pods(namespace)

    async def query_telemetry(query: str, default: float = 0.0):
        return await prom.scalar(query, default=default)

    return CapabilityPack(
        name="sreops.k8s", provider="k8s", domain="sreops",
        tools=(
            ToolSpec("sreops.k8s.list_namespaces", "List Kubernetes namespaces", "k8s", "read", list_namespaces),
            ToolSpec("sreops.k8s.list_deployments", "List deployments in a namespace", "k8s", "read", list_deployments),
            ToolSpec("sreops.k8s.list_pods", "List pods in a namespace", "k8s", "read", list_pods),
            ToolSpec("sreops.k8s.query_telemetry", "PromQL scalar telemetry query", "telemetry", "read", query_telemetry),
            # Declared day-2 lifecycle verbs — governed remediation path only.
            ToolSpec("sreops.k8s.restart", "Restart a deployment (day-2)", "k8s",
                     "mutation", day2_verb="k8s.restart"),
            ToolSpec("sreops.k8s.scale", "Scale a deployment (day-2)", "k8s",
                     "mutation", day2_verb="k8s.scale"),
            ToolSpec("sreops.k8s.rollback", "Roll back a deployment (day-2)", "k8s",
                     "mutation", day2_verb="k8s.rollback"),
        ),
        knowledge=("Investigate before remediating: pods → restarts → recent deploys → "
                   "per-service error rate. Remediation is a governed day-2 action.",),
        day2=("k8s.restart", "k8s.scale", "k8s.rollback"),
        enabled=lambda s: bool(getattr(get_kubernetes(s), "enabled", True)),
    )
