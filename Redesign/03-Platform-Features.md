# 03 — Platform Features

> What the redesigned AegisOps *does*, expressed as objectives, capability catalogs, and coverage
> matrices. Structure follows the mandate: objectives organize reasoning; services and tools are
> what objectives consume. Current-state columns are grounded in the `a974290` audit
> (`01-Current-State-Architecture.md`).

---

## 1. Objective taxonomy

The platform organizes agent reasoning around ten operational objective classes. Each objective is
a *goal with success criteria*, not a workflow script — the harness loop decides dynamically which
clouds, services, tools, and specialists an instance requires.

| # | Objective | Typical verbs exercised | Mutation? | Default gate |
|---|---|---|---|---|
| O1 | Provision workload | discovery → creation → verification | yes | approval (mode-dependent) |
| O2 | Modify infrastructure | inspection → update → verification | yes | approval |
| O3 | Deploy application | repo/CI inspection → deploy → health verification | yes | approval |
| O4 | Investigate failure | discovery → inspection → diagnosis | no | none (read-only) |
| O5 | Restore service | diagnosis → remediation → verification | yes | approval or pre-approved tier |
| O6 | Scale workload | inspection → scaling → verification | yes | approval or pre-approved tier |
| O7 | Migrate workload | cross-inspection → provision → cutover → verification | yes | approval per phase |
| O8 | Diagnose connectivity | multi-layer inspection (net/SG/DNS/LB) | no | none |
| O9 | Remediate incident | O4 + O5 with evidence chain | yes | approval or pre-approved tier |
| O10 | Verify system health | inspection + probes + telemetry correlation | no | none |

Objective contract (every objective instance carries):

```
Objective {
  goal: str                      # user's intent, normalized
  success_criteria: [Check]      # what "done" means — verifiable, not vibes
  constraints: {cloud?, region?, env, budget?, window?}
  evidence_required: [EvidenceKind]
  risk_class: read | low | medium | high | destructive
}
```

The loop pursues the objective; `success_criteria` drive the final goal-validation step — tool
success never substitutes for objective success.

## 2. Required workflow classes (mandate coverage map)

Every workflow the mandate enumerates maps onto objectives + capability packs. None gets a bespoke
deterministic pipeline.

### 2.1 CloudOps — per cloud

| Workflow (AWS / Azure / GCP variant) | Objective | Capabilities exercised |
|---|---|---|
| Create and verify a compute workload (EC2 / VM / GCE) | O1 | compute pack: create template, describe, status probes |
| Stop/start/restart existing workload | O2 (day-2) | day-2 verb registry, state verification |
| Create object storage (S3 / Blob / GCS) | O1 | storage pack; name-availability precheck; policy verification |
| Create + connect managed DB (RDS / Azure SQL / Cloud SQL) | O1 | db pack; network path check; connectivity probe from context |
| Build network infrastructure (VPC / VNet / VPC) | O1 | network pack; CIDR planning; route/subnet verification |
| Create K8s cluster + deploy app (EKS / AKS / GKE) | O1+O3 | k8s pack + DevOps pack composition; multi-step plan; subagent split |
| Deploy via serverless containers (ECS / Container Apps / Cloud Run) | O3 | container pack; image reference from DevOps pack |
| Diagnose unreachable application | O8 | net diagnosis playbook: instance state → SG/NSG/firewall → subnet/routes → LB targets → DNS |
| Diagnose failing K8s deployment | O4 | k8s diagnosis: rollout status → pod events → container logs → image pull/quota/probes |
| Diagnose failed deployment/operation | O4 | run-history inspection + cloud events + TF state comparison |
| Recover a failed infrastructure operation | O5 | saga/compensation, deviation re-approval, re-plan |

### 2.2 Cross-cloud

| Workflow | Objective | Notes |
|---|---|---|
| Find all K8s clusters across AWS/Azure/GCP | O10 | parallel read-only fan-out; one merged inventory answer |
| Find where an application is deployed | O4 | inventory + K8s workload search across accounts |
| Determine which cloud environment is unhealthy | O10 | telemetry correlation per cloud; ranked evidence |
| Compare infrastructure across clouds | O10 | normalized resource model comparison |
| Investigate app failure regardless of host cloud | O4 | locate-first, then cloud-specific diagnosis pack |
| Reproduce a workload in another cloud | O7 | source inspection → equivalence mapping → O1 in target |
| Compare networking configurations across clouds | O10 | normalized net model (CIDR/routes/gateways/SG) |
| Select allowed cloud/region under constraints | O1 pre-step | policy-constrained planning; constraints from org policy pack |

