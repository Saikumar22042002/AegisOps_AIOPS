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
    # MODSEED: honest per-module deletion semantics, surfaced on the destroy approval card
    # (e.g. KMS keys enter a scheduled-deletion window; GCP key rings are not deletable).
    destroy_note: str | None = None

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
    # MS-7: the dedicated DB SG must never be world-open (the module rejects /0 outright;
    # this check re-proves it on the REAL plan when the SG path is used).
    sg = _after(resources, "aws_security_group")
    if sg is not None:
        cidrs = [c for rule in (sg.get("ingress") or []) for c in (rule.get("cidr_blocks") or [])]
        world_open = any(str(c).endswith("/0") for c in cidrs)
        checks.append(_ck("DB security group scoped (no /0)", not world_open,
                          ", ".join(map(str, cidrs)) or "no ingress"))
    elif i.get("allowed_cidr"):
        checks.append(_ck("DB security group scoped (no /0)",
                          not str(i["allowed_cidr"]).endswith("/0"), str(i["allowed_cidr"])))
    if i.get("enable_log_exports"):
        checks.append(_ck("Engine-aware log exports", True,
                          f"CloudWatch exports + query-logging parameter group ({i.get('engine', 'postgres')})"))
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


def _aws_nlb_policy(i: dict, resources=None) -> list[dict]:
    """MODSEED MS-3: over the real plan - network type, cross-zone on, TCP health checks,
    deletion-protection state matches the resolved input (Production defaults it ON)."""
    lb = _after(resources, "aws_lb")
    tg = _after(resources, "aws_lb_target_group")
    checks: list[dict] = []
    if lb is not None:
        checks.append(_ck("Network load balancer", lb.get("load_balancer_type") == "network",
                          str(lb.get("load_balancer_type"))))
        checks.append(_ck("Cross-zone load balancing on",
                          lb.get("enable_cross_zone_load_balancing") is True,
                          "on" if lb.get("enable_cross_zone_load_balancing") else "OFF"))
        want_dp = bool(i.get("deletion_protection"))
        have_dp = bool(lb.get("enable_deletion_protection"))
        checks.append(_ck("Deletion protection as approved", have_dp == want_dp,
                          f"planned {'on' if have_dp else 'off'} (requested {'on' if want_dp else 'off'})"))
    else:
        checks.append(_todo("Network load balancer"))
        checks.append(_todo("Cross-zone load balancing on"))
        checks.append(_todo("Deletion protection as approved"))
    if tg is not None:
        hc = _block0(tg, "health_check")
        checks.append(_ck("TCP health checks (30s, threshold 3)",
                          hc.get("protocol") == "TCP" and hc.get("interval") == 30
                          and hc.get("healthy_threshold") == 3,
                          f"{hc.get('protocol')}/{hc.get('interval')}s/x{hc.get('healthy_threshold')}"))
    else:
        checks.append(_todo("TCP health checks (30s, threshold 3)"))
    return checks


def _aws_kms_policy(i: dict, resources=None) -> list[dict]:
    """MODSEED MS-4: over the real plan - rotation ON and a deletion window of at least 7 days."""
    key = _after(resources, "aws_kms_key")
    checks: list[dict] = []
    if key is not None:
        checks.append(_ck("Key rotation enabled", key.get("enable_key_rotation") is True,
                          "annual rotation on" if key.get("enable_key_rotation") else "rotation OFF"))
        window = key.get("deletion_window_in_days")
        checks.append(_ck("Deletion window >= 7 days", isinstance(window, int) and window >= 7,
                          f"{window} days"))
    else:
        checks.append(_ck("Key rotation enabled", bool(i.get("enable_rotation", True))))
        checks.append(_ck("Deletion window >= 7 days", int(i.get("deletion_window", 30)) >= 7,
                          f"{i.get('deletion_window', 30)} days requested"))
    checks.append(_todo("Key policy: root admin + allowed services only"))
    return checks


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


