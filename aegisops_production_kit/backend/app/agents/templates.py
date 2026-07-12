"""Multi-cloud workflow template registry.

Maps (cloud, resource, action) → a curated, org-approved Terraform workspace + a Pydantic
input schema + deterministic policy checks. The CloudOps agent selects a template by the
router's classification, validates inputs, runs availability checks via the matching cloud
reader, plans, and (after approval) applies/destroys. There is deliberately NO generic /
runtime-HCL escape hatch: the LLM only selects a curated template and passes variables — it
never authors or templates HCL (see the 2.3 Terraform-integrity audit).
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
    resource: str                 # s3|vpc|eks|rds|ec2|storage|resource_group|vm|postgres|aks|gcs|gke|cloudsql
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
    """A REAL policy check: `passed` is a genuine predicate over the request inputs."""
    return {"name": name, "passed": bool(passed), "detail": detail, "evaluated": True}


def _todo(name: str, detail: str = "") -> dict[str, Any]:
    """Honesty label (P8, Phase 1): a control the module enforces but the policy engine does not
    yet VERIFY against the plan. Rendered as "not evaluated" — never a green pass an approver
    might trust. Becomes a real predicate over `terraform show -json` in Phase 2 (U1).
    `passed=None` is the not-evaluated signal for the approval card."""
    d = "enforced by the module · verified against the plan in a later pass"
    return {"name": name, "passed": None, "detail": detail or d, "evaluated": False}


def _s3_policy(i: dict) -> list[dict]:
    return [
        _ck("Public access blocked", i.get("block_public", True)),
        _ck("Versioning enabled", i.get("versioning", True)),
        _todo("Server-side encryption (AES256)"),
        _todo("Approved module version", "aws/s3 v1"),
    ]


def _vpc_policy(i: dict) -> list[dict]:
    return [
        _ck("Multi-AZ subnets", int(i.get("az_count", 3)) >= 2, f"{i.get('az_count', 3)} AZs"),
        _todo("Private subnets + NAT egress"),
        _todo("Approved module (terraform-aws-modules/vpc)", "v5.8"),
    ]


def _eks_policy(i: dict) -> list[dict]:
    return [
        _todo("Secrets encryption enabled"),
        _todo("Private API endpoint only"),
        _todo("IRSA configured · no node IAM keys"),
        _todo("Approved module version (v20.8)"),
        _ck("Multi-AZ node placement", len(i.get("subnet_ids", [])) >= 2, f"{len(i.get('subnet_ids', []))} subnets"),
    ]


def _rds_policy(i: dict) -> list[dict]:
    return [
        _todo("Storage encrypted"),
        _todo("Not publicly accessible"),
        _todo("RDS-managed master password"),
        _ck("Approved engine", i.get("engine", "postgres") in {"postgres", "mysql", "mariadb"}, i.get("engine", "")),
    ]


def _ec2_policy(_i: dict) -> list[dict]:
    return [
        _todo("IMDSv2 enforced"),
        _todo("Root volume encrypted"),
        _todo("Launched in a private subnet"),
    ]


def _azure_storage_policy(_i: dict) -> list[dict]:
    return [
        _todo("Minimum TLS 1.2"),
        _todo("No public blob access"),
        _todo("Approved replication"),
    ]


def _azure_rg_policy(_i: dict) -> list[dict]:
    return [_todo("Tagging policy applied"), _todo("Approved region")]


def _gcs_policy(_i: dict) -> list[dict]:
    return [
        _todo("Uniform bucket-level access"),
        _todo("Versioning enabled"),
        _todo("force_destroy disabled"),
    ]


def _azure_vm_policy(_i: dict) -> list[dict]:
    return [
        _todo("SSH key auth (no password)"),
        _todo("Dedicated NSG (default-deny inbound)"),
        _todo("Managed OS disk"),
    ]


def _azure_pg_policy(_i: dict) -> list[dict]:
    return [
        _todo("TLS-enforced connections"),
        _todo("Server-managed admin password (generated)"),
        _ck("Approved PostgreSQL version", str(_i.get("pg_version", "15")) in {"14", "15", "16"}, _i.get("pg_version", "")),
    ]


def _azure_aks_policy(_i: dict) -> list[dict]:
    return [
        _todo("System-assigned managed identity"),
        _todo("Azure RBAC enabled"),
        _ck("Multi-node pool", int(_i.get("node_count", 2)) >= 2, f"{_i.get('node_count', 2)} nodes"),
    ]


def _gcp_gce_policy(_i: dict) -> list[dict]:
    return [
        _todo("SSH key auth (no password)"),
        _todo("Ingress restricted to declared ports"),
        _todo("Labelled ManagedBy=aegisops"),
    ]


def _gcp_gke_policy(_i: dict) -> list[dict]:
    return [
        _todo("Dedicated node pool (default removed)"),
        _todo("Deletion protection off (day-2 destroy)"),
        _ck("Multi-node pool", int(_i.get("node_count", 2)) >= 2, f"{_i.get('node_count', 2)} nodes"),
    ]


def _gcp_cloudsql_policy(_i: dict) -> list[dict]:
    return [
        _todo("Generated root password"),
        _ck("Approved engine (PostgreSQL)", str(_i.get("database_version", "")).startswith("POSTGRES"), _i.get("database_version", "")),
    ]


# Every template is a curated, org-approved, version-controlled Terraform workspace. There is
# deliberately NO runtime-HCL / arbitrary-module escape hatch: the LLM only selects a template
# and passes variables — it never authors or templates HCL (see 2.3 integrity audit).
TEMPLATES: list[WorkflowTemplate] = [
    WorkflowTemplate("aws.s3", "aws", "s3", "v1", "aws-s3", wf.AWSS3Inputs, "Provision an S3 bucket (encrypted, private)", _s3_policy),
    WorkflowTemplate("aws.vpc", "aws", "vpc", "v1", "aws-vpc", wf.AWSVPCInputs, "Provision a VPC with public/private subnets + NAT", _vpc_policy),
    WorkflowTemplate("aws.eks", "aws", "eks", "v3", "eks-provision", wf.AWSEKSInputs, "Provision a hardened EKS cluster reusing an existing VPC", _eks_policy),
    WorkflowTemplate("aws.rds", "aws", "rds", "v1", "aws-rds", wf.AWSRDSInputs, "Provision an encrypted RDS instance", _rds_policy),
    WorkflowTemplate("aws.ec2", "aws", "ec2", "v1", "aws-ec2", wf.AWSEC2Inputs, "Provision an EC2 instance (IMDSv2, encrypted)", _ec2_policy),
    WorkflowTemplate("azure.storage", "azure", "storage", "v1", "azure-storage", wf.AzureStorageInputs, "Provision an Azure Storage Account", _azure_storage_policy),
    WorkflowTemplate("azure.resource_group", "azure", "resource_group", "v1", "azure-resource-group", wf.AzureResourceGroupInputs, "Provision an Azure Resource Group", _azure_rg_policy),
    WorkflowTemplate("azure.vm", "azure", "vm", "v1", "azure-vm", wf.AzureVMInputs, "Provision an Azure Linux VM (generated SSH key)", _azure_vm_policy),
    WorkflowTemplate("azure.postgres", "azure", "postgres", "v1", "azure-postgres", wf.AzurePostgresInputs, "Provision an Azure PostgreSQL Flexible Server", _azure_pg_policy),
    WorkflowTemplate("azure.aks", "azure", "aks", "v1", "azure-aks", wf.AzureAKSInputs, "Provision an Azure Kubernetes Service (AKS) cluster", _azure_aks_policy),
    WorkflowTemplate("gcp.gcs", "gcp", "gcs", "v1", "gcp-gcs", wf.GCPGCSInputs, "Provision a GCS bucket (uniform access, versioned)", _gcs_policy),
    WorkflowTemplate("gcp.vm", "gcp", "vm", "v1", "gcp-gce", wf.GCPComputeInputs, "Provision a GCP Compute Engine VM (generated SSH key)", _gcp_gce_policy),
    WorkflowTemplate("gcp.gke", "gcp", "gke", "v1", "gcp-gke", wf.GCPGKEInputs, "Provision a GKE cluster", _gcp_gke_policy),
    WorkflowTemplate("gcp.cloudsql", "gcp", "cloudsql", "v1", "gcp-cloudsql", wf.GCPCloudSQLInputs, "Provision a Cloud SQL for PostgreSQL instance", _gcp_cloudsql_policy),
]

_BY_KEY = {t.key: t for t in TEMPLATES}

# Per-cloud resource synonyms → the canonical resource key for that cloud. Lets a generic word
# ("vm", "database", "k8s") resolve to the right cloud-specific module (aws.ec2 vs azure.vm vs
# gcp.vm; aws.rds vs azure.postgres vs gcp.cloudsql; aws.eks vs azure.aks vs gcp.gke).
_SYNONYMS: dict[str, dict[str, str]] = {
    "aws": {"vm": "ec2", "instance": "ec2", "server": "ec2", "compute": "ec2", "database": "rds",
            "db": "rds", "postgres": "rds", "postgresql": "rds", "mysql": "rds", "sql": "rds",
            "k8s": "eks", "kubernetes": "eks", "cluster": "eks", "bucket": "s3", "blob": "s3",
            "object_storage": "s3", "network": "vpc"},
    "azure": {"instance": "vm", "server": "vm", "compute": "vm", "ec2": "vm", "database": "postgres",
              "db": "postgres", "postgresql": "postgres", "sql": "postgres", "mysql": "postgres",
              "k8s": "aks", "kubernetes": "aks", "cluster": "aks", "blob": "storage", "bucket": "storage",
              "object_storage": "storage", "storage_account": "storage", "rg": "resource_group"},
    "gcp": {"instance": "vm", "server": "vm", "compute": "vm", "gce": "vm", "ec2": "vm",
            "database": "cloudsql", "db": "cloudsql", "postgres": "cloudsql", "postgresql": "cloudsql",
            "sql": "cloudsql", "mysql": "cloudsql", "k8s": "gke", "kubernetes": "gke", "cluster": "gke",
            "bucket": "gcs", "blob": "gcs", "object_storage": "gcs"},
}


def select(cloud: str, resource: str) -> WorkflowTemplate | None:
    """Exact (cloud, resource) → curated template, or None (after resolving cloud synonyms).

    NO cross-cloud fallback: a request for a cloud/resource without an approved module returns
    None so the agent can clarify honestly. This makes wrong-cloud execution (e.g. an Azure VM
    request planning `aws.ec2`) structurally impossible.
    """
    cloud = (cloud or "").lower()
    resource = (resource or "").lower()
    resource = _SYNONYMS.get(cloud, {}).get(resource, resource)
    return _BY_KEY.get(f"{cloud}.{resource}")


def catalog() -> list[dict[str, str]]:
    """Compact catalog for the router's classification prompt."""
    return [{"key": t.key, "cloud": t.cloud, "resource": t.resource, "description": t.description}
            for t in TEMPLATES]
