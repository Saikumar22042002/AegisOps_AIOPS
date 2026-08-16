"""cloudops.azure pack (P4). Read tools wrap AzureReader; mutation declared as templates."""

from __future__ import annotations

from ...settings import Settings
from ...tools.azure import get_azure
from ..base import CapabilityPack, ToolSpec


def build(settings: Settings) -> CapabilityPack:
    az = get_azure(settings)

    async def list_networks():
        return await az.list_vnets()

    async def list_compute():
        return await az.list_vms()

    async def list_resource_groups():
        return await az.list_resource_groups()

    return CapabilityPack(
        name="cloudops.azure", provider="azure", domain="cloudops",
        tools=(
            ToolSpec("cloudops.azure.list_networks", "List Azure VNets", "network", "read", list_networks),
            ToolSpec("cloudops.azure.list_compute", "List Azure VMs", "compute", "read", list_compute),
            ToolSpec("cloudops.azure.list_resource_groups", "List resource groups", "identity", "read", list_resource_groups),
            ToolSpec("cloudops.azure.create_network", "Provision a VNet (Terraform)", "network",
                     "mutation", template_key="azure.vnet"),
            ToolSpec("cloudops.azure.create_compute", "Provision an Azure VM (Terraform)", "compute",
                     "mutation", template_key="azure.vm"),
            ToolSpec("cloudops.azure.create_storage", "Provision Blob Storage (Terraform)", "storage",
                     "mutation", template_key="azure.storage"),
            ToolSpec("cloudops.azure.create_k8s", "Provision AKS (Terraform)", "k8s",
                     "mutation", template_key="azure.aks"),
            ToolSpec("cloudops.azure.create_database", "Provision Azure SQL (Terraform)", "db",
                     "mutation", template_key="azure.db"),
        ),
        knowledge=("Azure resources live in resource groups; a VNet is region-scoped.",),
        templates=("azure.vnet", "azure.vm", "azure.storage", "azure.aks", "azure.db",
                   "azure.keyvault", "azure.resource_group"),
        enabled=lambda s: bool(getattr(get_azure(s), "enabled", True)),
    )