def _azure_keyvault_policy(i: dict, resources=None) -> list[dict]:
    """MODSEED MS-5: over the real plan - soft-delete >=7, purge protection as requested,
    AzureServices bypass on the network ACLs. No secret values ever pass through here."""
    kv = _after(resources, "azurerm_key_vault")
    checks: list[dict] = []
    if kv is not None:
        days = kv.get("soft_delete_retention_days")
        checks.append(_ck("Soft delete >= 7 days", isinstance(days, int) and days >= 7,
                          f"{days} days"))
        want_pp = bool(i.get("purge_protection", True))
        checks.append(_ck("Purge protection as approved",
                          bool(kv.get("purge_protection_enabled")) == want_pp,
                          f"planned {'on' if kv.get('purge_protection_enabled') else 'off'}"))
        acls = _block0(kv, "network_acls")
        checks.append(_ck("AzureServices bypass on network ACLs",
                          acls.get("bypass") == "AzureServices", str(acls.get("bypass"))))
    else:
        checks.append(_ck("Soft delete >= 7 days", int(i.get("soft_delete_days", 90)) >= 7,
                          f"{i.get('soft_delete_days', 90)} days requested"))
        checks.append(_todo("Purge protection as approved"))
        checks.append(_todo("AzureServices bypass on network ACLs"))
    return checks


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


def _azure_vnet_policy(i: dict, resources=None) -> list[dict]:
    """MODSEED MS-2: over the real plan - >=1 subnet and an RFC1918 address space; the module
    ships NO NSG (no admin ingress surface here at all)."""
    import ipaddress
    vnet = _after(resources, "azurerm_virtual_network")
    checks: list[dict] = []
    if vnet is not None:
        subnet_count = sum(1 for r in (resources or [])
                           if r.get("type") == "azurerm_subnet")
        checks.append(_ck("At least one subnet", subnet_count >= 1,
                          f"{subnet_count} subnet(s) in the plan"))
        spaces = vnet.get("address_space") or []
        try:
            private = all(ipaddress.ip_network(c).is_private for c in spaces) and bool(spaces)
        except ValueError:
            private = False
        checks.append(_ck("RFC1918 address space", private, ", ".join(spaces) or "none"))
        nsg_count = sum(1 for r in (resources or [])
                        if str(r.get("type", "")).startswith("azurerm_network_security"))
        checks.append(_ck("No NSG in the network module (no admin ingress here)", nsg_count == 0,
                          f"{nsg_count} NSG resource(s) planned"))
    else:
        checks.append(_ck("At least one subnet", len(i.get("subnet_cidrs", [])) >= 1,
                          f"{len(i.get('subnet_cidrs', []))} requested"))
        checks.append(_todo("RFC1918 address space"))
        checks.append(_todo("No NSG in the network module"))
    return checks


def _azure_vm_policy(i: dict, resources=None) -> list[dict]:
    return [
        _todo("SSH key auth (no password)"),
        _todo("Dedicated NSG (default-deny inbound)"),
        _todo("Managed OS disk"),
    ]


def _azure_db_policy(i: dict, resources=None) -> list[dict]:
    """MS-8 multi-engine (postgresql/mysql/mssql). Keeps the pre-enhancement postgres
    checks verbatim and adds engine-aware statements."""
    engine = str(i.get("engine", "postgresql"))
    checks = [
        _todo("TLS-enforced connections"),
        _todo("Server-managed admin password (generated)"),
        _ck("Approved engine", engine in {"postgresql", "mysql", "mssql"}, engine),
    ]
    if engine == "postgresql":
        checks.append(_ck("Approved PostgreSQL version",
                          str(i.get("pg_version", "15")) in {"14", "15", "16"},
                          i.get("pg_version", "")))
    if engine == "mssql":
        mssql = _after(resources, "azurerm_mssql_server")
        if mssql is not None:
            checks.append(_ck("TLS 1.2 minimum (SQL Server)",
                              mssql.get("minimum_tls_version") == "1.2",
                              str(mssql.get("minimum_tls_version"))))
    if i.get("ha_enabled"):
        checks.append(_ck("Zone-redundant HA", engine in {"postgresql", "mysql"},
                          f"ZoneRedundant ({engine})"))
    if i.get("delegated_subnet_id"):
        checks.append(_ck("Private access (delegated subnet)", bool(i.get("private_dns_zone_id")),
                          "delegated subnet + private DNS" if i.get("private_dns_zone_id")
                          else "delegated subnet WITHOUT a private DNS zone"))
    return checks


def _azure_aks_policy(i: dict, resources=None) -> list[dict]:
    return [
        _todo("System-assigned managed identity"),
        _todo("Azure RBAC enabled"),
        _ck("Multi-node pool", int(i.get("node_count", 2)) >= 2, f"{i.get('node_count', 2)} nodes"),
    ]


