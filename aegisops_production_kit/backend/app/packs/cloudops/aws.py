"""cloudops.aws pack (P4). Read tools wrap the existing AWSReader (provider-specific logic
stays here); mutation is DECLARED as approved Terraform template keys, executed by the
governed exec_loop/approval/P3 path — never as a read tool."""

from __future__ import annotations

from ...settings import Settings
from ...tools.aws import get_aws
from ..base import CapabilityPack, ToolSpec


def build(settings: Settings) -> CapabilityPack:
    aws = get_aws(settings)

    async def list_networks(region: str | None = None):
        return await aws.list_vpcs(region=region)

    async def list_subnets(vpc_id: str, region: str | None = None):
        return await aws.list_subnets(vpc_id, region=region)

    async def list_compute(region: str | None = None):
        return await aws.list_instances(region=region)

    async def list_k8s_clusters(region: str | None = None):
        return await aws.list_eks_clusters(region=region)

    async def list_storage():
        return await aws.list_buckets()

    async def list_databases(region: str | None = None):
        return await aws.list_databases(region=region)

    return CapabilityPack(
        name="cloudops.aws", provider="aws", domain="cloudops",
        tools=(
            ToolSpec("cloudops.aws.list_networks", "List AWS VPCs", "network", "read", list_networks),
            ToolSpec("cloudops.aws.list_subnets", "List subnets of a VPC", "network", "read", list_subnets),
            ToolSpec("cloudops.aws.list_compute", "List EC2 instances", "compute", "read", list_compute),
            ToolSpec("cloudops.aws.list_k8s_clusters", "List EKS clusters", "k8s", "read", list_k8s_clusters),
            ToolSpec("cloudops.aws.list_storage", "List S3 buckets", "storage", "read", list_storage),
            ToolSpec("cloudops.aws.list_databases", "List RDS databases", "db", "read", list_databases),
            # Declared mutation capabilities (approved Terraform catalog) — governed path only.
            ToolSpec("cloudops.aws.create_network", "Provision a VPC (Terraform)", "network",
                     "mutation", template_key="aws.vpc"),
            ToolSpec("cloudops.aws.create_compute", "Provision EC2 (Terraform)", "compute",
                     "mutation", template_key="aws.ec2"),
            ToolSpec("cloudops.aws.create_storage", "Provision S3 (Terraform)", "storage",
                     "mutation", template_key="aws.s3"),
            ToolSpec("cloudops.aws.create_k8s", "Provision EKS (Terraform)", "k8s",
                     "mutation", template_key="aws.eks"),
            ToolSpec("cloudops.aws.create_database", "Provision RDS (Terraform)", "db",
                     "mutation", template_key="aws.rds"),
        ),
        knowledge=("AWS regions default to the caller's configured region; VPC→subnet→"
                   "instance is the network containment order.",),
        templates=("aws.vpc", "aws.ec2", "aws.s3", "aws.eks", "aws.rds", "aws.nlb", "aws.kms"),
        enabled=lambda s: bool(getattr(get_aws(s), "enabled", True)),
    )
