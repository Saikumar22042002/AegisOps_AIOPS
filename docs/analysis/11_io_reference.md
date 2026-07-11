# 11 — Input / Output reference (cheat sheet)

[← back to index](../../ANALYSIS.md)

"What to type / what you get." Inputs are the **decision-critical** params the agent asks for (from `agents/params.py`); everything else defaults silently and is overridable by naming it. Say the cloud explicitly (or set the UI selector) — the compute paths won't infer it.

## Create

| Say something like | Resolves to | Agent asks for (required) | Defaults applied | You get back |
|--------------------|-------------|---------------------------|------------------|--------------|
| "create an EC2 instance in AWS" | `aws.ec2` | name, instance_type, os, key_pair, allowed_cidr | region, vpc/subnet (default VPC), root vol (AMI size), gp3 | Approval card → apply → "✅ Instance ready" (id, public DNS/IP, login user, `ssh` line, Reveal private key) |
| "create an S3 bucket" | `aws.s3` | bucket_name | region, versioning=on, block_public=on | "✅ Bucket ready" (name, ARN, region, console link) — global-name precheck may re-ask |
| "provision an RDS postgres db" | `aws.rds` | identifier | engine=postgres, db.t3.medium, 20GiB | "✅ Database ready" (endpoint; password AWS-managed) — plan-verified |
| "create a VPC named prod-net" | `aws.vpc` | name | 10.0.0.0/16, 3 AZ, NAT on | "✅ Network ready" (VPC id, CIDR, subnets) |
| "provision an EKS cluster" | `aws.eks` | cluster_name, vpc_id, subnet_ids | k8s 1.29, m6i.xlarge×3 | Approval → plan (private endpoint, IRSA, KMS) |
| "create a VM in Azure" | `azure.vm` | name, size, os, allowed_cidr | eastus, admin=azureuser, RG=`<name>-rg` | "✅ Instance ready" (Linux SSH key **or** Windows password via Reveal) — apply-deferred |
| "create a storage account in Azure" | `azure.storage` | account_name, resource_group (existing) | eastus, Standard/LRS | Storage account (TLS1.2, no public blobs) |
| "create a resource group rg-x" | `azure.resource_group` | name | eastus | RG id |
| "create a postgres server in Azure" | `azure.postgres` | name | eastus, B_Standard_B1ms, pg15, own RG | FQDN + generated admin password (Reveal) — apply-deferred |
| "create an AKS cluster" | `azure.aks` | name | eastus, 2×Standard_B2s | FQDN; kubeconfig sensitive — apply-deferred |
| "create a VM in GCP" | `gcp.vm` | name, machine_type, os, allowed_cidr | project auto, us-central1-a, ssh_user=aegis | "✅ Instance ready" (public IP, SSH key via Reveal) — **apply-verified** |
| "create a GCS bucket" | `gcp.gcs` | bucket_name | project auto, US, STANDARD | Bucket (uniform access, versioned) |
| "create a Cloud SQL postgres" | `gcp.cloudsql` | name | project auto, us-central1, db-f1-micro, POSTGRES_15 | Endpoint + generated root password (Reveal) — apply-deferred |
| "create a GKE cluster" | `gcp.gke` | name | project auto, 2×e2-medium | Endpoint — apply-deferred |

## Read

| Say something like | Path | You get back |
|--------------------|------|--------------|
| "how many EC2 instances are running in AWS?" | `_read_path` (broad, action=read) | Live counts per named cloud (running EC2 + names, S3 buckets, RDS, EKS, VPC) + the AegisOps inventory listing. No approval, no Terraform. |
| "did I create any resources?" / "list my resources" | broad inventory read | Grouped-by-cloud listing of every active inventoried resource (honest empty state). |
| "what's the VPC id of test-vm?" | `_read_resource` (specific) | test-vm's **real recorded** values (size, region, VPC, subnet, private/public IP, DNS, SG, key, open ports, status) — EC2 reconciled live; provenance from the graph. |
| "the s3 bucket I created" | `_read_resource` (typed-descriptive) | Only an s3/gcs/storage resource — never an EC2 (type-safe). |

## Modify (day-2)

| Say something like | Path | You get back |
|--------------------|------|--------------|
| "add inbound ports 8501,8502 to test-vm" | `_modify_resource` (ec2/vm only) | Re-planned SG/NSG/firewall change in the resource's own state → **approval gate** → apply → inventory updated. |
| "modify my S3 bucket" | `_modify_resource` | Refused: "I can currently modify inbound ports on compute instances (AWS EC2, Azure/GCP VM)." |

## Destroy (day-2)

| Say something like | Path | You get back |
|--------------------|------|--------------|
| "destroy the VM sai-test" | `_destroy_resource` | Resolves sai-test from inventory → `-destroy` plan (deletes-only, guarded) → **approval gate** → apply → marked destroyed in both stores. |
| "delete everything" | `_destroy_resource` (broad) | Refused: "I don't bulk-destroy in one shot — tell me exactly which resource." |
| "remove the vpc" (no explicit target) | intent guard | If not explicitly destructive → clarification; if inventory can't find it → "I only tear down what I created." |

## Other domains

| Say something like | Domain | You get back |
|--------------------|--------|--------------|
| "deploy repo owner/app to prod" | devops | 6-stage GitHub→CI→K8s pipeline plan → approval → real repo/Actions ops (CI status read; K8s deploy needs an `image`). |
| "why did checkout latency spike after the 14:20 deploy?" | sre | Telemetry (Prometheus) + RAG runbooks + decision matrix + Gemini triage. Remediation is **proposed** (execution is a no-op today — see [09 P7](09_problems.md)). |
| "explain how EKS IRSA works" | knowledge | RAG-grounded answer with citations (pgvector cosine; trigram fallback). |
| "hi" / general chat | general | Gemini answer with session transcript threaded in. |

**Credentials:** any VM/DB with a generated secret shows a **Reveal credential** button → `POST /runs/{id}/credentials` returns the value **once** (raw `terraform output -raw`, never logged/persisted). ⚠ This endpoint is currently under-authorized ([09 P1](09_problems.md)).