def _gcp_vpc_policy(i: dict, resources=None) -> list[dict]:
    """MODSEED MS-1: over the real plan — the network must be custom-mode (no auto subnets)
    and carry at least one explicit subnet."""
    net = _after(resources, "google_compute_network")
    checks: list[dict] = []
    if net is not None:
        auto = net.get("auto_create_subnetworks")
        checks.append(_ck("Custom-mode network (no auto subnets)", auto is False,
                          "custom mode" if auto is False else "AUTO subnet mode"))
        subnet_count = sum(1 for r in (resources or []) if r.get("type") == "google_compute_subnetwork")
        checks.append(_ck("At least one explicit subnet", subnet_count >= 1,
                          f"{subnet_count} subnet(s) in the plan"))
    else:
        checks.append(_todo("Custom-mode network (no auto subnets)"))
        checks.append(_ck("At least one explicit subnet", len(i.get("subnet_cidrs", [])) >= 1,
                          f"{len(i.get('subnet_cidrs', []))} requested"))
    checks.append(_todo("Internal firewall scoped to subnet CIDRs (no admin ingress here)"))
    return checks


def _gcp_kms_policy(i: dict, resources=None) -> list[dict]:
    """MODSEED MS-6: over the real plan - rotation configured, SOFTWARE protection,
    ENCRYPT_DECRYPT purpose."""
    key = _after(resources, "google_kms_crypto_key")
    checks: list[dict] = []
    if key is not None:
        rp = str(key.get("rotation_period") or "")
        try:
            rot_ok = rp.endswith("s") and int(rp[:-1]) >= 86400
        except ValueError:
            rot_ok = False
        checks.append(_ck("Automatic rotation configured", rot_ok, rp or "none"))
        vt = _block0(key, "version_template")
        checks.append(_ck("SOFTWARE protection level", vt.get("protection_level") == "SOFTWARE",
                          str(vt.get("protection_level"))))
        checks.append(_ck("ENCRYPT_DECRYPT purpose", key.get("purpose") == "ENCRYPT_DECRYPT",
                          str(key.get("purpose"))))
    else:
        checks.append(_ck("Automatic rotation configured", int(i.get("rotation_days", 90)) >= 1,
                          f"{i.get('rotation_days', 90)} days requested"))
        checks.append(_todo("SOFTWARE protection level"))
        checks.append(_todo("ENCRYPT_DECRYPT purpose"))
    return checks


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
    checks = [
        _todo("Generated root password"),
        _ck("Approved engine (PostgreSQL)", str(i.get("database_version", "")).startswith("POSTGRES"), i.get("database_version", "")),
    ]
    # MS-9: a world-open authorized network fails VISIBLY (the legacy default is exactly
    # that — the approver sees it; the private path or a scoped CIDR list passes).
    if i.get("private_network"):
        checks.append(_ck("Network exposure", True, "private VPC peering (no public IP)"))
    else:
        nets = [str(n) for n in (i.get("authorized_networks") or [])]
        world_open = any(n.endswith("/0") for n in nets)
        checks.append(_ck("No world-open authorized networks", not world_open,
                          ", ".join(nets) or "none"))
    if i.get("backup_enabled"):
        checks.append(_ck("Automated backups + PITR", True, "daily backups, point-in-time recovery"))
    if i.get("encryption_key_name"):
        checks.append(_ck("CMEK encryption", True, str(i["encryption_key_name"])))
    return checks


