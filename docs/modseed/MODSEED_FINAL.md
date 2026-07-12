# MODSEED — FINAL requirement (supersedes any earlier MODSEED spec/addendum text)

Phase-3 checklist item, positioned immediately BEFORE MPP. Goal: grow the approved module
library 14 → 20 and upgrade existing modules, such that a user in the chatbot can perform
the full lifecycle on every new type through the SAME governed pipeline — routing → params
→ plan → real policy checks → approval → apply → success card → inventory → day-2 →
world model — with zero disturbance to existing workflows and resources.

## 1. Non-negotiables
- Flat layout only: `infra/terraform-workspaces/<module>/`. Never nested per-cloud dirs;
  never touch TerraformRunner path resolution.
- Multi-file modules (main/variables/outputs.tf) are permitted and PREFERRED for the
  larger MODSEED modules — terraform merges every .tf in a dir; the runner and the
  registry↔disk test validate the DIRECTORY, not a filename. Still no backend blocks,
  no tfvars, no provider files carrying variables.
- A module EXISTS only when registered. Module dir + FULL registration land in ONE commit:
  templates.py key + synonyms · Pydantic schema · params entry · REAL policy predicate
  (U1 style, from plan JSON) · DEP Slots where parents exist · inventory + destroy support
  · world-model extraction fields · C1 plan-assertion test · faked-runner plan test.
- Registry↔disk consistency test stays green: every workspace dir maps to a registered
  template (allowlist: demo-null) and validates.
- No backend blocks in modules (A3 injects); no hardcoded regions; providers pinned to the
  repo's current majors (no bumps in MODSEED).
- fmt/validate/Checkov/tfsec clean per module; live applies → DEFERRED LIVE VERIFICATION
  with exact replay steps. Never fake a live result.
- NEVER import: tfvars files (several source ones contain literal passwords), `backend
  "pg"`, hardcoded us-east-1, the source AWS/LB folder (mislabeled — it is a second VPC
  module), Ani-playbooks/Operations playbooks (Ansible/SDK mutation outside the governed
  pipeline), world-open (`*`/0.0.0.0/0) admin ingress anywhere, password-as-variable DB
  inputs, instance_count>1 multi-instance patterns (inventory is one-resource-per-row).

## 2. Six new modules (author fresh; one commit each, this order)
1. **gcp-vpc** (`gcp.vpc`; syn: network, vpc). Custom-mode network + regional subnets
   (cidr list var, default 10.10.0.0/16 split) with private_ip_google_access + optional
   flow logs + secondary IP ranges per subnet (pods/services — recorded in outputs/
   attributes so GKE can be DEP-placed later); per-region Cloud Router + NAT for private
   subnets (enable_nat, logging ERRORS_ONLY); internal firewall scoped to subnet CIDRs
   ONLY (no admin/SSH rules — the VM module owns admin ingress via allowed_cidr).
   Required: name (project auto). Outputs: network id/name/self_link, subnet ids+cidrs,
   secondary range names. Policy: custom-mode on, ≥1 subnet.
2. **azure-vnet** (`azure.vnet`; syn: vnet, network). VNet + subnets (address_space default
   10.20.0.0/16; subnets list default one /24) + NAT gateway (+static IP+assoc) +
   public/private route tables with name-based association; RG handling like azure-vm
   (auto `<name>-rg` or existing). NO NSG allowing admin from `*` — omit or default-deny.
   Required: name. Outputs: vnet id/name, subnet ids, RG. Policy: ≥1 subnet, RFC1918.
3. **aws-nlb** (`aws.nlb`; syn: load balancer, lb, nlb). aws_lb type=network (cross-zone
   on) + TCP target group (health check TCP/30s/threshold 3/traffic-port) + TCP listener
   + auto egress-only SG when none given. deletion_protection VAR, default true when
   env=Production. Required: name; target_port default 80. DEP Slots: vpc_id + subnets
   (parent aws.vpc; subnets ← the VPC's recorded public_subnet_ids; two VPCs → ask;
   none → create-first DAG). Outputs: lb_dns_name, lb_arn, target_group_arn. Card: DNS
   name + "attach targets to <tg arn>".
4. **aws-kms** (`aws.kms`; syn: kms, key, encryption key; secrets→kms). KMS key
   (deletion_window default 30, rotation default ON) + alias `alias/<name>` + key policy
   (root admin via caller identity + allowed_services var default [secretsmanager, rds]
   with Decrypt/DescribeKey/CreateGrant). Required: name. Secret VALUES are OUT of scope
   forever (never chat-supplied). Policy: rotation on, deletion_window ≥ 7. Destroy card:
   "enters a scheduled-deletion window, not immediate".
5. **azure-keyvault** (`azure.keyvault`; syn: key vault, kv, vault). Vault (sku standard,
   soft-delete ≥7, purge_protection var default true, network_acls: bypass AzureServices,
   default_action var — state it on the card when Allow) + current-SP access policy +
   optional additional policies map + optional keys map (RSA 2048). RG via the existing
   resource_group Slot pattern. Required: name. No secret values. Outputs: vault_id/uri.
