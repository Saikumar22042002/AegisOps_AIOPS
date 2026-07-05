"""Per-workflow input schemas (Pydantic) for multi-cloud provisioning.

The CloudOps agent extracts inputs from natural language (via Gemini) and/or parses free-form
(comma-separated / multiline key=value), then validates against the schema for the selected
template. Validation errors are returned to the user as actionable clarification.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, field_validator


def parse_freeform(text: str) -> dict[str, Any]:
    """Parse 'key=value, key2=value2' or multiline 'key: value' into a dict."""
    out: dict[str, Any] = {}
    if not text:
        return out
    # Split pairs on newlines/semicolons or a comma FOLLOWED BY whitespace, so a bare
    # "a,b,c" stays inside a single value (list) while "k=1, k2=2" splits into two pairs.
    parts = re.split(r"[\n;]|,(?=\s)", text)
    for part in parts:
        m = re.match(r"\s*([\w.\-]+)\s*[=:]\s*(.+?)\s*$", part)
        if m:
            key, val = m.group(1).strip(), m.group(2).strip()
            if "," in val and key.endswith(("s", "ids", "types", "zones")):
                out[key] = [v.strip() for v in val.split(",") if v.strip()]
            else:
                out[key] = val
    return out


class WorkflowInputs(BaseModel):
    model_config = {"extra": "ignore"}


# ── Per-cloud machine-shape validation (Phase 7 / BUG-02) ─────────────────────────────────────
# Screenshot 12/13: `machine_type = "ec2-micro"` reached a google_compute_instance plan — an
# AWS-style value must be rejected at validation (specific re-ask) and can never reach Terraform.
_AWS_INSTANCE_SHAPE = re.compile(r"[a-z]+[0-9]+[a-z]*\.[a-z0-9]+")            # t3.micro, m5.large
_GCP_MACHINE_SHAPE = re.compile(
    r"(?:e2|n1|n2|n2d|n4|c2|c2d|c3|c3d|c4|c4a|c4d|t2d|t2a|m1|m2|m3|m4|a2|a3|a4|g2|h3|z3|x4|f1|g1)"
    r"-[a-z0-9-]+|custom-\d+-\d+")                                            # e2-micro, n2-standard-2
_AZURE_SIZE_SHAPE = re.compile(r"(?:Standard|Basic)_[A-Za-z0-9_]+")           # Standard_B1s, Standard_D2s_v5


def _validate_gcp_machine_type(v: str) -> str:
    v = v.strip()
    if _AWS_INSTANCE_SHAPE.fullmatch(v) or v.lower().startswith("ec2"):
        raise ValueError(f"'{v}' looks like an AWS instance type — GCP machine types are e.g. "
                         "e2-micro, e2-medium, e2-standard-4, n2-standard-2")
    if not _GCP_MACHINE_SHAPE.fullmatch(v):
        raise ValueError(f"'{v}' is not a valid GCP machine type — expected e.g. e2-micro, "
                         "e2-medium, e2-standard-4, n2-standard-2")
    return v


def _validate_azure_size(v: str) -> str:
    v = v.strip()
    if _AWS_INSTANCE_SHAPE.fullmatch(v) or _GCP_MACHINE_SHAPE.fullmatch(v):
        raise ValueError(f"'{v}' looks like another cloud's machine type — Azure VM sizes are e.g. "
                         "Standard_B1s, Standard_B2s, Standard_D2s_v5")
    if not _AZURE_SIZE_SHAPE.fullmatch(v):
        raise ValueError(f"'{v}' is not a valid Azure VM size — expected e.g. Standard_B1s, "
                         "Standard_B2s, Standard_D2s_v5")
    return v


# ── AWS ──
class AWSS3Inputs(WorkflowInputs):
    bucket_name: str = Field(min_length=3, max_length=63)
    region: str = "us-east-1"
    versioning: bool = True
    block_public: bool = True

    @field_validator("bucket_name")
    @classmethod
    def _valid_bucket(cls, v: str) -> str:
        if not re.fullmatch(r"[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9]", v):
            raise ValueError("bucket_name must be lowercase, 3-63 chars, alphanumeric/.-")
        return v


class AWSVPCInputs(WorkflowInputs):
    name: str
    cidr_block: str = "10.0.0.0/16"
    region: str = "us-east-1"
    az_count: int = Field(default=3, ge=1, le=6)
    enable_nat: bool = True  # set false for sandboxes that disallow NAT gateways


class AWSEKSInputs(WorkflowInputs):
    cluster_name: str
    kubernetes_version: str = "1.29"
    vpc_id: str
    subnet_ids: list[str]
    instance_types: list[str] = Field(default_factory=lambda: ["m6i.xlarge"])
    desired_size: int = Field(default=3, ge=1, le=20)
    region: str = "us-east-1"


class AWSRDSInputs(WorkflowInputs):
    identifier: str
    engine: str = "postgres"
    instance_class: str = "db.t3.medium"
    allocated_storage: int = Field(default=20, ge=20, le=4096)
    region: str = "us-east-1"

    @field_validator("instance_class")
    @classmethod
    def _valid_class(cls, v: str) -> str:
        if not re.fullmatch(r"db\.[a-z0-9]+\.[a-z0-9]+", v.strip()):
            raise ValueError(f"'{v}' is not a valid RDS instance class — expected e.g. db.t3.medium")
        return v.strip()


EC2_OS_CHOICES = ("amazon-linux-2023", "ubuntu-22.04", "ubuntu-24.04", "windows-2022")


class AWSEC2Inputs(WorkflowInputs):
    name: str = "aegisops-vm"
    instance_type: str = "t3.micro"
    os: str = "amazon-linux-2023"
    ami: str = ""          # explicit AMI overrides the OS lookup when set
    subnet_id: str = ""    # auto-resolved to a default-VPC subnet when empty
    region: str = "us-east-1"
    key_name: str = ""            # existing key pair name (or created when create_key_pair=True)
    create_key_pair: bool = False
    # 0 = use the AMI's own root size (varies by image, e.g. AL2023 needs ≥30GB); override to grow it.
    root_volume_size: int = Field(default=0, ge=0, le=16384)
    root_volume_type: str = "gp3"
    ingress_ports: list[int] = Field(default_factory=list)  # inbound TCP ports on the managed SG (day-2)

    @field_validator("ingress_ports", mode="before")
    @classmethod
    def _coerce_ports(cls, v: Any) -> Any:
        # Accept "8501,8502" / ["8501","8502"] / [8501,8502] → [8501, 8502].
        if isinstance(v, str):
            v = [p.strip() for p in re.split(r"[,\s]+", v) if p.strip()]
        return [int(p) for p in v] if isinstance(v, (list, tuple)) else v

    @field_validator("os")
    @classmethod
    def _valid_os(cls, v: str) -> str:
        if v not in EC2_OS_CHOICES:
            raise ValueError(f"os must be one of {list(EC2_OS_CHOICES)}")
        return v

    @field_validator("instance_type")
    @classmethod
    def _valid_instance_type(cls, v: str) -> str:
        # Shape check (family.size, e.g. t3.micro, m5.large, c5.2xlarge). AWS validates existence at plan.
        if not re.fullmatch(r"[a-z]+[0-9]+[a-z]*\.[a-z0-9]+", v):
            raise ValueError(f"'{v}' is not a valid instance type — expected e.g. t3.micro, t3.large, m5.large")
        return v

    @field_validator("root_volume_type")
    @classmethod
    def _valid_vol_type(cls, v: str) -> str:
        if v not in {"gp2", "gp3", "io1", "io2", "standard"}:
            raise ValueError("root_volume_type must be one of gp2, gp3, io1, io2, standard")
        return v


# ── Azure ──
class AzureResourceGroupInputs(WorkflowInputs):
    name: str
    location: str = "eastus"


class AzureStorageInputs(WorkflowInputs):
    account_name: str = Field(min_length=3, max_length=24)
    resource_group: str
    location: str = "eastus"
    account_tier: str = "Standard"
    replication: str = "LRS"

    @field_validator("account_name")
    @classmethod
    def _valid_account(cls, v: str) -> str:
        if not re.fullmatch(r"[a-z0-9]{3,24}", v):
            raise ValueError("account_name must be 3-24 lowercase alphanumeric chars")
        return v


# ── GCP ──
class GCPGCSInputs(WorkflowInputs):
    bucket_name: str
    location: str = "US"
    project: str
    storage_class: str = "STANDARD"


# ── Azure (Phase 5) ──
class AzureVMInputs(WorkflowInputs):
    name: str
    location: str = "eastus"
    size: str = "Standard_B1s"
    os: str = "ubuntu-22.04"           # ubuntu-22.04 | ubuntu-24.04
    admin_username: str = "azureuser"
    resource_group: str = ""
    ingress_ports: list[int] = Field(default_factory=list)

    @field_validator("size")
    @classmethod
    def _valid_size(cls, v: str) -> str:
        return _validate_azure_size(v)

    @field_validator("os")
    @classmethod
    def _valid_os(cls, v: str) -> str:
        if v not in ("ubuntu-22.04", "ubuntu-24.04"):
            raise ValueError("os must be ubuntu-22.04 or ubuntu-24.04")
        return v

    @field_validator("ingress_ports", mode="before")
    @classmethod
    def _ports(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = [p.strip() for p in re.split(r"[,\s]+", v) if p.strip()]
        return [int(p) for p in v] if isinstance(v, (list, tuple)) else v


class AzurePostgresInputs(WorkflowInputs):
    name: str
    location: str = "eastus"
    admin_username: str = "pgadmin"
    sku_name: str = "B_Standard_B1ms"
    storage_mb: int = Field(default=32768, ge=32768)
    pg_version: str = "15"
    resource_group: str = ""


class AzureAKSInputs(WorkflowInputs):
    name: str
    location: str = "eastus"
    node_count: int = Field(default=2, ge=1, le=100)
    node_size: str = "Standard_B2s"
    kubernetes_version: str = ""
    resource_group: str = ""

    @field_validator("node_size")
    @classmethod
    def _valid_node_size(cls, v: str) -> str:
        return _validate_azure_size(v)


# ── GCP (Phase 5) ──
class GCPComputeInputs(WorkflowInputs):
    name: str
    project: str = ""                  # defaults to the configured GCP project
    region: str = "us-central1"
    zone: str = "us-central1-a"
    machine_type: str = "e2-micro"
    os: str = "debian-12"              # debian-12 | ubuntu-22.04 | ubuntu-24.04
    ssh_user: str = "aegis"
    ingress_ports: list[int] = Field(default_factory=list)

    @field_validator("machine_type")
    @classmethod
    def _valid_machine_type(cls, v: str) -> str:
        return _validate_gcp_machine_type(v)

    @field_validator("ingress_ports", mode="before")
    @classmethod
    def _ports(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = [p.strip() for p in re.split(r"[,\s]+", v) if p.strip()]
        return [int(p) for p in v] if isinstance(v, (list, tuple)) else v


class GCPGKEInputs(WorkflowInputs):
    name: str
    project: str = ""
    region: str = "us-central1"
    node_count: int = Field(default=2, ge=1, le=100)
    machine_type: str = "e2-medium"

    @field_validator("machine_type")
    @classmethod
    def _valid_machine_type(cls, v: str) -> str:
        return _validate_gcp_machine_type(v)


class GCPCloudSQLInputs(WorkflowInputs):
    name: str
    project: str = ""
    region: str = "us-central1"
    tier: str = "db-f1-micro"
    database_version: str = "POSTGRES_15"

    @field_validator("tier")
    @classmethod
    def _valid_tier(cls, v: str) -> str:
        if not re.fullmatch(r"db-[a-z0-9-]+", v.strip()):
            raise ValueError(f"'{v}' is not a valid Cloud SQL tier — expected e.g. db-f1-micro, "
                             "db-custom-2-8192")
        return v.strip()


# NOTE: the single source of truth mapping a template key → its input schema is the
# `WorkflowTemplate.schema` field in `agents/templates.py` (`templates._BY_KEY[key].schema`).
# There is deliberately no separate schema registry here and no generic/runtime-HCL schema —
# the arbitrary-module escape hatch was removed in the 2.3 Terraform-integrity audit.