# Every template is a curated, org-approved, version-controlled Terraform workspace. There is
# deliberately NO runtime-HCL / arbitrary-module escape hatch: the LLM only selects a template
# and passes variables — it never authors or templates HCL (see 2.3 integrity audit).
TEMPLATES: list[WorkflowTemplate] = [
    WorkflowTemplate("aws.s3", "aws", "s3", "v1", "aws-s3", wf.AWSS3Inputs, "Provision an S3 bucket (encrypted, private)", _s3_policy),
    WorkflowTemplate("aws.vpc", "aws", "vpc", "v1", "aws-vpc", wf.AWSVPCInputs, "Provision a VPC with public/private subnets + NAT", _vpc_policy),
    WorkflowTemplate("aws.eks", "aws", "eks", "v3", "eks-provision", wf.AWSEKSInputs, "Provision a hardened EKS cluster reusing an existing VPC", _eks_policy),
    WorkflowTemplate("aws.rds", "aws", "rds", "v1", "aws-rds", wf.AWSRDSInputs, "Provision an encrypted RDS instance", _rds_policy),
    WorkflowTemplate("aws.ec2", "aws", "ec2", "v1", "aws-ec2", wf.AWSEC2Inputs, "Provision an EC2 instance (IMDSv2, encrypted)", _ec2_policy),
    WorkflowTemplate("aws.nlb", "aws", "nlb", "v1", "aws-nlb", wf.AWSNLBInputs, "Provision a network load balancer (TCP target group + listener)", _aws_nlb_policy),
    WorkflowTemplate("aws.kms", "aws", "kms", "v1", "aws-kms", wf.AWSKMSInputs, "Provision a KMS key (rotation on, alias, service policy)", _aws_kms_policy,
                     destroy_note="A destroyed KMS key enters its scheduled-deletion window (the module's deletion_window, 7-30 days) - it is NOT removed immediately and remains recoverable until the window elapses."),
    WorkflowTemplate("azure.storage", "azure", "storage", "v1", "azure-storage", wf.AzureStorageInputs, "Provision an Azure Storage Account", _azure_storage_policy),
    WorkflowTemplate("azure.vnet", "azure", "vnet", "v1", "azure-vnet", wf.AzureVNetInputs, "Provision an Azure VNet (subnets, NAT gateway, route tables)", _azure_vnet_policy),
    WorkflowTemplate("azure.keyvault", "azure", "keyvault", "v1", "azure-keyvault", wf.AzureKeyVaultInputs, "Provision an Azure Key Vault (soft delete, purge protection, optional RSA keys)", _azure_keyvault_policy,
                     destroy_note="A destroyed Key Vault enters soft-delete retention (the module's soft_delete_days) - with purge protection ON it CANNOT be permanently purged until the window elapses; the name stays reserved meanwhile."),
    WorkflowTemplate("azure.resource_group", "azure", "resource_group", "v1", "azure-resource-group", wf.AzureResourceGroupInputs, "Provision an Azure Resource Group", _azure_rg_policy),
    WorkflowTemplate("azure.vm", "azure", "vm", "v1", "azure-vm", wf.AzureVMInputs, "Provision an Azure Linux VM (generated SSH key)", _azure_vm_policy),
    WorkflowTemplate("azure.db", "azure", "db", "v2", "azure-postgres", wf.AzureDBInputs, "Provision an Azure database (PostgreSQL/MySQL flexible server or SQL Server)", _azure_db_policy),
    WorkflowTemplate("azure.aks", "azure", "aks", "v1", "azure-aks", wf.AzureAKSInputs, "Provision an Azure Kubernetes Service (AKS) cluster", _azure_aks_policy),
    WorkflowTemplate("gcp.gcs", "gcp", "gcs", "v1", "gcp-gcs", wf.GCPGCSInputs, "Provision a GCS bucket (uniform access, versioned)", _gcs_policy),
    WorkflowTemplate("gcp.vpc", "gcp", "vpc", "v1", "gcp-vpc", wf.GCPVPCInputs, "Provision a custom-mode GCP VPC (subnets + secondary ranges, NAT, internal firewall)", _gcp_vpc_policy),
    WorkflowTemplate("gcp.kms", "gcp", "kms", "v1", "gcp-kms", wf.GCPKMSInputs, "Provision a KMS key ring + crypto keys (90-day rotation)", _gcp_kms_policy,
                     destroy_note="GCP key rings are NOT deletable - destroying this removes the crypto-key versions and IAM bindings only; the ring (and key names within it) remain reserved in the project permanently."),
    WorkflowTemplate("gcp.vm", "gcp", "vm", "v1", "gcp-gce", wf.GCPComputeInputs, "Provision a GCP Compute Engine VM (generated SSH key)", _gcp_gce_policy),
    WorkflowTemplate("gcp.gke", "gcp", "gke", "v1", "gcp-gke", wf.GCPGKEInputs, "Provision a GKE cluster", _gcp_gke_policy),
    WorkflowTemplate("gcp.cloudsql", "gcp", "cloudsql", "v1", "gcp-cloudsql", wf.GCPCloudSQLInputs, "Provision a Cloud SQL for PostgreSQL instance", _gcp_cloudsql_policy),
]

_BY_KEY = {t.key: t for t in TEMPLATES}
# MS-8 (B3 backcompat): `azure.postgres` became the multi-engine `azure.db`; the old key
# stays resolvable so anything holding it (stored refs, older clients) keeps working.
_BY_KEY["azure.postgres"] = _BY_KEY["azure.db"]