6. **gcp-kms** (`gcp.kms`; syn: kms, keyring, key). Key ring + crypto key(s) (rotation
   90d default, ENCRYPT_DECRYPT, SOFTWARE protection; default one key `<name>-key`) +
   encrypter/decrypter IAM members var. Required: name (project auto). Destroy card MUST
   say: GCP key rings are not deletable — destroy removes key versions/IAM only.

## 3. Enhancements to existing modules (7–13, after the six; separate commits)
7. **aws-rds** multi-engine (postgres/mysql/mariadb) + latest-engine-version data source +
   dedicated SG with MANDATORY allowed_cidr (no 0.0.0.0/0 default ever) + subnet group +
   engine-aware ports/log-exports + sensitive connection-string outputs. KEEP
   manage_master_user_password=true.
8. **azure-postgres → azure.db** multi-engine (postgresql/mysql/mssql) + optional HA mode,
   geo-redundant backup, delegated subnet/private DNS. KEEP generated random_password.
   Keep `azure.postgres` as synonym; the WORKSPACE DIR NAME does not change (rule B3).
9. **gcp-cloudsql**: optional private-VPC-peering + backup/PITR + maintenance window +
   query insights + ssl_mode + deletion_protection var + optional CMEK encryption_key_name
   (DEP Slot on gcp.kms: existing ring → offered; none → optional, never forced).
10. **aws-ec2**: optional SSM+CloudWatch instance profile; card adds "Session Manager
    access available" when on.
11. **eks-provision**: eks_mode=auto|standard — auto → API auth mode, compute_config
    (general-purpose pool), elastic-LB + block-storage configs, auto-mode IAM policy set;
    standard → current node-group path. Card states the mode.
12. **gcp-gce**: shielded_instance_config option, OS Login option, preemptible/spot option
    (maintenance implications stated on the card), optional least-scope service account.
    KEEP generated SSH key + one-time reveal.
13. **azure-aks** (optional, last): Log Analytics + OMS agent, network_policy=calico,
    azure_policy_enabled var.

## 4. BACKCOMPAT rules (bind items 7–13; these WIN over anything above)
B1. **No-op re-plan gate per enhanced module**: a resource created with pre-enhancement
    inputs, re-planned with the enhanced module from its STORED inventory inputs, produces
    ZERO changes. This test gates every enhancement commit.
B2. Plan-shape-changing options default to OLD behavior at the schema level (enable_ssm
    =false, cloudsql public path preserved, eks_mode=standard, shielded off, AKS
    observability off). New creates get new capability via params/collection (suggested/
    asked), never via a default that mutates existing resources' re-plans.
B3. Workspace dir names are immutable; existing inventory rows' workspace refs and day-2
    destroy keep working — prove with a test.
B4. New DEP Slots on existing templates (gcp.vm→network, azure.vm→vnet) change placement
    behavior BY DESIGN: update affected DEF/U4/plan tests deliberately, record each
    behavior change in PROGRESS.md; never weaken a test to pass.
B5. Canary gate after EVERY MODSEED commit: the pre-MODSEED e2e flows (GCP VM full
    lifecycle, EC2/S3 apply paths, day-2 modify on an existing resource) stay green.

## 5. Seamless-operation contract (the acceptance bar per new module)
For EACH of the six, a user typing natural language in the chatbot gets the complete
governed flow with no dead ends. Per-module integration test (faked runner, live
datastores) + a scripted UI walkthrough entry in DEFERRED LIVE VERIFICATION:
- "create a kms key named app-secrets in aws" → routed (synonyms), params card if needed,
  plan, REAL policy checks, approval card, apply, success card with real outputs,
  inventory row, world-model node.
- "what's the rotation on app-secrets" → day-2 read from inventory (recorded values).
- "create a load balancer web-lb in aws" → DEP: one VPC → subnets filled from its
  recorded outputs (provenance on the card); two → asks; none → create-first DAG
  (vpc → nlb) via the executive loop, ONE approval.
- "create a vm in my-vnet" (azure) / "create a vm in prod-network" (gcp) → placed into
  the EXISTING network from the world model, stated on the Defaults/provenance card.
- "destroy app-secrets" → gated destroy; the card carries the module's honest deletion
  semantics (KMS scheduled window / GCP ring not deletable); impact_of consulted.
- Unknown-type request ("create a lambda") → the UNSUP honest-catalog answer + MPP offer
  (unchanged).

## 6. Owner decisions — do NOT implement
- VM start/stop (SDK mutation; conflicts with invariant 2): skip; present options at
  MODSEED end only.
- ALB: becomes MPP's first drafted proposal.

## 7. Order + stops
Insert MODSEED into FIX.md §8 immediately before MPP. Execute in checklist order when
reached (finish the current item first). One commit per module/enhancement, suite green
every time, trackers updated. **STOP with the evidence table after modules 1–6** (tests,
scans, registry↔disk, canaries, seamless-contract tests) before enhancements 7–13; STOP
again after 13 before MPP.