### 2.3 DevOps

| Workflow | Objective | Capabilities |
|---|---|---|
| Investigate failed GitHub Actions workflow | O4 | runs list → failed jobs → **log download** → failure classification |
| Diagnose and fix CI failure | O4→O3 | log diagnosis → patch proposal → **PR flow** (never direct push) |
| Prepare a PR for an infrastructure change | O2 | branch → commit → PR with plan artifact attached |
| Inspect deployment failure | O4 | workflow + environment + k8s correlation |
| Roll back a failed deployment | O5 | release history → previous artifact → gated rollback |
| Verify deployment after CI/CD completion | O10 | post-deploy probes + telemetry + version assertion |

### 2.4 SREOps

| Workflow | Objective | Capabilities |
|---|---|---|
| Investigate an alert | O4 | alert ingest (webhook) → INV loop over telemetry tools |
| Diagnose service degradation | O4 | metrics/logs/traces correlation, per-service PromQL (not hardcoded, not self-referential — fixes F-15) |
| Find root cause of pod restarts | O4 | restart events → OOM/probe/image analysis → deploy correlation |
| Investigate increasing latency | O4 | latency SLI decomposition → dependency graph → recent-change correlation |
| Correlate metrics/logs/traces/deployments | O4 | cross-signal correlation primitives in the SRE pack |
| Remediate an incident | O9 | decision policy → gated remediation (restart/scale/rollback) |
| Verify service recovery | O10 | bake-time re-checks, SLO re-assertion, evidence card |

## 3. Multi-cloud service coverage matrix

Verbs: **D**iscovery · **I**nspection · **C**reation · **U**pdate · **L**ifecycle (stop/start/
restart) · **S**caling · **X** Deletion · **T**roubleshooting · **V**erification.
Cell values: ✅ target-required · ◐ target-optional (phase 2 of coverage) · — not applicable.
`Cur` column = what exists at `a974290` (**W** = write template only, **R** = read tool only,
**WR** = both, **∅** = nothing).

### 3.1 AWS

| Service | Cur | D | I | C | U | L | S | X | T | V |
|---|---|---|---|---|---|---|---|---|---|---|
| EC2 | WR | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| S3 | WR | ✅ | ✅ | ✅ | ✅ | — | — | ✅ | ✅ | ✅ |
| RDS | WR | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| VPC | WR | ✅ | ✅ | ✅ | ✅ | — | — | ✅ | ✅ | ✅ |
| EKS | WR | ✅ | ✅ | ✅ | ✅ | — | ✅ | ✅ | ✅ | ✅ |
| ECS | ∅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Lambda | ∅ | ✅ | ✅ | ✅ | ✅ | — | ◐ | ✅ | ✅ | ✅ |
| IAM | W(kms) | ✅ | ✅ | ◐ | ◐ | — | — | ◐ | ✅ | ✅ |
| CloudWatch | ∅ | ✅ | ✅ | ◐ | ◐ | — | — | ◐ | ✅ | ✅ |
| ELB/ALB/NLB | W | ✅ | ✅ | ✅ | ✅ | — | — | ✅ | ✅ | ✅ |

### 3.2 Azure

| Service | Cur | D | I | C | U | L | S | X | T | V |
|---|---|---|---|---|---|---|---|---|---|---|
| Virtual Machines | WR | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Blob Storage | W | ✅ | ✅ | ✅ | ✅ | — | — | ✅ | ✅ | ✅ |
| Azure SQL | W | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Virtual Network | WR | ✅ | ✅ | ✅ | ✅ | — | — | ✅ | ✅ | ✅ |
| AKS | W | ✅ | ✅ | ✅ | ✅ | — | ✅ | ✅ | ✅ | ✅ |
| Container Apps | ∅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Azure Functions | ∅ | ✅ | ✅ | ✅ | ✅ | — | ◐ | ✅ | ✅ | ✅ |
| Entra ID / RBAC | ∅ | ✅ | ✅ | ◐ | ◐ | — | — | ◐ | ✅ | ✅ |
| Azure Monitor | ∅ | ✅ | ✅ | ◐ | ◐ | — | — | ◐ | ✅ | ✅ |
| LB / App Gateway | ∅ | ✅ | ✅ | ✅ | ✅ | — | — | ✅ | ✅ | ✅ |