# Per-cloud resource synonyms → the canonical resource key for that cloud. Lets a generic word
# ("vm", "database", "k8s") resolve to the right cloud-specific module (aws.ec2 vs azure.vm vs
# gcp.vm; aws.rds vs azure.postgres vs gcp.cloudsql; aws.eks vs azure.aks vs gcp.gke).
_SYNONYMS: dict[str, dict[str, str]] = {
    "aws": {"vm": "ec2", "instance": "ec2", "server": "ec2", "compute": "ec2", "database": "rds",
            "db": "rds", "postgres": "rds", "postgresql": "rds", "mysql": "rds", "sql": "rds",
            "k8s": "eks", "kubernetes": "eks", "cluster": "eks", "bucket": "s3", "blob": "s3",
            "object_storage": "s3", "network": "vpc",
            "lb": "nlb", "load_balancer": "nlb", "loadbalancer": "nlb",
            "key": "kms", "encryption_key": "kms", "secrets": "kms"},
    "azure": {"instance": "vm", "server": "vm", "compute": "vm", "ec2": "vm", "database": "db",
              "postgres": "db", "postgresql": "db", "sql": "db", "mysql": "db",
              "mssql": "db", "sqlserver": "db", "sql_server": "db",
              "k8s": "aks", "kubernetes": "aks", "cluster": "aks", "blob": "storage", "bucket": "storage",
              "object_storage": "storage", "storage_account": "storage", "rg": "resource_group", "network": "vnet",
              "key_vault": "keyvault", "kv": "keyvault", "vault": "keyvault"},
    "gcp": {"instance": "vm", "server": "vm", "compute": "vm", "gce": "vm", "ec2": "vm",
            "network": "vpc",
            "database": "cloudsql", "db": "cloudsql", "postgres": "cloudsql", "postgresql": "cloudsql",
            "sql": "cloudsql", "mysql": "cloudsql", "k8s": "gke", "kubernetes": "gke", "cluster": "gke",
            "bucket": "gcs", "blob": "gcs", "object_storage": "gcs",
            "keyring": "kms", "key": "kms", "encryption_key": "kms", "secrets": "kms"},
}


# MPP: modules promoted through the Module Promotion Pipeline join the approved library at
# runtime (rehydrated from the DB at startup). A draft/proposed module is NEVER in here —
# only a human review's `promote` decision registers a template.
_PROMOTED: dict[str, WorkflowTemplate] = {}


def register_promoted(template: WorkflowTemplate) -> None:
    """Add a PROMOTED module to the runtime library (MPP `review(decision='promote')` only)."""
    if template.key in _BY_KEY:
        raise ValueError(f"'{template.key}' already exists in the built-in catalog")
    _PROMOTED[template.key] = template


def apply_env_defaults(key: str, validated: dict, env: str | None) -> list[str]:
    """MODSEED: environment-aware input defaults, resolved AFTER validation and STATED on the
    approval card (never silent). Returns the note lines. Only fields the user left unset
    (None) are touched - an explicit choice always wins."""
    notes: list[str] = []
    if key == "azure.keyvault" and validated.get("network_default_action") == "Allow":
        notes.append("network_default_action: Allow - the vault accepts traffic from ALL "
                     "networks (AzureServices bypass is always on); set Deny to lock down")
    if key == "aws.nlb" and validated.get("deletion_protection") is None:
        on = (env or "").lower() == "production"
        validated["deletion_protection"] = on
        notes.append(f"deletion_protection: defaulted {'ON' if on else 'off'} for env={env or 'n/a'}"
                     + (" (Production default)" if on else ""))
    return notes


def by_key(key: str) -> WorkflowTemplate | None:
    """Approved template by its catalog key (e.g. "aws.vpc") — the exec loop's lookup."""
    return _BY_KEY.get(key) or _PROMOTED.get(key)


def select(cloud: str, resource: str) -> WorkflowTemplate | None:
    """Exact (cloud, resource) → curated template, or None (after resolving cloud synonyms).

    NO cross-cloud fallback: a request for a cloud/resource without an approved module returns
    None so the agent can clarify honestly. This makes wrong-cloud execution (e.g. an Azure VM
    request planning `aws.ec2`) structurally impossible.
    """
    cloud = (cloud or "").lower()
    resource = (resource or "").lower()
    resource = _SYNONYMS.get(cloud, {}).get(resource, resource)
    return _BY_KEY.get(f"{cloud}.{resource}") or _PROMOTED.get(f"{cloud}.{resource}")


def catalog() -> list[dict[str, str]]:
    """Compact catalog for the router's classification prompt (built-ins + promoted)."""
    return ([{"key": t.key, "cloud": t.cloud, "resource": t.resource, "description": t.description}
             for t in TEMPLATES]
            + [{"key": t.key, "cloud": t.cloud, "resource": t.resource, "description": t.description}
               for t in _PROMOTED.values()])
