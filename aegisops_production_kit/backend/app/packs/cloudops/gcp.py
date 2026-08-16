"""cloudops.gcp pack (P4). Read tools wrap GCPReader; mutation declared as templates."""

from __future__ import annotations

from ...settings import Settings
from ...tools.gcp import get_gcp
from ..base import CapabilityPack, ToolSpec


def build(settings: Settings) -> CapabilityPack:
    gcp = get_gcp(settings)

    async def list_networks():
        return await gcp.list_networks()

    async def list_compute():
        return await gcp.list_all_instances()

    return CapabilityPack(
        name="cloudops.gcp", provider="gcp", domain="cloudops",
        tools=(
            ToolSpec("cloudops.gcp.list_networks", "List GCP VPC networks", "network", "read", list_networks),
            ToolSpec("cloudops.gcp.list_compute", "List Compute Engine instances", "compute", "read", list_compute),
            ToolSpec("cloudops.gcp.create_network", "Provision a VPC (Terraform)", "network",
                     "mutation", template_key="gcp.vpc"),
            ToolSpec("cloudops.gcp.create_compute", "Provision Compute Engine (Terraform)", "compute",
                     "mutation", template_key="gcp.vm"),
            ToolSpec("cloudops.gcp.create_storage", "Provision Cloud Storage (Terraform)", "storage",
                     "mutation", template_key="gcp.gcs"),
            ToolSpec("cloudops.gcp.create_k8s", "Provision GKE (Terraform)", "k8s",
                     "mutation", template_key="gcp.gke"),
            ToolSpec("cloudops.gcp.create_database", "Provision Cloud SQL (Terraform)", "db",
                     "mutation", template_key="gcp.cloudsql"),
        ),
        knowledge=("GCP instances are zone-scoped; a VPC network is global with regional "
                   "subnets.",),
        templates=("gcp.vpc", "gcp.vm", "gcp.gcs", "gcp.gke", "gcp.cloudsql", "gcp.kms"),
        enabled=lambda s: bool(getattr(get_gcp(s), "enabled", True)),
    )
