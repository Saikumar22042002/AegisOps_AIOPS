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
    policy_fn: Callable[..., list[dict[str, Any]]] = field(default=lambda _i, resources=None: [])
    actions: tuple[str, ...] = ("create", "modify", "destroy")

    def var_map(self, inputs: dict[str, Any]) -> dict[str, Any]:
        """Validated inputs → Terraform variables (identity by default)."""
        return dict(inputs)


def _ck(name: str, passed: bool, detail: str = "") -> dict[str, Any]:
    """A REAL policy check: `passed` is a genuine predicate over the request inputs."""
    return {"name": name, "passed": bool(passed), "detail": detail, "evaluated": True}


def _todo(name: str, detail: str = "") -> dict[str, Any]:
    """Honesty label: a control the module enforces but the policy engine cannot VERIFY against
    the plan (attribute not extractable, or the plan JSON isn't available on this path).
    Rendered as "not evaluated" — never a green pass. `passed=None` is the not-evaluated signal."""
    d = "enforced by the module · not verifiable from the plan JSON here"
    return {"name": name, "passed": None, "detail": detail or d, "evaluated": False}


# U1: real-predicate helpers over the planned resource attributes (terraform show -json
# `change.after`), passed in by cloudops after show_plan. `resources` is a list of
# {type, name, address, after}. None/empty ⇒ no plan available ⇒ checks stay "not evaluated".
def _after(resources, tf_type: str) -> dict | None:
    for r in (resources or []):
        if r.get("type") == tf_type:
            return r.get("after") or {}
    return None


def _block0(after: dict, key: str) -> dict:
    """First element of a Terraform nested-block list attribute (e.g. root_block_device)."""
    v = (after or {}).get(key)
    if isinstance(v, list) and v:
        return v[0] or {}
    return v if isinstance(v, dict) else {}


def _s3_policy(i: dict, resources=None) -> list[dict]:
    pab = _after(resources, "aws_s3_bucket_public_access_block")
    sse = _after(resources, "aws_s3_bucket_server_side_encryption_configuration")
    ver = _after(resources, "aws_s3_bucket_versioning")
    checks = []
    if pab is not None:
        blocked = all(bool(pab.get(k)) for k in
                      ("block_public_acls", "block_public_policy", "ignore_public_acls", "restrict_public_buckets"))
        checks.append(_ck("Public access blocked", blocked,
                          "all four public-access-block flags on" if blocked else "a public-access flag is OFF"))
    else:
        checks.append(_ck("Public access blocked", i.get("block_public", True)))
    if sse is not None:
        rule = _block0(sse, "rule")
        algo = (_block0(rule, "apply_server_side_encryption_by_default") or {}).get("sse_algorithm")
        checks.append(_ck("Server-side encryption", bool(algo), algo or "no SSE rule in the plan"))
    else:
        checks.append(_todo("Server-side encryption (AES256)"))
    if ver is not None:
        status = (_block0(ver, "versioning_configuration") or {}).get("status")
        checks.append(_ck("Versioning enabled", status == "Enabled", status or "unset"))
    else:
        checks.append(_ck("Versioning enabled", i.get("versioning", True)))
    checks.append(_todo("Approved module version", "aws/s3 v1"))
    return checks


def _vpc_policy(i: dict, resources=None) -> list[dict]:
    return [
        _ck("Multi-AZ subnets", int(i.get("az_count", 3)) >= 2, f"{i.get('az_count', 3)} AZs"),
        _todo("Private subnets + NAT egress"),
        _todo("Approved module (terraform-aws-modules/vpc)", "v5.8"),
    ]


def _eks_policy(i: dict, resources=None) -> list[dict]:
    return [
        _todo("Secrets encryption enabled"),
        _todo("Private API endpoint only"),
        _todo("IRSA configured · no node IAM keys"),
        _todo("Approved module version (v20.8)"),
        _ck("Multi-AZ node placement", len(i.get("subnet_ids", [])) >= 2, f"{len(i.get('subnet_ids', []))} subnets"),
    ]


def _rds_policy(i: dict, resources=None) -> list[dict]:
    db = _after(resources, "aws_db_instance")
    checks = []
    if db is not None:
        checks.append(_ck("Storage encrypted", bool(db.get("storage_encrypted")),
                          "encrypted" if db.get("storage_encrypted") else "NOT encrypted"))
        checks.append(_ck("Not publicly accessible", db.get("publicly_accessible") is False,
                          "public" if db.get("publicly_accessible") else "private"))
    else:
        checks.append(_todo("Storage encrypted"))
        checks.append(_todo("Not publicly accessible"))
    checks.append(_todo("RDS-managed master password"))
    checks.append(_ck("Approved engine", i.get("engine", "postgres") in {"postgres", "mysql", "mariadb"}, i.get("engine", "")))
    return checks


