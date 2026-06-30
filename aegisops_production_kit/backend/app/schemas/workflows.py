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


class AWSEC2Inputs(WorkflowInputs):
    name: str = "aegisops-vm"
    instance_type: str = "t3.micro"
    ami: str = ""          # auto-resolved to latest Amazon Linux 2023 when empty
    subnet_id: str = ""    # auto-resolved to a default-VPC subnet when empty
    region: str = "us-east-1"


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


# ── Generic (any approved/published Terraform module) ──
class GenericModuleInputs(WorkflowInputs):
    source: str
    version: str | None = None
    variables: dict[str, Any] = Field(default_factory=dict)


SCHEMAS: dict[str, type[WorkflowInputs]] = {
    "aws.s3": AWSS3Inputs,
    "aws.vpc": AWSVPCInputs,
    "aws.eks": AWSEKSInputs,
    "aws.rds": AWSRDSInputs,
    "aws.ec2": AWSEC2Inputs,
    "azure.resource_group": AzureResourceGroupInputs,
    "azure.storage": AzureStorageInputs,
    "gcp.gcs": GCPGCSInputs,
    "generic.module": GenericModuleInputs,
}
