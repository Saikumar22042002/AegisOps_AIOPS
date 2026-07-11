# 05 — GCP operations

[← back to index](../../ANALYSIS.md) · Related: [03 AWS](03_aws_operations.md) · [04 Azure](04_azure_operations.md)

Supported GCP resources: **Compute Engine VM, GCS bucket, Cloud SQL (PostgreSQL), GKE**. Same `router → cloudops_plan → approval → execute → verify → finalize` chain as AWS.

## 5.0 Supported matrix (GCP)

| Resource | Template key | Module dir | create | read | modify | destroy | Status |
|----------|--------------|-----------|:---:|:---:|:---:|:---:|---|
| Compute VM | `gcp.vm` | `gcp-gce` | ✅ | ✅ (count) | ✅ (firewall ports) | ✅ | **apply-verified** (full lifecycle per PROGRESS) |
| GCS bucket | `gcp.gcs` | `gcp-gcs` | ✅ | ✅ (via VM/network read) | ❌ | ✅ | plan |
| Cloud SQL | `gcp.cloudsql` | `gcp-cloudsql` | ✅ | — | ❌ | ✅ | plan-verified; apply-deferred (cost/time) |
| GKE | `gcp.gke` | `gcp-gke` | ✅ | — | ❌ | ✅ | plan-verified; apply-deferred |

Synonyms (`_SYNONYMS["gcp"]`): instance/server/compute/gce/ec2→vm, database/db/postgres/sql/mysql→cloudsql, k8s/kubernetes/cluster→gke, bucket/blob/object_storage→gcs.

**Project auto-fill:** for every GCP template, `cloudops_plan` sets `collected["project"] = settings.google_cloud_project` if the user didn't name one (`cloudops.py:229`) — so `project` is never asked. **Credential flow:** the google provider authenticates via `GOOGLE_PROJECT` + `GOOGLE_APPLICATION_CREDENTIALS` (a mounted SA key at `/secrets/gcp-sa.json`), injected by `TerraformRunner._env`. Read discovery uses `google-cloud-compute` + `google-cloud-resource-manager` (`tools/gcp.py`).

## 5.1 GCP Compute VM — create/modify (apply-verified)
**Required params** (`params.PARAMS["gcp.vm"]`): **name, machine_type, os, allowed_cidr** (project/region/zone/ssh_user/ingress_ports defaulted). Validation (`GCPComputeInputs`): `machine_type` via `_validate_gcp_machine_type` — an allowlist of families (e2/n1/n2/n2d/c2/c3/…) **plus** a special-case that rejects AWS-style values ("looks like an AWS instance type, e.g. ec2-micro") with a helpful message (the Phase-7 `ec2-micro`→GCP bug). `os` ∈ {debian-12, ubuntu-22.04, ubuntu-24.04}; `allowed_cidr` validated.

**Module** (`infra/terraform-workspaces/gcp-gce/main.tf`): generated SSH key (`tls_private_key`, sensitive `private_key_pem`), ephemeral public IP, **network tags = [name]** so the firewall rules actually attach (a real reviewer-caught defect — without tags the firewalls never bound). Two firewalls: `ingress` (day-2 ports, `0.0.0.0/0`) and `admin` (SSH 22 to `allowed_cidr` only). Outputs: instance_id, self_link, public/private IP, login_user, zone.

**Modify:** `_modify_resource` supports `vm` (adds firewall ingress ports) against the resource's own state workspace.

**Full lifecycle (per PROGRESS, consistent with code):** create → apply (`104.198.229.218`) → read → add port 8080 → destroy, recorded in both Postgres inventory and Neo4j graph. GCP VM + AWS EC2 are the two apply-verified compute paths.

## 5.2 GCS bucket — create
Required: **bucket_name** (project auto-filled; location/storage_class defaulted). Module: `uniform_bucket_level_access=true`, versioning on, `force_destroy=false`. `_gcs_policy` asserts those. Success card: "Bucket ready" with name + self_link console.

## 5.3 Cloud SQL (PostgreSQL) — create
Required: **name** (project/region/tier/database_version defaulted). `tier` validated as `db-*`. Module: generated `random_password` root credential (sensitive), `deletion_protection=false` (so day-2 destroy works), a demo `authorized_networks 0.0.0.0/0`. Success card: "Database ready" with endpoint + generated password via Reveal.

## 5.4 GKE — create
Required: **name** (project/region/node_count/machine_type defaulted). Module: `google_container_cluster` with `remove_default_node_pool=true` + a dedicated `google_container_node_pool`, `deletion_protection=false`. Success card: "Cluster ready" with endpoint.

## 5.5 GCP day-2 & gaps
- **Read (broad):** `_discover_gcp` (`cloudops.py:491`) uses `list_all_instances()` (AggregatedList across all zones — real) for running Compute counts + names, and `list_networks()`; storage/db/k8s honestly report "not wired yet — see inventory."
- **Read (specific):** no GCP branch in `inventory.reconcile` — recorded attributes only (only AWS EC2 refreshes live).
- **Modify:** VM firewall-ports only.
- **Verify:** AWS-only reconciliation (`finalize._reconcile_checks`), so GCP applies get only "Terraform outputs present."
- **Infra prerequisite:** GCP applies require the **Compute Engine API enabled** on the project; when it isn't, `provider_errors` classifies the `SERVICE_DISABLED` error and surfaces the exact activation URL from the error text (`provider_errors.py:48`).
