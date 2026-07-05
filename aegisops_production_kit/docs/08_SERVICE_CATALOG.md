# 08 — Multi-cloud Service Catalog

The catalog is the set of curated, version-pinned Terraform modules the CloudOps agent can
provision. Every entry is **real HCL** (never LLM-authored); the LLM only classifies intent,
collects/validates parameters, selects a module, and passes variables. Provisioning always runs
through the human-approval gate; created resources are recorded in the DB inventory + Neo4j
context graph for day-2 operations.

## Supported matrix (current)

| Cloud | Compute / VM | Object storage | Managed DB | Kubernetes | Network / other |
|-------|--------------|----------------|------------|------------|-----------------|
| AWS   | EC2 (`aws-ec2`) ✅ | S3 (`aws-s3`) ✅ | RDS (`aws-rds`) | EKS (`eks-provision`) | VPC (`aws-vpc`) |
| Azure | VM (`azure-vm`) | Storage (`azure-storage`) | PostgreSQL Flexible (`azure-postgres`) | AKS (`azure-aks`) | Resource Group (`azure-resource-group`) |
| GCP   | Compute (`gcp-gce`) ✅ | GCS (`gcp-gcs`) | Cloud SQL (`gcp-cloudsql`) | GKE (`gcp-gke`) | — |

✅ = full create→apply(→destroy) verified live. All other entries are real modules whose
cloud-specific plans are verified live; live applies are gated by sandbox credentials/permissions
(e.g. an Azure SP needs Contributor to create resource groups) and are deferred, not faked.

## How the frameworks fit together
- **Routing (Phase 2):** `templates.select(cloud, resource)` maps the resolved cloud + resource
  (with per-cloud synonyms: "vm"/"database"/"k8s") to exactly one module — no cross-cloud fallback.
- **Collection (Phase 3):** `params.PARAMS[key]` declares each parameter; only decision-critical ones
  (no safe default) are asked, the rest default and are overridable. Validated by the Pydantic schema.
- **Credentials:** VM modules generate an SSH key (private key = sensitive output, surfaced once, never
  logged); DB modules generate the admin/root password (sensitive). `TerraformRunner` injects AWS
  (`AWS_*`), Azure (`ARM_*`), and GCP (`GOOGLE_*` + mounted SA key) credentials.
- **Inventory + day-2 (Phase 4):** on apply, the resource is recorded in Postgres + the context graph
  (resource↔run↔session). Day-2 read works for every module; day-2 modify (inbound ports) works for
  compute (EC2/Azure VM/GCP VM). Destroy marks it destroyed in both stores.

## Add a new service (module + schema + registration)
1. **Module** — `infra/terraform-workspaces/<cloud>-<service>/main.tf`: real, version-pinned HCL.
   Pin `required_providers`; emit useful `output`s; mark any secret output `sensitive = true`.
2. **Schema** — a `WorkflowInputs` subclass in `backend/app/schemas/workflows.py`: fields mapped to the
   module's Terraform variables, with types, `field_validator`s, and safe defaults.
3. **Template** — a `WorkflowTemplate(key, cloud, resource, version, workspace, schema, description,
   policy_fn)` entry in `backend/app/agents/templates.py` (add resource synonyms to `_SYNONYMS` if the
   users' words differ from the canonical resource).
4. **Params** — a `ParamSpec` list under the template key in `backend/app/agents/params.py`; mark only
   the decision-critical params `required=True`.

Inventory recording, context-graph relationships, day-2 read, credential surfacing, and the approval
gate are all inherited — no per-module wiring needed. Run `terraform validate` on the new workspace,
then verify: route → collect → plan → (apply where creds allow) → inventory.