def _ec2_policy(i: dict, resources=None) -> list[dict]:
    inst = _after(resources, "aws_instance")
    if inst is None:
        return [_todo("IMDSv2 enforced"), _todo("Root volume encrypted"), _todo("Dedicated security group")]
    mo = _block0(inst, "metadata_options")
    rbd = _block0(inst, "root_block_device")
    tokens = mo.get("http_tokens")
    return [
        _ck("IMDSv2 enforced", tokens == "required", tokens or "unset"),
        _ck("Root volume encrypted", bool(rbd.get("encrypted")),
            "encrypted" if rbd.get("encrypted") else "NOT encrypted"),
    ]


def _azure_storage_policy(i: dict, resources=None) -> list[dict]:
    acct = _after(resources, "azurerm_storage_account")
    if acct is None:
        return [_todo("Minimum TLS 1.2"), _todo("No public blob access"), _todo("Approved replication")]
    tls = acct.get("min_tls_version")
    return [
        _ck("Minimum TLS 1.2", str(tls) in ("TLS1_2", "TLS1_3"), tls or "unset"),
        _ck("No public blob access", acct.get("allow_nested_items_to_be_public") is False,
            "public blobs disabled" if acct.get("allow_nested_items_to_be_public") is False else "public blobs ALLOWED"),
    ]


def _azure_rg_policy(i: dict, resources=None) -> list[dict]:
    return [_todo("Tagging policy applied"), _todo("Approved region")]


def _gcs_policy(i: dict, resources=None) -> list[dict]:
    b = _after(resources, "google_storage_bucket")
    if b is None:
        return [_todo("Uniform bucket-level access"), _todo("Versioning enabled"), _todo("force_destroy disabled")]
    return [
        _ck("Uniform bucket-level access", bool(b.get("uniform_bucket_level_access")),
            "uniform" if b.get("uniform_bucket_level_access") else "fine-grained ACLs"),
        _ck("force_destroy disabled", b.get("force_destroy") is False,
            "protected" if b.get("force_destroy") is False else "force_destroy ON"),
    ]


def _azure_vm_policy(i: dict, resources=None) -> list[dict]:
    return [
        _todo("SSH key auth (no password)"),
        _todo("Dedicated NSG (default-deny inbound)"),
        _todo("Managed OS disk"),
    ]


def _azure_pg_policy(i: dict, resources=None) -> list[dict]:
    return [
        _todo("TLS-enforced connections"),
        _todo("Server-managed admin password (generated)"),
        _ck("Approved PostgreSQL version", str(i.get("pg_version", "15")) in {"14", "15", "16"}, i.get("pg_version", "")),
    ]


def _azure_aks_policy(i: dict, resources=None) -> list[dict]:
    return [
        _todo("System-assigned managed identity"),
        _todo("Azure RBAC enabled"),
        _ck("Multi-node pool", int(i.get("node_count", 2)) >= 2, f"{i.get('node_count', 2)} nodes"),
    ]


def _gcp_gce_policy(i: dict, resources=None) -> list[dict]:
    return [
        _todo("SSH key auth (no password)"),
        _todo("Ingress restricted to declared ports"),
        _todo("Labelled ManagedBy=aegisops"),
    ]


def _gcp_gke_policy(i: dict, resources=None) -> list[dict]:
    return [
        _todo("Dedicated node pool (default removed)"),
        _todo("Deletion protection off (day-2 destroy)"),
        _ck("Multi-node pool", int(i.get("node_count", 2)) >= 2, f"{i.get('node_count', 2)} nodes"),
    ]


def _gcp_cloudsql_policy(i: dict, resources=None) -> list[dict]:
    return [
        _todo("Generated root password"),
        _ck("Approved engine (PostgreSQL)", str(i.get("database_version", "")).startswith("POSTGRES"), i.get("database_version", "")),
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


def by_key(key: str) -> WorkflowTemplate | None:
    """Approved template by its catalog key (e.g. "aws.vpc") — the exec loop's lookup."""
    return _BY_KEY.get(key)


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
