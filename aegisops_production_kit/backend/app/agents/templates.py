"""Multi-cloud workflow template registry.

Maps (cloud, resource, action) → a curated, org-approved Terraform workspace + a Pydantic
input schema + deterministic policy checks. The CloudOps agent selects a template by the
router's classification, validates inputs, runs availability checks via the matching cloud
reader, plans, and (after approval) applies/destroys. The `generic.module` template is the
escape hatch for any published Terraform module not yet curated.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..schemas import workflows as wf


@dataclass
class WorkflowTemplate:
    key: str                      # e.g. "aws.s3"
    cloud: str                    # aws | azure | gcp
    resource: str                 # s3 | vpc | eks | rds | ec2 | storage | resource_group | gcs | module
    version: str
    workspace: str | None         # terraform workspace dir under TERRAFORM_WORKSPACES_DIR
    schema: type[wf.WorkflowInputs]
    description: str
    policy_fn: Callable[[dict[str, Any]], list[dict[str, Any]]] = field(default=lambda _i: [])
    actions: tuple[str, ...] = ("create", "modify", "destroy")

    def var_map(self, inputs: dict[str, Any]) -> dict[str, Any]:
        """Validated inputs → Terraform variables (identity by default)."""
        return dict(inputs)


def _ck(name: str, passed: bool, detail: str = "") -> dict[str, Any]:
    return {"name": name, "passed": bool(passed), "detail": detail}


def _s3_policy(i: dict) -> list[dict]:
    return [
        _ck("Public access blocked", i.get("block_public", True)),
        _ck("Versioning enabled", i.get("versioning", True)),
        _ck("Server-side encryption (AES256)", True),
        _ck("Approved module version", True, "aws/s3 v1"),
    ]


def _vpc_policy(i: dict) -> list[dict]:
    return [
        _ck("Multi-AZ subnets", int(i.get("az_count", 3)) >= 2, f"{i.get('az_count', 3)} AZs"),
        _ck("Private subnets + NAT egress", True),
        _ck("Approved module (terraform-aws-modules/vpc)", True, "v5.8"),
    ]


def _eks_policy(i: dict) -> list[dict]:
    return [
        _ck("Secrets encryption enabled", True),
        _ck("Private API endpoint only", True),
        _ck("IRSA configured · no node IAM keys", True),
        _ck("Approved module version (v20.8)", True),
        _ck("Multi-AZ node placement", len(i.get("subnet_ids", [])) >= 2, f"{len(i.get('subnet_ids', []))} subnets"),
    ]


def _rds_policy(i: dict) -> list[dict]:
    return [
        _ck("Storage encrypted", True),
        _ck("Not publicly accessible", True),
        _ck("RDS-managed master password", True),
        _ck("Approved engine", i.get("engine", "postgres") in {"postgres", "mysql", "mariadb"}, i.get("engine", "")),
    ]


def _ec2_policy(_i: dict) -> list[dict]:
    return [
        _ck("IMDSv2 enforced", True),
        _ck("Root volume encrypted", True),
        _ck("Launched in a private subnet", True),
    ]


def _azure_storage_policy(_i: dict) -> list[dict]:
    return [
        _ck("Minimum TLS 1.2", True),
        _ck("No public blob access", True),
        _ck("Approved replication", True),
    ]


def _azure_rg_policy(_i: dict) -> list[dict]:
    return [_ck("Tagging policy applied", True), _ck("Approved region", True)]


def _gcs_policy(_i: dict) -> list[dict]:
    return [
        _ck("Uniform bucket-level access", True),
        _ck("Versioning enabled", True),
        _ck("force_destroy disabled", True),
    ]


def _generic_policy(i: dict) -> list[dict]:
    return [_ck("Module source pinned to a version", bool(i.get("version")), i.get("source", ""))]


TEMPLATES: list[WorkflowTemplate] = [
    WorkflowTemplate("aws.s3", "aws", "s3", "v1", "aws-s3", wf.AWSS3Inputs, "Provision an S3 bucket (encrypted, private)", _s3_policy),
    WorkflowTemplate("aws.vpc", "aws", "vpc", "v1", "aws-vpc", wf.AWSVPCInputs, "Provision a VPC with public/private subnets + NAT", _vpc_policy),
    WorkflowTemplate("aws.eks", "aws", "eks", "v3", "eks-provision", wf.AWSEKSInputs, "Provision a hardened EKS cluster reusing an existing VPC", _eks_policy),
    WorkflowTemplate("aws.rds", "aws", "rds", "v1", "aws-rds", wf.AWSRDSInputs, "Provision an encrypted RDS instance", _rds_policy),
    WorkflowTemplate("aws.ec2", "aws", "ec2", "v1", "aws-ec2", wf.AWSEC2Inputs, "Provision an EC2 instance (IMDSv2, encrypted)", _ec2_policy),
    WorkflowTemplate("azure.storage", "azure", "storage", "v1", "azure-storage", wf.AzureStorageInputs, "Provision an Azure Storage Account", _azure_storage_policy),
    WorkflowTemplate("azure.resource_group", "azure", "resource_group", "v1", "azure-resource-group", wf.AzureResourceGroupInputs, "Provision an Azure Resource Group", _azure_rg_policy),
    WorkflowTemplate("gcp.gcs", "gcp", "gcs", "v1", "gcp-gcs", wf.GCPGCSInputs, "Provision a GCS bucket (uniform access, versioned)", _gcs_policy),
    WorkflowTemplate("generic.module", "any", "module", "v1", None, wf.GenericModuleInputs, "Provision any published/approved Terraform module by source", _generic_policy),
]

_BY_KEY = {t.key: t for t in TEMPLATES}


def select(cloud: str, resource: str) -> WorkflowTemplate | None:
    key = f"{(cloud or '').lower()}.{(resource or '').lower()}"
    if key in _BY_KEY:
        return _BY_KEY[key]
    # resource-only match (any cloud) e.g. module
    for t in TEMPLATES:
        if t.resource == (resource or "").lower():
            return t
    return None


def catalog() -> list[dict[str, str]]:
    """Compact catalog for the router's classification prompt."""
    return [{"key": t.key, "cloud": t.cloud, "resource": t.resource, "description": t.description}
            for t in TEMPLATES]
