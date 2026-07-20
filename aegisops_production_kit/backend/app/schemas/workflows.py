"""Per-workflow input schemas (Pydantic) for multi-cloud provisioning.

The CloudOps agent extracts inputs from natural language (via Gemini) and/or parses free-form
(comma-separated / multiline key=value), then validates against the schema for the selected
template. Validation errors are returned to the user as actionable clarification.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


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


def _validate_cidr(v: str) -> str:
    """Allowed-source CIDR for VM admin access (Phase 8 / N-02). Accepts a CIDR or bare IP
    (normalized to /32); ''/'none'/'skip'/'closed' mean default-closed — an explicit choice."""
    import ipaddress
    v = (v or "").strip()
    if v.lower() in ("", "none", "skip", "closed", "no"):
        return ""
    bare = v if "/" in v else f"{v}/32"
    try:
        ipaddress.ip_network(bare, strict=False)
    except ValueError as e:
        raise ValueError(f"'{v}' is not a valid CIDR — send your public IP (e.g. 203.0.113.7 "
                         "or 203.0.113.7/32), or 'none' to keep remote access closed") from e
    return bare


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
    # MOD: 0 = no lifecycle (old behavior). Auto-expiry is always an explicit user choice.
    lifecycle_expire_days: int = Field(default=0, ge=0, le=3650)
    extra_tags: dict[str, str] = Field(default_factory=dict)

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
    # MS-11 (B2, verbatim per spec): standard = the pre-enhancement node-group path.
    eks_mode: str = "standard"

    @field_validator("eks_mode")
    @classmethod
    def _valid_mode(cls, v: str) -> str:
        if v.strip().lower() not in ("standard", "auto"):
            raise ValueError(f"eks_mode must be 'standard' or 'auto' — got '{v}'")
        return v.strip().lower()


class AWSRDSInputs(WorkflowInputs):
    """MS-7 multi-engine RDS. BACKCOMPAT B2: every plan-shape-changing option defaults to
    the OLD behavior HERE (the module's own defaults are the secure ones for bare use) —
    the platform always passes these fields explicitly, so stored pre-enhancement inputs
    re-plan to zero changes (B1)."""

    identifier: str
    engine: str = "postgres"
    engine_version: str = ""            # "" = provider default (old) · "latest" · explicit pin

    # STAB P1-4 (BUGFIX-1 family): canonicalize case, refuse the rest honestly — Terraform
    # is never the validator. Live (screenshot 21): `Sai-test-v1` reached `terraform plan`
    # and died with a raw provider error instead of a per-field re-ask.
    @field_validator("identifier")
    @classmethod
    def _norm_identifier(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", v) or "--" in v or v.endswith("-"):
            raise ValueError(
                f"'{v}' is not a valid RDS identifier — 1-63 chars, letters/digits/hyphens, "
                "starts with a letter, no trailing or double hyphen (e.g. payments-db)")
        return v
    instance_class: str = "db.t3.medium"
    allocated_storage: int = Field(default=20, ge=20, le=4096)
    region: str = "us-east-1"
    allowed_cidr: str = ""              # dedicated SG only when set; MANDATORY for that path
    subnet_ids: list[str] = Field(default_factory=list)
    enable_log_exports: bool = False    # engine-aware exports + query-logging param group
    extra_tags: dict[str, str] = Field(default_factory=dict)   # MOD: day-2 in-place tags

    @field_validator("instance_class")
    @classmethod
    def _valid_class(cls, v: str) -> str:
        if not re.fullmatch(r"db\.[a-z0-9]+\.[a-z0-9]+", v.strip()):
            raise ValueError(f"'{v}' is not a valid RDS instance class — expected e.g. db.t3.medium")
        return v.strip()

    @field_validator("engine")
    @classmethod
    def _valid_engine(cls, v: str) -> str:
        allowed = ("postgres", "mysql", "mariadb")
        if v.strip().lower() not in allowed:
            raise ValueError(f"engine must be one of {allowed} — got '{v}'")
        return v.strip().lower()

    @field_validator("allowed_cidr")
    @classmethod
    def _never_world_open(cls, v: str) -> str:
        v = v.strip()
        if v == "":
            return v
        if v.endswith("/0"):
            raise ValueError("allowed_cidr must never be world-open — a /0 CIDR is rejected outright")
        if not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}/\d{1,2}", v):
            raise ValueError(f"'{v}' is not a valid IPv4 CIDR (e.g. 10.0.0.0/16)")
        return v


EC2_OS_CHOICES = ("amazon-linux-2023", "ubuntu-22.04", "ubuntu-24.04", "windows-2022")


class AWSKMSInputs(WorkflowInputs):
    """MODSEED MS-4 - aws.kms: key + alias + service policy. Secret VALUES are permanently
    out of scope (never chat-supplied) - this manages KEYS."""

    name: str
    region: str = "us-east-1"
    deletion_window: int = Field(default=30, ge=7, le=30)
    enable_rotation: bool = True
    allowed_services: list[str] = Field(default_factory=lambda: ["secretsmanager", "rds"])


class AWSNLBInputs(WorkflowInputs):
    """MODSEED MS-3 - aws.nlb: network LB + TCP target group/listener. vpc_id/subnets are
    DEP-resolved (existing VPC's recorded outputs, or a create-first DAG). deletion_protection
    None = platform default (ON for env=Production, stated on the card)."""

    name: str
    region: str = "us-east-1"
    vpc_id: str = ""
    subnets: list[str] = Field(default_factory=list)
    target_port: int = Field(default=80, ge=1, le=65535)
    listener_port: int | None = Field(default=None, ge=1, le=65535)
    internal: bool = False
    deletion_protection: bool | None = None
    security_group_ids: list[str] = Field(default_factory=list)

    @field_validator("listener_port", mode="before")
    @classmethod
    def _default_listener(cls, v, info):
        return v  # resolved to target_port post-validation (model_validator below)

    def model_post_init(self, __context) -> None:
        if self.listener_port is None:
            object.__setattr__(self, "listener_port", self.target_port)


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
    allowed_cidr: str = ""  # admin access (22/3389) source CIDR; "" = closed (N-02)
    # MS-10 (B2 at the schema level): SSM+CloudWatch instance profile — OFF here so existing
    # instances re-plan unchanged; the module's own default is ON for bare use.
    enable_ssm: bool = False
    # MOD (owner Option A): Terraform-encoded power state; "" = unmanaged (old behavior).
    power_state: str = ""
    extra_tags: dict[str, str] = Field(default_factory=dict)

    @field_validator("power_state")
    @classmethod
    def _valid_power(cls, v: str) -> str:
        if v.strip().lower() not in ("", "running", "stopped"):
            raise ValueError("power_state must be empty, running, or stopped")
        return v.strip().lower()

    @field_validator("allowed_cidr")
    @classmethod
    def _valid_cidr(cls, v: str) -> str:
        return _validate_cidr(v)

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


class GCPKMSInputs(WorkflowInputs):
    """MODSEED MS-6 - gcp.kms: key ring + crypto keys. Ring names are PERMANENT in GCP -
    destroy removes key versions/IAM only (stated on the destroy card)."""

    name: str
    project: str = ""
    region: str = "us-central1"
    keys: list[str] = Field(default_factory=list)
    rotation_days: int = Field(default=90, ge=1, le=365)
    encrypter_decrypters: list[str] = Field(default_factory=list)


class GCPVPCInputs(WorkflowInputs):
    """MODSEED MS-1 - gcp.vpc: custom-mode network + subnets (+pods/services secondary
    ranges) + NAT. Only `name` is decision-critical; the project is auto-filled."""

    name: str
    project: str = ""
    region: str = "us-central1"
    subnet_cidrs: list[str] = Field(default_factory=lambda: ["10.10.0.0/20", "10.10.16.0/20"])
    enable_nat: bool = True
    enable_flow_logs: bool = False

    @field_validator("subnet_cidrs")
    @classmethod
    def _valid_cidrs(cls, v: list[str]) -> list[str]:
        import ipaddress
        if not v:
            raise ValueError("at least one subnet CIDR is required")
        for c in v:
            net = ipaddress.ip_network(c, strict=True)  # raises on malformed input
            if not net.is_private:
                raise ValueError(f"subnet CIDR {c} must be RFC1918 private space")
        return v


class AzureKeyVaultInputs(WorkflowInputs):
    """MODSEED MS-5 - azure.keyvault: vault + optional keys. Secret VALUES are permanently
    out of scope. network_default_action=Allow is STATED on the approval card."""

    name: str = Field(min_length=3, max_length=24)
    location: str = "eastus"
    resource_group: str = ""
    soft_delete_days: int = Field(default=90, ge=7, le=90)
    purge_protection: bool = True
    network_default_action: str = "Allow"
    keys: list[str] = Field(default_factory=list)

    @field_validator("network_default_action")
    @classmethod
    def _valid_action(cls, v: str) -> str:
        if v not in ("Allow", "Deny"):
            raise ValueError("network_default_action must be Allow or Deny")
        return v


class AzureVNetInputs(WorkflowInputs):
    """MODSEED MS-2 - azure.vnet: VNet + subnets + NAT + route tables. Only `name` is
    decision-critical; the RG defaults to a module-created '<name>-rg' (like azure-vm)."""

    name: str
    location: str = "eastus"
    resource_group: str = ""
    address_space: str = "10.20.0.0/16"
    subnet_cidrs: list[str] = Field(default_factory=lambda: ["10.20.1.0/24"])
    private_subnet_cidrs: list[str] = Field(default_factory=list)

    @field_validator("address_space", "subnet_cidrs", "private_subnet_cidrs")
    @classmethod
    def _rfc1918(cls, v):
        import ipaddress
        vals = v if isinstance(v, list) else [v]
        for c in vals:
            net = ipaddress.ip_network(c, strict=True)
            if not net.is_private:
                raise ValueError(f"CIDR {c} must be RFC1918 private space")
        return v


# ── Azure (Phase 5) ──
AZURE_OS_CHOICES = ("ubuntu-22.04", "ubuntu-24.04", "debian-12", "windows-2022")


class AzureVMInputs(WorkflowInputs):
    name: str
    location: str = "eastus"
    size: str = "Standard_B1s"
    os: str = "ubuntu-22.04"           # ubuntu-22.04 | ubuntu-24.04 | debian-12 | windows-2022
    admin_username: str = "azureuser"
    resource_group: str = ""
    ingress_ports: list[int] = Field(default_factory=list)
    allowed_cidr: str = ""             # admin access (22/3389) source CIDR; "" = closed (N-02)
    # MS-13 (B4): filled by the azure.vm→vnet DEP slot; "" keeps the module-created vnet.
    existing_subnet_id: str = ""

    @field_validator("size")
    @classmethod
    def _valid_size(cls, v: str) -> str:
        return _validate_azure_size(v)

    @field_validator("os")
    @classmethod
    def _valid_os(cls, v: str) -> str:
        # Provider-accurate (Phase 8 / N-05): the platform genuinely offers Windows Server
        # and Debian alongside Ubuntu — reject only what Azure itself wouldn't create.
        if v not in AZURE_OS_CHOICES:
            raise ValueError(f"os must be one of {list(AZURE_OS_CHOICES)}")
        return v

    @field_validator("allowed_cidr")
    @classmethod
    def _valid_cidr(cls, v: str) -> str:
        return _validate_cidr(v)

    @field_validator("ingress_ports", mode="before")
    @classmethod
    def _ports(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = [p.strip() for p in re.split(r"[,\s]+", v) if p.strip()]
        return [int(p) for p in v] if isinstance(v, (list, tuple)) else v


class AzureDBInputs(WorkflowInputs):
    """MS-8 multi-engine Azure database (postgresql/mysql/mssql). BACKCOMPAT B2: every
    plan-shape-changing option defaults to OLD behavior HERE (module defaults stay secure
    for bare use) — the platform passes these explicitly, so stored pre-enhancement inputs
    render the exact old postgres shape (B1, proven by the workspace's terraform test)."""

    name: str
    engine: str = "postgresql"
    location: str = "eastus"
    admin_username: str = "pgadmin"
    sku_name: str = "B_Standard_B1ms"
    storage_mb: int = Field(default=32768, ge=32768)
    pg_version: str = "15"
    engine_version: str = ""            # mysql override (default 8.0.21); mssql is fixed 12.0
    resource_group: str = ""
    ha_enabled: bool = False            # ZoneRedundant HA (postgresql/mysql)
    geo_redundant_backup: bool = False  # module default is ON; schema keeps old behavior
    delegated_subnet_id: str = ""       # private access (postgresql/mysql)
    private_dns_zone_id: str = ""

    @field_validator("engine")
    @classmethod
    def _valid_engine(cls, v: str) -> str:
        allowed = ("postgresql", "mysql", "mssql")
        vv = v.strip().lower()
        if vv == "postgres":
            vv = "postgresql"           # common shorthand
        if vv not in allowed:
            raise ValueError(f"engine must be one of {allowed} — got '{v}'")
        return vv

    @model_validator(mode="after")
    def _private_access_pairing(self) -> "AzureDBInputs":
        if self.delegated_subnet_id and not self.private_dns_zone_id:
            raise ValueError("private access needs BOTH delegated_subnet_id and private_dns_zone_id")
        if self.delegated_subnet_id and self.engine == "mssql":
            raise ValueError("delegated-subnet private access applies to postgresql/mysql; "
                             "SQL Server private connectivity uses private endpoints")
        if self.ha_enabled and self.engine == "mssql":
            raise ValueError("HA (ZoneRedundant) applies to the postgresql/mysql engines")
        return self


# MS-8 backcompat alias — old imports and stored references keep validating.
AzurePostgresInputs = AzureDBInputs


class AzureAKSInputs(WorkflowInputs):
    """MS-13. B2: options default OFF here (existing clusters re-plan unchanged); the
    module's own defaults are the observable/governed ones."""

    name: str
    location: str = "eastus"
    node_count: int = Field(default=2, ge=1, le=100)
    node_size: str = "Standard_B2s"
    enable_monitoring: bool = False     # Log Analytics + OMS agent
    network_policy: str = ""            # "" = old rendering; calico | azure
    azure_policy_enabled: bool = False

    @field_validator("network_policy")
    @classmethod
    def _valid_netpol(cls, v: str) -> str:
        if v.strip().lower() not in ("", "calico", "azure"):
            raise ValueError("network_policy must be empty, calico, or azure")
        return v.strip().lower()
    kubernetes_version: str = ""
    resource_group: str = ""

    @field_validator("node_size")
    @classmethod
    def _valid_node_size(cls, v: str) -> str:
        return _validate_azure_size(v)


# ── GCP (Phase 5) ──
class GCPComputeInputs(WorkflowInputs):
    """MS-12. BACKCOMPAT B2: options default to the OLD behavior here (public IP ON,
    everything else off, network 'default'); the module's own defaults are the secure
    ones. The gcp.vm→network DEP slot fills `network` from an existing gcp.vpc (B4)."""

    name: str
    project: str = ""                  # defaults to the configured GCP project
    region: str = "us-central1"
    zone: str = "us-central1-a"
    machine_type: str = "e2-micro"
    os: str = "debian-12"              # debian-12 | ubuntu-22.04 | ubuntu-24.04
    ssh_user: str = "aegis"
    ingress_ports: list[int] = Field(default_factory=list)
    allowed_cidr: str = ""             # SSH (22) source CIDR; "" = closed (N-02)
    network: str = "default"           # DEP-slot fillable; "default" = old placement
    public_ip: bool = True             # B2 old behavior (module default is OFF)
    enable_shielded: bool = False      # module default is ON
    block_project_ssh_keys: bool = False
    enable_oslogin: bool = False       # replaces metadata SSH keys while enabled
    spot: bool = False                 # preemptible — maintenance implications on the card
    service_account_email: str = ""    # least-scope (logging+monitoring writes) when set
    power_state: str = ""              # MOD (Option A): "" unmanaged · running · stopped

    @field_validator("power_state")
    @classmethod
    def _valid_power(cls, v: str) -> str:
        if v.strip().lower() not in ("", "running", "stopped"):
            raise ValueError("power_state must be empty, running, or stopped")
        return v.strip().lower()

    # STAB P1-1: the module is genuinely Linux-only. Without this validator the extractor's
    # normalized "windows-2022" sailed through Pydantic and the module's image lookup fell
    # back to Linux — a silent substitution the user never asked for (live, screenshot 7-8).
    # An unsupported OS is an HONEST REFUSAL naming where the request IS supported.
    @field_validator("os")
    @classmethod
    def _valid_os(cls, v: str) -> str:
        allowed = ("debian-12", "ubuntu-22.04", "ubuntu-24.04")
        if v not in allowed:
            raise ValueError(
                f"gcp.vm is Linux-only ({', '.join(allowed)}) — Windows Server is available "
                "on aws.ec2 or azure.vm; say which you'd like instead")
        return v

    @field_validator("machine_type")
    @classmethod
    def _valid_machine_type(cls, v: str) -> str:
        return _validate_gcp_machine_type(v)

    @field_validator("allowed_cidr")
    @classmethod
    def _valid_cidr(cls, v: str) -> str:
        return _validate_cidr(v)

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
    """MS-9. BACKCOMPAT B2: every new option defaults to the OLD behavior here — including
    the legacy world-open 'all' authorized network, which the module itself no longer
    defaults to (module defaults are the secure ones). The platform passes all fields
    explicitly, so stored pre-enhancement inputs render the exact old plan (B1, proven by
    the workspace's committed terraform test)."""

    name: str
    project: str = ""
    region: str = "us-central1"
    tier: str = "db-f1-micro"
    database_version: str = "POSTGRES_15"
    authorized_networks: list[str] = Field(default_factory=lambda: ["0.0.0.0/0"])
    private_network: str = ""           # VPC self-link → private peering, drops the public IP
    ssl_mode: str = ""                  # "" = provider default (old); ENCRYPTED_ONLY etc.
    backup_enabled: bool = False        # module default is ON; schema keeps old behavior
    database_flags: dict[str, str] = Field(default_factory=dict)
    enable_query_insights: bool = False
    maintenance_day: int = Field(default=0, ge=0, le=7)    # 0 = unset (old behavior)
    maintenance_hour: int = Field(default=3, ge=0, le=23)
    deletion_protection: bool = False   # destroys stay approval-gated by the platform
    encryption_key_name: str = ""       # CMEK; offered by the DEP slot, never forced

    @field_validator("database_version")
    @classmethod
    def _normalize_engine(cls, v: str) -> str:
        """BUGFIX-1 (live acceptance run 2): the param extractor passes the user's own word
        ("postgres") straight through — un-normalized it fails the approved-engine policy
        check AND the provider's enum at apply. Canonicalize the honest spellings to the
        Cloud SQL enum; reject what can't be mapped (with examples), never guess an engine
        the user didn't name. Canonical values pass through unchanged (B1)."""
        raw = re.sub(r"[\s-]+", "_", v.strip().upper())
        if not raw:
            return "POSTGRES_15"                      # schema default (B2)
        raw = re.sub(r"^POSTGRESQL", "POSTGRES", raw)
        if raw == "POSTGRES":
            return "POSTGRES_15"                      # bare engine → default version
        if raw == "MYSQL":
            return "MYSQL_8_0"
        if re.fullmatch(r"POSTGRES_\d+(_\d+)?|MYSQL_\d+(_\d+)?|SQLSERVER_\d{4}_[A-Z0-9]+", raw):
            return raw
        raise ValueError(f"'{v}' is not a valid Cloud SQL engine/version — expected e.g. "
                         "POSTGRES_15, MYSQL_8_0, SQLSERVER_2019_STANDARD (or just "
                         "'postgres' / 'mysql')")

    @field_validator("ssl_mode")
    @classmethod
    def _valid_ssl_mode(cls, v: str) -> str:
        allowed = ("", "ALLOW_UNENCRYPTED_AND_ENCRYPTED", "ENCRYPTED_ONLY",
                   "TRUSTED_CLIENT_CERTIFICATE_REQUIRED")
        if v.strip() not in allowed:
            raise ValueError(f"ssl_mode must be empty (provider default) or one of {allowed[1:]}")
        return v.strip()

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
