"""Cloud-agnostic parameter-collection framework (Phase 3).

For each REAL module in the inventory, a list of `ParamSpec` declares its parameters mapped to
the module's Terraform variables: which are decision-critical (ask the user if missing) vs which
have a safe, documented default (used silently, overridable). The CloudOps agent uses this to:
  1. detect which required params are still missing,
  2. ask the user only for those (never for defaulted ones like VPC/subnet),
  3. transform collected values into Terraform variables,
  4. validate against the module's Pydantic schema before planning.

Adding a new module = add its ParamSpec list here (+ its Pydantic schema + Terraform workspace).
Interactive collection spans chat turns via a Redis-backed pending record keyed by session.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..cache.redis import get_redis

_PENDING_TTL = 1800  # 30 min


@dataclass
class ParamSpec:
    name: str                      # maps to a Terraform variable (or a collection alias, e.g. key_pair)
    label: str                     # human label shown in the request UI
    kind: str = "string"           # string | choice | int | bool
    required: bool = False         # decision-critical: ask if missing (no safe default)
    choices: list[str] | None = None
    default: Any = None            # documented default for optional params (overridable)
    help: str = ""                 # example / guidance shown to the user
    secret: bool = False           # mask in logs/UI


# ── Per-module parameter declarations (REAL modules only) ─────────────────────────────────────
PARAMS: dict[str, list[ParamSpec]] = {
    "aws.ec2": [
        ParamSpec("name", "Instance name", required=True, help="e.g. web-server-01"),
        ParamSpec("instance_type", "Instance type", required=True, help="e.g. t3.micro, t3.large, m5.large"),
        ParamSpec("os", "Operating system", kind="choice", required=True,
                  choices=["amazon-linux-2023", "ubuntu-22.04", "ubuntu-24.04", "windows-2022"],
                  help="Amazon Linux 2023, Ubuntu 22.04/24.04, or Windows 2022"),
        ParamSpec("key_pair", "SSH key pair", required=True,
                  help="an existing EC2 key pair name, or say 'create' to have one generated"),
        ParamSpec("allowed_cidr", "Allowed source IP/CIDR", required=True,
                  help="your public IP (e.g. 203.0.113.7) to open SSH/RDP for it, or 'none' to keep "
                       "remote access closed"),
        # Defaulted / overridable — never asked:
        ParamSpec("region", "Region", help="defaults to the selected region"),
        ParamSpec("vpc_id", "VPC", help="defaults to the account's default VPC"),
        ParamSpec("subnet_id", "Subnet", help="defaults to a default-VPC subnet"),
        ParamSpec("root_volume_size", "Root volume size (GiB)", kind="int", default=None,
                  help="defaults to the image's own size; set to grow it"),
        ParamSpec("root_volume_type", "Root volume type", kind="choice",
                  choices=["gp2", "gp3", "io1", "io2", "standard"], default="gp3"),
    ],
    "aws.s3": [
        ParamSpec("bucket_name", "Bucket name", required=True, help="globally-unique, lowercase, 3–63 chars"),
        ParamSpec("region", "Region", help="defaults to the selected region"),
        ParamSpec("versioning", "Versioning", kind="bool", default=True),
        ParamSpec("block_public", "Block public access", kind="bool", default=True),
    ],
    "aws.rds": [
        ParamSpec("identifier", "DB identifier", required=True, help="e.g. payments-db"),
        ParamSpec("engine", "Engine", kind="choice",
                  choices=["postgres", "mysql", "mariadb"], default="postgres"),
        ParamSpec("engine_version", "Engine version", default="",
                  help='empty = provider default, "latest" = newest available, or a pin'),
        ParamSpec("instance_class", "Instance class", default="db.t3.medium", help="e.g. db.t3.medium"),
        ParamSpec("allocated_storage", "Storage (GiB)", kind="int", default=20),
        ParamSpec("region", "Region", help="defaults to the selected region"),
        ParamSpec("allowed_cidr", "Client CIDR (dedicated SG)", default="",
                  help="set to get a dedicated DB security group; a /0 CIDR is rejected"),
        ParamSpec("enable_log_exports", "CloudWatch log exports", kind="bool", default=False,
                  help="engine-aware log exports + query-logging parameter group (B2: off for existing)"),
    ],
    "aws.vpc": [
        ParamSpec("name", "VPC name", required=True, help="e.g. prod-network"),
        ParamSpec("cidr_block", "CIDR block", default="10.0.0.0/16"),
        ParamSpec("az_count", "AZ count", kind="int", default=3),
        ParamSpec("enable_nat", "NAT gateway", kind="bool", default=True),
        ParamSpec("region", "Region", help="defaults to the selected region"),
    ],
    "aws.eks": [
        ParamSpec("cluster_name", "Cluster name", required=True, help="e.g. payments-eks"),
        ParamSpec("vpc_id", "Existing VPC id", required=True, help="EKS reuses an existing VPC (vpc-…)"),
        ParamSpec("subnet_ids", "Private subnet ids", required=True, help="comma-separated subnet-… ids (≥2)"),
        ParamSpec("kubernetes_version", "Kubernetes version", default="1.29"),
        ParamSpec("instance_types", "Node instance types", default=["m6i.xlarge"]),
        ParamSpec("desired_size", "Node desired size", kind="int", default=3),
        ParamSpec("region", "Region", help="defaults to the selected region"),
    ],
    "azure.resource_group": [
        ParamSpec("name", "Resource group name", required=True, help="e.g. rg-payments"),
        ParamSpec("location", "Location", default="eastus"),
    ],
    "azure.storage": [
        ParamSpec("account_name", "Storage account name", required=True,
                  help="globally-unique, 3–24 lowercase alphanumeric chars"),
        ParamSpec("resource_group", "Resource group", required=True, help="an existing resource group name"),
        ParamSpec("location", "Location", default="eastus"),
        ParamSpec("account_tier", "Account tier", kind="choice", choices=["Standard", "Premium"], default="Standard"),
        ParamSpec("replication", "Replication", kind="choice",
                  choices=["LRS", "GRS", "ZRS", "RAGRS"], default="LRS"),
    ],
    "gcp.gcs": [
        ParamSpec("bucket_name", "Bucket name", required=True, help="globally-unique, lowercase"),
        ParamSpec("project", "GCP project id", help="defaults to the configured project"),
        ParamSpec("location", "Location", default="US"),
        ParamSpec("storage_class", "Storage class", kind="choice",
                  choices=["STANDARD", "NEARLINE", "COLDLINE", "ARCHIVE"], default="STANDARD"),
    ],
    # ── Azure (Phase 5) ──
    "azure.vm": [
        ParamSpec("name", "VM name", required=True, help="e.g. web-vm-01"),
        ParamSpec("size", "VM size", required=True,
                  help="B/D/E-series the subscription allows — e.g. Standard_B1s, Standard_B2s, "
                       "Standard_D2s_v5, Standard_E2s_v5"),
        ParamSpec("os", "Operating system", kind="choice", required=True,
                  choices=["ubuntu-22.04", "ubuntu-24.04", "debian-12", "windows-2022"],
                  help="Ubuntu/Debian (SSH key auto-generated) or Windows Server 2022 "
                       "(admin password auto-generated)"),
        ParamSpec("allowed_cidr", "Allowed source IP/CIDR", required=True,
                  help="your public IP (e.g. 203.0.113.7) to open SSH/RDP for it, or 'none' to keep "
                       "remote access closed"),
        ParamSpec("location", "Location", default="eastus"),
        ParamSpec("admin_username", "Admin username", default="azureuser"),
        ParamSpec("resource_group", "Resource group", help="created as <name>-rg if omitted"),
        ParamSpec("ingress_ports", "Inbound ports", kind="int", default=None, help="optional; day-2 modifiable"),
    ],
    "azure.db": [
        ParamSpec("name", "Server name", required=True, help="e.g. payments-pg (globally-unique)"),
        ParamSpec("engine", "Engine", kind="choice",
                  choices=["postgresql", "mysql", "mssql"], default="postgresql"),
        ParamSpec("location", "Location", default="eastus"),
        ParamSpec("admin_username", "Admin username", default="pgadmin"),
        ParamSpec("pg_version", "PostgreSQL version", kind="choice", choices=["14", "15", "16"], default="15"),
        ParamSpec("sku_name", "SKU", default="B_Standard_B1ms"),
        ParamSpec("ha_enabled", "Zone-redundant HA", kind="bool", default=False,
                  help="postgresql/mysql flexible servers (B2: off for existing)"),
        ParamSpec("geo_redundant_backup", "Geo-redundant backup", kind="bool", default=False,
                  help="B2: off for existing resources; module default is on"),
        ParamSpec("delegated_subnet_id", "Delegated subnet (private access)", default="",
                  help="pair with a private DNS zone; postgresql/mysql only"),
    ],
    "azure.aks": [
        ParamSpec("name", "Cluster name", required=True, help="e.g. payments-aks"),
        ParamSpec("location", "Location", default="eastus"),
        ParamSpec("node_count", "Node count", kind="int", default=2),
        ParamSpec("node_size", "Node size", default="Standard_B2s"),
    ],
    # ── GCP (Phase 5) ──
    "gcp.vm": [
        ParamSpec("name", "Instance name", required=True, help="e.g. web-01"),
        ParamSpec("machine_type", "Machine type", required=True, help="e.g. e2-micro, e2-medium, n2-standard-2"),
        ParamSpec("os", "Operating system", kind="choice", required=True,
                  choices=["debian-12", "ubuntu-22.04", "ubuntu-24.04"], help="SSH key auto-generated"),
        ParamSpec("allowed_cidr", "Allowed source IP/CIDR", required=True,
                  help="your public IP (e.g. 203.0.113.7) to open SSH for it, or 'none' to keep "
                       "remote access closed"),
        ParamSpec("project", "GCP project id", help="defaults to the configured project"),
        ParamSpec("region", "Region", default="us-central1"),
        ParamSpec("zone", "Zone", default="us-central1-a"),
        ParamSpec("ssh_user", "SSH user", default="aegis"),
        ParamSpec("ingress_ports", "Inbound ports", kind="int", default=None, help="optional; day-2 modifiable"),
    ],
    "gcp.gke": [
        ParamSpec("name", "Cluster name", required=True, help="e.g. payments-gke"),
        ParamSpec("project", "GCP project id", help="defaults to the configured project"),
        ParamSpec("region", "Region", default="us-central1"),
        ParamSpec("node_count", "Node count", kind="int", default=2),
        ParamSpec("machine_type", "Node machine type", default="e2-medium"),
    ],
    "azure.keyvault": [
        ParamSpec("name", "Vault name", required=True, help="globally unique, 3-24 chars"),
        ParamSpec("location", "Region", default="eastus"),
        ParamSpec("purge_protection", "Purge protection", kind="bool", default=True),
    ],
    "aws.kms": [
        ParamSpec("name", "Key name", required=True, help="e.g. app-secrets (alias becomes alias/<name>)"),
        ParamSpec("deletion_window", "Deletion window (days)", kind="int", default=30),
        ParamSpec("enable_rotation", "Annual rotation", kind="bool", default=True),
    ],
    "aws.nlb": [
        ParamSpec("name", "Load balancer name", required=True, help="e.g. web-lb"),
        ParamSpec("target_port", "Target port", kind="int", default=80),
        ParamSpec("internal", "Internal only", kind="bool", default=False),
    ],
    "azure.vnet": [
        ParamSpec("name", "VNet name", required=True, help="e.g. prod-vnet"),
        ParamSpec("location", "Region", default="eastus"),
        ParamSpec("resource_group", "Resource group", help="defaults to '<name>-rg' (auto-created)"),
    ],
    "gcp.kms": [
        ParamSpec("name", "Key ring name", required=True, help="permanent in GCP; default key '<name>-key'"),
        ParamSpec("region", "Location", default="us-central1"),
        ParamSpec("rotation_days", "Rotation (days)", kind="int", default=90),
    ],
    "gcp.vpc": [
        ParamSpec("name", "Network name", required=True, help="e.g. prod-network"),
        ParamSpec("region", "Region", default="us-central1"),
        ParamSpec("enable_nat", "Cloud NAT for private egress", kind="bool", default=True),
    ],
    "gcp.cloudsql": [
        ParamSpec("name", "Instance name", required=True, help="e.g. payments-sql (globally-unique)"),
        ParamSpec("project", "GCP project id", help="defaults to the configured project"),
        ParamSpec("region", "Region", default="us-central1"),
        ParamSpec("tier", "Machine tier", default="db-f1-micro"),
        ParamSpec("database_version", "Engine version", default="POSTGRES_15"),
    ],
}


def specs_for(template_key: str) -> list[ParamSpec]:
    return PARAMS.get(template_key, [])


def _present(value: Any) -> bool:
    return value not in (None, "", [], {})


def missing_required(template_key: str, collected: dict) -> list[ParamSpec]:
    """Decision-critical params with no value yet (these — and only these — are asked for)."""
    return [p for p in specs_for(template_key) if p.required and not _present(collected.get(p.name))]


def request_payload(template_key: str, collected: dict) -> dict:
    """Structured param-request for the UI + a natural-language summary of what's still needed."""
    missing = missing_required(template_key, collected)
    items = [{"name": p.name, "label": p.label, "kind": p.kind, "choices": p.choices, "help": p.help}
             for p in missing]
    return {"template": template_key, "items": items,
            "collected": {k: v for k, v in collected.items() if _present(v)}}


