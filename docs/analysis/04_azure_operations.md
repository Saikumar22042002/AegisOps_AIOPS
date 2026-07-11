# 04 — Azure operations

[← back to index](../../ANALYSIS.md) · Related: [03 AWS](03_aws_operations.md) · [05 GCP](05_gcp_operations.md)

Supported Azure resources (from `agents/templates.py`): **VM, Storage Account, Resource Group, PostgreSQL (Flexible Server), AKS**. The call chain is identical to [AWS create](03_aws_operations.md#31-aws-ec2--create) — `router → cloudops_plan → approval → execute → verify → finalize` — so this doc focuses on Azure-specific inputs, modules, credential flow, and the honest apply-status.

## 4.0 Supported matrix (Azure)

| Resource | Template key | Module dir | create | read | modify | destroy | Status (code + PROGRESS) |
|----------|--------------|-----------|:---:|:---:|:---:|:---:|---|
| VM (Linux/Windows) | `azure.vm` | `azure-vm` | ✅ | ✅ (count) | ✅ (NSG ports) | ✅ | plan-verified; **apply-deferred** (SP lacks Contributor) |
| Storage Account | `azure.storage` | `azure-storage` | ✅ | — | ❌ | ✅ | plan/apply where creds allow |
| Resource Group | `azure.resource_group` | `azure-resource-group` | ✅ | — | ❌ | ✅ | plan |
| PostgreSQL Flexible | `azure.postgres` | `azure-postgres` | ✅ | — | ❌ | ✅ | plan-verified; apply-deferred |
| AKS | `azure.aks` | `azure-aks` | ✅ | ✅ (via VM/VNet read only) | ❌ | ✅ | plan-verified; apply-deferred |

Synonyms (`_SYNONYMS["azure"]`): instance/server/compute/ec2→vm, database/db/postgres/sql/mysql→postgres, k8s/kubernetes/cluster→aks, blob/bucket/object_storage/storage_account→storage, rg→resource_group.

**Credential flow (Terraform):** the azurerm provider authenticates via `ARM_CLIENT_ID/ARM_CLIENT_SECRET/ARM_TENANT_ID/ARM_SUBSCRIPTION_ID`, injected by `TerraformRunner._env` (`tools/terraform.py:76`). All Azure modules set `skip_provider_registration = true` (the sandbox SP may lack `*/register/action`). **Read discovery** uses `azure-identity` + `azure-mgmt-*` (`tools/azure.py`); the import path `azure.mgmt.resource.resources.ResourceManagementClient` was fixed for azure-mgmt-resource ≥ 23 (a real bug that had mislabeled Azure as "not configured").

## 4.1 Azure VM — create/modify (the richest Azure path)
**Required params** (`params.PARAMS["azure.vm"]`): **name, size, os, allowed_cidr** (location/admin_username/resource_group/ingress_ports defaulted). Validation (`schemas/workflows.py:AzureVMInputs`): `size` via `_validate_azure_size` (must be `Standard_*`/`Basic_*`, and it rejects AWS/GCP shapes with a specific message — the Phase-7 cross-cloud-shape bug); `os` ∈ {ubuntu-22.04, ubuntu-24.04, debian-12, windows-2022}; `allowed_cidr` via `_validate_cidr`.

**Module** (`infra/terraform-workspaces/azure-vm/main.tf`) — self-contained: RG (created as `<name>-rg` if omitted) + VNet + subnet + public IP + NSG + NIC + VM. Genuinely supports **both** `azurerm_linux_virtual_machine` (generated SSH key via `tls_private_key`) **and** `azurerm_windows_virtual_machine` (generated `random_password`), keyed on `os == "windows-2022"`. Admin port (22 or 3389) opened only to `allowed_cidr`; `ingress_ports` open to `*`. Credentials (`private_key_pem` / `admin_password`) are **sensitive** outputs → one-time reveal only.

**Modify:** `_modify_resource` supports `vm` (adds NSG inbound ports) exactly like EC2, against the resource's own state workspace, in-place-guarded.

**Success card:** `cards.success_card("vm", …)` → "Instance ready" with public address, login user, `ssh`/`mstsc` connect line, and a Reveal button (private key for Linux, password for Windows).

## 4.2 Azure Storage Account — create
Required: **account_name, resource_group** (an *existing* RG name — this module does **not** create the RG; location/tier/replication defaulted). `account_name` validated as 3–24 lowercase alphanumeric. Module enforces `min_tls_version=TLS1_2` and `allow_nested_items_to_be_public=false`. Note: because it requires a pre-existing RG, a fresh apply also needs a separate `azure.resource_group` run first.

## 4.3 Azure Resource Group — create
Required: **name** (location defaulted). Trivial module (RG + tags). This was apply-verified in early phases where creds allowed.

## 4.4 Azure PostgreSQL Flexible Server — create
Required: **name** (location/admin_username/sku_name/pg_version/storage_mb defaulted; `storage_mb` floor 32768). Module creates its own RG + `azurerm_postgresql_flexible_server` with a generated `random_password` admin credential (sensitive output) + a demo firewall rule allowing Azure services. `public_network_access_enabled=true` (demo default). Success card: "Database ready" with FQDN + "generated admin password via Reveal."

## 4.5 Azure AKS — create
Required: **name** (location/node_count/node_size/kubernetes_version defaulted; `node_size` validated as `Standard_*`). Module creates RG + `azurerm_kubernetes_cluster` (SystemAssigned identity, default node pool). `kube_config_raw` is a sensitive output. Success card: "Cluster ready" with FQDN.

## 4.6 Azure day-2 & gaps
- **Read (broad):** `_discover_azure` (`cloudops.py:475`) lists VMs (count + names) and VNets; for storage/db/k8s it honestly says "live listing isn't wired for Azure yet — the inventory below covers what I created."
- **Read (specific):** `inventory.reconcile` has **no Azure branch** — an Azure resource read returns *recorded* attributes only (no live refresh). Only AWS EC2 reconciles live.
- **Modify:** VM NSG-ports only. No modify for storage/postgres/aks.
- **Verify:** `finalize._reconcile_checks` is AWS-only, so Azure applies get "Terraform outputs present" and nothing else — no live Azure reconciliation post-apply.
- **Honest apply-status:** per `PROGRESS.md` and consistent with the code (which does nothing to fake it), Azure **applies are deferred** because the sandbox service principal lacks Contributor — plans are real, applies weren't run. The code path is fully wired; only the cloud grant is missing.