### 3.3 GCP

| Service | Cur | D | I | C | U | L | S | X | T | V |
|---|---|---|---|---|---|---|---|---|---|---|
| Compute Engine | WR | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Cloud Storage | W | ✅ | ✅ | ✅ | ✅ | — | — | ✅ | ✅ | ✅ |
| Cloud SQL | W | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| VPC | WR | ✅ | ✅ | ✅ | ✅ | — | — | ✅ | ✅ | ✅ |
| GKE | W | ✅ | ✅ | ✅ | ✅ | — | ✅ | ✅ | ✅ | ✅ |
| Cloud Run | ∅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Cloud Functions | ∅ | ✅ | ✅ | ✅ | ✅ | — | ◐ | ✅ | ✅ | ✅ |
| IAM | W(kms) | ✅ | ✅ | ◐ | ◐ | — | — | ◐ | ✅ | ✅ |
| Cloud Monitoring | ∅ | ✅ | ✅ | ◐ | ◐ | — | — | ◐ | ✅ | ✅ |
| Cloud Load Balancing | ∅ | ✅ | ✅ | ✅ | ✅ | — | — | ✅ | ✅ | ✅ |

### 3.4 Coverage rules (how the matrix is honest)

1. **Verb → tool-class mapping is fixed:**
   D/I/T/V verbs = read-effect SDK tools (registry-registered, denylist-checked).
   C/U/X verbs = Terraform catalog templates only.
   L/S verbs = day-2 verb registry (the single audited SDK-write file) *or* Terraform where the verb
   is a desired-state change (e.g., resize = `-var instance_type`).
2. **Parity gate:** a service family ships only when all three clouds reach the same verb set for
   it (per-row, not per-cloud waves) — this prevents the current failure mode (write-symmetric,
   read-asymmetric: Azure 3 / GCP 2 read services vs 7/6 write templates, `01 §2.6`).
3. **Every C/U/X verb requires:** template schema + real policy predicates (no `_todo` blanket
   rows — closes F-11) + verification strategy + compensation ref (or explicit `irreversible`).
4. **Every L/S verb requires:** preconditions + inverse verb + state-read verification.
5. **Troubleshooting (T)** is not a tool — it is a diagnosis playbook (procedural knowledge) the
   INV loop uses over the read tools of that service family.
6. IAM C/U is deliberately ◐: identity mutation is the highest-risk class; ships behind
   `destructive` risk class with mandatory human approval regardless of mode.

## 4. DevOps capability catalog