def summary_text(template_key: str, collected: dict) -> str:
    """Human message asking only for the missing decision-critical params."""
    missing = missing_required(template_key, collected)
    lines = [f"• **{p.label}** — {p.help}" if p.help else f"• **{p.label}**" for p in missing]
    have = [f"{p.label} = {collected[p.name]}" for p in specs_for(template_key)
            if _present(collected.get(p.name)) and p.required]
    head = f"To provision this ({template_key}), I need a few details:" if not have \
        else "Thanks — I still need:"
    tail = ("\n\nDefaults (region, VPC/subnet, volume) are applied automatically — just name them if you "
            "want to override.") if template_key == "aws.ec2" else ""
    got = f"\n\nGot so far: {', '.join(have)}." if have else ""
    return head + "\n" + "\n".join(lines) + got + tail


# ── Collected params → Terraform variables (module-specific transforms) ───────────────────────
def _ec2_to_tf(collected: dict) -> dict:
    out = dict(collected)
    kp = str(out.pop("key_pair", "") or out.get("key_name", "") or "").strip()
    if kp.lower() in {"create", "generate", "new", "make one", "create one", "auto", "yes"}:
        out["create_key_pair"] = True
        out["key_name"] = (out.get("key_name") or f"{out.get('name', 'aegisops')}-key").strip()
    elif kp:
        out["create_key_pair"] = False
        out["key_name"] = kp
    return out


