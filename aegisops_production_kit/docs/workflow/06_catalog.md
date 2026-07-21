# 06 · Resource Catalog

Every registered workflow module — ground truth from `agents/templates.py` (registration +
policy), `agents/params.py` (decision-critical collection), `schemas/workflows.py` (validation +
defaults), `agents/dependency.py` (DEP slots), and `agents/cloudops.py` (`_MODIFY_CAPS`).

**20 built-in templates** (7 AWS · 7 Azure · 6 GCP) + 1 alias key (`azure.postgres → azure.db`).

## Registration mechanism

- **`WorkflowTemplate` dataclass** (`templates.py:20-37`): `key`, `cloud`, `resource`, `version`,
  `workspace` (TF dir), `schema` (a `WorkflowInputs` subclass), `description`, `policy_fn`
  (default returns `[]`), `actions` (default `("create","modify","destroy")` — no template
  overrides it), `destroy_note` (default `None`).
- **Registry**: flat `TEMPLATES` list (`templates.py:454-478`) → `_BY_KEY` dict (`:480`); one
  alias `_BY_KEY["azure.postgres"] = _BY_KEY["azure.db"]` (`:483`); promoted runtime modules in a
  separate `_PROMOTED` dict (`:513`, MPP).
- **Lookup**: `by_key` (`:539`), `select(cloud, resource)` (`:544`, per-cloud synonym resolution,
  **no cross-cloud fallback**), `catalog()` (`:557`, the router's menu).
- **Synonyms** are per-cloud *resource* synonyms (`_SYNONYMS`, `:488-507`), e.g. AWS
  `database/db/postgres/mysql → rds`, GCP `keyring/key/secrets → kms`. The only *key* synonym is
  `azure.postgres`.
- **"Required"** = `params.py` `ParamSpec(required=True)` (the collection layer,
  `missing_required` `:253`); the Pydantic schema often still carries a default for the same
  field (used if the value arrives another way).

## Master table

| Key | Cloud | Workspace | Creates | Required params | DEP slots | Day-2 (`_MODIFY_CAPS`) | destroy_note |
|---|---|---|---|---|---|---|---|
| `aws.s3` | aws | aws-s3 | encrypted private bucket | `bucket_name` | — | versioning, lifecycle_expire_days, tags | — |
| `aws.vpc` | aws | aws-vpc | VPC + public/private subnets + NAT | `name` | — (creator) | — | — |
| `aws.eks` | aws | eks-provision | hardened EKS reusing a VPC | `cluster_name`, `vpc_id`, `subnet_ids` | `vpc_id`←aws.vpc **(req)** + `subnet_ids` | — | — |
| `aws.rds` | aws | aws-rds | encrypted RDS (pg/mysql/mariadb) | `identifier` | — | instance_class, allocated_storage, tags | — |
| `aws.ec2` | aws | aws-ec2 | EC2 (IMDSv2, encrypted) | `name`, `instance_type`, `os`, `key_pair`, `allowed_cidr` | `subnet_id`←aws.vpc (opt) | ingress_ports, power, tags | — |
| `aws.nlb` | aws | aws-nlb | network LB (TCP TG + listener) | `name` | `vpc_id`←aws.vpc **(req)** + `subnets` | — | — |
| `aws.kms` | aws | aws-kms | KMS key (rotation, alias, policy) | `name` | — | — | **scheduled-deletion window (7-30d)** |
| `azure.storage` | azure | azure-storage | storage account | `account_name`, `resource_group` | `resource_group`←azure.rg **(req)** | — | — |
| `azure.vnet` | azure | azure-vnet | VNet + subnets + NAT + routes | `name` | `resource_group`←azure.rg (opt) | — | — |
| `azure.keyvault` | azure | azure-keyvault | Key Vault (soft-delete, purge prot.) | `name` | `resource_group`←azure.rg (opt) | — | **soft-delete retention + purge protection** |
| `azure.resource_group` | azure | azure-resource-group | resource group | `name` | — (creator) | — | — |
| `azure.vm` | azure | azure-vm | Linux/Windows VM (generated key) | `name`, `size`, `os`, `allowed_cidr` | `existing_subnet_id`←azure.vnet (opt) + `resource_group`←azure.rg (opt) | ingress_ports | — |
| `azure.db` (alias `azure.postgres`) | azure | azure-postgres | pg/mysql flexible or SQL Server | `name` | `resource_group`←azure.rg (opt) | — | — |
| `azure.aks` | azure | azure-aks | AKS cluster | `name` | `resource_group`←azure.rg (opt) | — | — |
| `gcp.gcs` | gcp | gcp-gcs | GCS bucket (uniform, versioned) | `bucket_name` | — | — | — |
| `gcp.vpc` | gcp | gcp-vpc | custom-mode VPC + subnets + NAT | `name` | — (creator) | — | — |
| `gcp.kms` | gcp | gcp-kms | key ring + crypto keys (90d rot.) | `name` | — | — | **key rings NOT deletable — versions/IAM only** |
| `gcp.vm` | gcp | gcp-gce | GCE VM (generated key) | `name`, `machine_type`, `os`, `allowed_cidr` | `network`←gcp.vpc (opt) | ingress_ports, power | — |
| `gcp.gke` | gcp | gcp-gke | GKE cluster | `name` | — | — | — |
| `gcp.cloudsql` | gcp | gcp-cloudsql | Cloud SQL for PostgreSQL | `name` | `encryption_key_name`←gcp.kms (opt, CMEK) | — | — |

Only **5 modules** support day-2 modify (`_MODIFY_CAPS`, `cloudops.py:124-130`); only **3** carry
a `destroy_note` (the three key/vault modules). **9 module keys** carry DEP slots (10 slots;
`azure.vm` has 2) — `dependency.py:54-119`; `slot_fields()` (`:136`) is what keeps the params
card from ever demanding raw provider ids.

## Per-module detail (defaults · validators · policy)

### AWS

- **`aws.s3`** — schema `AWSS3Inputs` (`workflows.py:88`). Defaults: `region=us-east-1`,
  `versioning=True`, `block_public=True`, `lifecycle_expire_days=0`. Validator `_valid_bucket`
  (`:97`) lowercase 3-63. Policy `_s3_policy` (`templates.py:71`): public-access-blocked,
  SSE present, versioning enabled, approved-version.
- **`aws.vpc`** — `AWSVPCInputs` (`workflows.py:105`): `cidr_block=10.0.0.0/16`, `az_count=3`
  (1-6), `enable_nat=True`. Policy `_vpc_policy` (`templates.py:98`): multi-AZ (`az_count>=2`),
  private-subnets+NAT, approved module.
- **`aws.eks`** — `AWSEKSInputs` (`workflows.py:113`): `eks_mode=standard` (validator `:124`),
  `kubernetes_version=1.29`, `instance_types=[m6i.xlarge]`, `desired_size=3`. Policy `_eks_policy`
  (`templates.py:106`): secrets-encryption, private-endpoint, IRSA, multi-AZ (`subnet_ids>=2`),
  cluster mode. DEP: `vpc_id` (req) + companion `subnet_ids ← private_subnet_ids`
  (`dependency.py:55`).
- **`aws.rds`** — `AWSRDSInputs` (`workflows.py:132`): `engine=postgres`, `instance_class=
  db.t3.medium`, `allocated_storage=20` (20-4096), `allowed_cidr=""`, `enable_log_exports=False`.
  Validators: `_norm_identifier` (`:146`, lowercase, letter-start, no `--`/trailing `-` — STAB
  P1-4), `_valid_engine` (`:169`, pg/mysql/mariadb), `_never_world_open` (`:177`, rejects `/0`).
  Policy `_rds_policy` (`templates.py:121`): storage-encrypted, not-public, managed-password,
  approved-engine, SG-scoped-no-/0, engine-aware log exports.
- **`aws.ec2`** — `AWSEC2Inputs` (`workflows.py:229`): `instance_type=t3.micro`, `os=
  amazon-linux-2023`, `root_volume_type=gp3`, `enable_ssm=False`, `power_state=""`. `key_pair` is
  a collection alias transformed by `_ec2_to_tf` (`params.py:282`) into `create_key_pair`+
  `key_name`. Validators: `_valid_os` (`:270`, `EC2_OS_CHOICES` incl. `windows-2022`),
  `_valid_power` (`:250`), `_valid_cidr` (`:257`), instance-type/vol-type shape. Policy
  `_ec2_policy` (`templates.py:151`): IMDSv2, root-encrypted, dedicated-SG, SSM-note. DEP:
  `subnet_id ← public_subnet_ids[0]` (opt, `dependency.py:63`).
- **`aws.nlb`** — `AWSNLBInputs` (`workflows.py:204`): `target_port=80`, `listener_port`
  defaults to `target_port` via `model_post_init` (`:224`), `internal=False`. `apply_env_defaults`
  turns `deletion_protection` ON for `env=production` (`templates.py:531`). Policy `_aws_nlb_policy`
  (`templates.py:171`): network type, cross-zone on, deletion-protection-as-approved, TCP health
  30s/×3. DEP: `vpc_id` (req) + companion `subnets ← public_subnet_ids` (`dependency.py:59`).
- **`aws.kms`** — `AWSKMSInputs` (`workflows.py:193`): `deletion_window=30` (7-30),
  `enable_rotation=True`, `allowed_services=[secretsmanager,rds]`. Policy `_aws_kms_policy`
  (`templates.py:202`): rotation on, window≥7, root+services-only policy. **destroy_note**
  (`templates.py:462`): scheduled-deletion window, recoverable until it elapses.

### Azure

- **`azure.storage`** — `AzureStorageInputs` (`workflows.py:299`): `account_tier=Standard`,
  `replication=LRS`. Validator `_valid_account` (`:306`, 3-24 lowercase alnum). Policy
  (`templates.py:220`): min TLS 1.2, no public blob. DEP: `resource_group` (req) by name
  (`dependency.py:68`).
- **`azure.vnet`** — `AzureVNetInputs` (`workflows.py:378`): `address_space=10.20.0.0/16`,
  `subnet_cidrs=[10.20.1.0/24]`. Validator `_rfc1918` (`:389`). Policy `_azure_vnet_policy`
  (`templates.py:272`): ≥1 subnet, RFC1918, **no NSG in the network module**. DEP: `resource_group`
  (opt).
- **`azure.keyvault`** — `AzureKeyVaultInputs` (`workflows.py:358`): `soft_delete_days=90` (7-90),
  `purge_protection=True`, `network_default_action=Allow` (validator `:370`). `apply_env_defaults`
  warns when action is `Allow` (`templates.py:528`). Policy (`templates.py:232`): soft-delete≥7,
  purge-as-approved, AzureServices bypass. **destroy_note** (`:466`): soft-delete retention;
  purge-protection blocks permanent delete until the window elapses. DEP: `resource_group` (opt).
- **`azure.resource_group`** — `AzureResourceGroupInputs` (`workflows.py:294`): `location=eastus`.
  Policy `_azure_rg_policy` (`templates.py:256`): tagging, approved-region (both `_todo`).
- **`azure.vm`** — `AzureVMInputs` (`workflows.py:405`): `size=Standard_B1s`, `os=ubuntu-22.04`
  (validator `:422`, `AZURE_OS_CHOICES` incl. `windows-2022`), `admin_username=azureuser`. Policy
  (`templates.py:301`): SSH-key-auth, dedicated NSG, managed disk. Day-2: `{ingress_ports}`; Azure
  power returns an honest "use the portal" (`cloudops.py:132`). DEP: `existing_subnet_id ←
  subnet_ids[0]` (opt) + `resource_group` (opt) (`dependency.py:85-94`).
- **`azure.db`** (alias `azure.postgres`, version v2) — `AzureDBInputs` (`workflows.py:444`):
  `engine=postgresql` (validator `:464`, pg/mysql/mssql; `postgres→postgresql`), `pg_version=15`,
  `sku_name=B_Standard_B1ms`, `storage_mb=32768`, `ha_enabled=False`. `_private_access_pairing`
  (`:475`): delegated subnet needs a DNS zone; mssql rejects HA/delegated-subnet. Policy
  `_azure_db_policy` (`templates.py:309`): TLS, managed-password, approved-engine, pg-version,
  mssql TLS-1.2, zone-HA, private-access. DEP: `resource_group` (opt).
- **`azure.aks`** — `AzureAKSInputs` (`workflows.py:491`): `node_count=2`, `node_size=
  Standard_B2s`, `network_policy=""` (validator `:503`, ""/calico/azure), `enable_monitoring=
  False`, `azure_policy_enabled=False`. Policy (`templates.py:338`): managed identity, Azure RBAC,
  multi-node, monitoring/network-policy/policy-addon (conditional). DEP: `resource_group` (opt).

### GCP

- **`gcp.gcs`** — `GCPGCSInputs` (`workflows.py:315`): `location=US`, `storage_class=STANDARD`,
  `project` **required in schema** (divergence: optional in params.py). Policy `_gcs_policy`
  (`templates.py:260`): uniform-access, force_destroy off, versioning.
- **`gcp.vpc`** — `GCPVPCInputs` (`workflows.py:334`): `subnet_cidrs=[10.10.0.0/20,10.10.16.0/20]`,
  `enable_nat=True`. Validator `_valid_cidrs` (`:345`, RFC1918). Policy `_gcp_vpc_policy`
  (`templates.py:355`): custom-mode (no auto-subnets), ≥1 subnet, internal-firewall-scoped.
- **`gcp.kms`** — `GCPKMSInputs` (`workflows.py:322`): `rotation_days=90` (1-365). Policy
  `_gcp_kms_policy` (`templates.py:375`): rotation configured, SOFTWARE protection,
  ENCRYPT_DECRYPT. **destroy_note** (`:474`): key rings are NOT deletable — destroy removes
  versions/IAM only.
- **`gcp.vm`** — `GCPComputeInputs` (`workflows.py:519`): `machine_type=e2-micro`, `os=debian-12`,
  `network=default`, `public_ip=True`, `spot=False`. Validator `_valid_os` (`:553`) is a
  **Linux-only allowlist** (`debian-12`, `ubuntu-22.04/24.04`) whose refusal names aws.ec2/azure.vm
  for Windows (STAB P1-1). Policy `_gcp_gce_policy` (`templates.py:400`): SSH-key-auth,
  restricted-ingress, labelled, plus conditional shielded/spot/OS-login/least-scope-SA. Day-2:
  `{ingress_ports, power}` (power via GCE `desired_status`). DEP: `network ← gcp.vpc` name (opt,
  `dependency.py:114`).
- **`gcp.gke`** — `GCPGKEInputs` (`workflows.py:581`): `node_count=2`, `machine_type=e2-medium`.
  Policy `_gcp_gke_policy` (`templates.py:422`): dedicated-node-pool, deletion-protection off,
  multi-node.
- **`gcp.cloudsql`** — `GCPCloudSQLInputs` (`workflows.py:594`): `tier=db-f1-micro`,
  `database_version=POSTGRES_15`, `authorized_networks=["0.0.0.0/0"]` (**legacy world-open
  default** — the policy fails on it visibly), `backup_enabled=False`, `deletion_protection=False`.
  Validators: `_normalize_engine` (`:617`, canonicalizes to the Cloud SQL enum), `_valid_ssl_mode`
  (`:639`), `_valid_tier` (`:648`). Policy `_gcp_cloudsql_policy` (`templates.py:430`): generated
  password, approved-engine, and either "network exposure" (private) OR **"No world-open
  authorized networks"** (rejects any `/0`), backups+PITR, CMEK. DEP: `encryption_key_name ←
  gcp.kms key_ids[0]` (opt, CMEK, `dependency.py:107`).

## Day-2 modify map (`cloudops.py:124-130`)

```python
"aws.ec2": {"ingress_ports", "power", "tags"},
"gcp.vm":  {"ingress_ports", "power"},
"azure.vm":{"ingress_ports"},
"aws.s3":  {"versioning", "lifecycle_expire_days", "tags"},
"aws.rds": {"instance_class", "allocated_storage", "tags"},
```

Change→input mapping in `_apply_modification` (`cloudops.py:136`): `power → power_state`
(Terraform-encoded, never SDK), `tags → extra_tags` (merge), `ingress_ports` (union-merge). All
other modules are create/destroy only. Every modify runs the same approval gate + plan_guard
in-place check + policy re-run.