| Capability | Current (`a974290`) | Target |
|---|---|---|
| Repository inspection | `get_repo`, `repo_exists` | + contents, branches, tags, languages, CODEOWNERS |
| Branches / commits | `upsert_file` direct-to-branch | branch create, commit batches, diff read; **direct default-branch pushes banned by policy** |
| Pull requests | client method exists, never called (D7) | PR create/read/review-state/merge-when-green; PR is the default change vehicle |
| Code changes | placeholder Dockerfile/CI pushed (F-14) | change proposals as PRs with rendered artifacts; no hardcoded scaffolding |
| GitHub Actions | dispatch + find-run + poll | + workflow list/inspect, **job-level status** |
| Workflow logs | **none** | log download + failure extraction (the #1 CI-diagnosis primitive) |
| Failed jobs / reruns | none | failed-job identification, `rerun` (all / failed-only) as day-2-class verbs |
| Release workflows | none | releases list/create-draft, tag inspection |
| Deployment workflows | hardcoded K8s manifest apply | environment-aware deployment via governed K8s executor; image ref verified in registry (real ENSURE_IMAGE_EXISTS) |
| Container/image workflows | CI poll pretends to be image check (F-14) | registry inspection (GHCR/ECR/ACR/GAR): tag exists, digest, scan status |
| Rollback | none | previous-release redeploy through the same gated path |
| Verification | CI conclusion == success | deployment probes + version assertion + telemetry check (evidence card) |

## 5. SREOps capability catalog

| Capability | Current | Target |
|---|---|---|
| Kubernetes | list ns/deploy/pods; mutate: apply/restart/scale/rollback | + events, logs, describe (pod/node), rollout status/history; mutations only via day-2 registry |
| Cloud infrastructure | via cloud read tools (asymmetric) | symmetric read packs (§3) |
| Logs | none (K8s logs unavailable) | pod logs, Loki/CloudWatch/Azure Monitor/Cloud Logging query tools (read-effect) |
| Metrics | 5 hardcoded PromQL, self-referential error rate (F-15) | per-service query templates + free-form PromQL (read-effect, bounded range/step); target-service scoping mandatory |
| Traces | none | trace-search tool (Langfuse/OTel backend) for request-path diagnosis |
| Alerts | none inbound | Alertmanager webhook ingress → incident run (`source="alert"`, dedupe by fingerprint) |
| Events | none | K8s events + cloud activity/audit-log readers |
| Deployments | list only | deploy-history correlation (what changed near T₀) |
| CI/CD | none from SRE side | shared DevOps pack read tools |
| Application health | up/5xx only | HTTP/TCP probes, SLO evaluation, synthetic checks |
| Diagnosis | one hardcoded `list_deployments` call | INV loop (bounded, read-only, model-directed) over the whole read surface |
| Remediation | restart/scale/rollback behind approval | same three + catalog day-2 verbs; **pre-approved tier** for org-listed low-blast actions, rate-limited, audited, verified |
| Postmortem | none | postmortem draft artifact generated from run evidence; lessons → memory *proposals* |

## 6. Platform-level features (cross-domain)

### 6.1 Permission modes (product feature, not just policy plumbing)

| Mode | Read tools | Compile plan artifact | Request approval / execute | Notes |
|---|---|---|---|---|
| READ_ONLY | ✅ | ❌ (advisory prose only) | ❌ / ❌ | investigation/audit personas |
| PLAN_ONLY | ✅ | ✅ full artifact (plan, cost, blast radius, verification plan, rollback plan) → `plan_ready` | ❌ / ❌ — zero mutation; executing a ready plan is a new run, re-validated + approved | CAB/change-window prep; full semantics in 09 §2 |
| APPROVAL_REQUIRED | ✅ | ✅ | ✅ / post-approval; granularity via `ApprovalPolicy` (PER_STEP_HIGH · SINGLE_DAG · PRE_APPROVED); deviations re-approved | **default for new orgs**; today's model, formalized |
| AUTONOMOUS | ✅ | ✅ | auto-execute **within budgets + risk ceiling + pre-approved verb list**; destructive ops always gated | bounded, per-env, never for `destructive` risk class |

Approval *granularity* is deliberately not a mode: the former "ASSISTED" concept is
`APPROVAL_REQUIRED` with per-step `ApprovalPolicy` (see 09 §2 for the resolution).
Mode is org × environment × risk-class policy (e.g., AUTONOMOUS in dev, APPROVAL_REQUIRED in prod).
Every approval artifact stamps the active mode + governance flags (closes D9/F-9 drift invisibility).

### 6.2 Budgets (enforced, visible)

Per-run and per-org-daily: tokens, dollars, iterations, tool calls, wall clock, mutation count.
Breach → halt at safe boundary → honest partial + resume-after-raise affordance. Ledger-backed
(`llm_usage`), dashboarded, chargeback-ready.

### 6.3 Task/Run operations

Durable runs resumable across restarts; background execution decoupled from HTTP; cancellation at
boundaries; user steering mid-run (queued, consumed at iteration boundaries); run history with full
evidence trail; deep links (`?run=`).

### 6.4 Channels

Web SPA + Telegram (existing) → Slack, Teams on the same Transport Protocol; alert webhook ingress.
Channel rule (constitution): *the channel proves who clicked; the core decides whether the click
counts.* All channel output redacted + confidentiality-gated.

### 6.5 Observability & evaluation surface

Live flow view (run_steps-driven) with stalled-step tell; served-by model badges per message;
fallback-hop visibility; spend dashboards (org/purpose/model); eval-gate history; approval-wait,
drift, stranded-runs actually charted (closes the 4-of-11 Grafana gap); `/metrics` authenticated.

### 6.6 Knowledge & memory features

Governed memory proposals (consolidation → human accept); org policy packs (standing constraints
injected into planning); runbook RAG with citations; per-org operational lessons with provenance
(details: `06-Memory-Context-and-Execution.md`).