_TF_TRANSFORMS: dict[str, Callable[[dict], dict]] = {"aws.ec2": _ec2_to_tf}


def to_tf_vars(template_key: str, collected: dict) -> dict:
    """Map validated collection params to the module's Terraform variables."""
    transform = _TF_TRANSFORMS.get(template_key)
    return transform(collected) if transform else dict(collected)


def extraction_fields(template_key: str) -> str:
    """Guidance for the LLM extractor: which fields to pull + allowed values."""
    parts = []
    for p in specs_for(template_key):
        seg = p.name
        if p.choices:
            seg += f" (one of: {', '.join(p.choices)})"
        if p.help:
            seg += f" [{p.help}]"
        parts.append(seg)
    return "; ".join(parts)


# ── Pending multi-turn collection (Redis, session-scoped) ─────────────────────────────────────
def _key(session_id: str) -> str:
    return f"pending:collect:{session_id}"


async def save_pending(session_id: str, data: dict) -> None:
    await get_redis().set(_key(session_id), json.dumps(data), ex=_PENDING_TTL)


async def load_pending(session_id: str) -> dict | None:
    raw = await get_redis().get(_key(session_id))
    return json.loads(raw) if raw else None


async def clear_pending(session_id: str) -> None:
    await get_redis().delete(_key(session_id))
