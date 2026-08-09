# 00 — Redesign Mandate

> **Status:** Approved direction, pre-implementation. No production code changes are authorized by this
> document. It defines *what AegisOps must become* and the boundaries every subsequent design document
> (01–08) must honor.
>
> **Grounding:** Every current-state claim in this suite is audited against commit `a974290`
> (branch `feature/cloudops-v3`). Reference-architecture claims come from source-level study of
> Hermes (NousResearch), OpenClaw, Pi, and the local Waku agent — implementations, not READMEs.

---

## 1. Mission

Transform AegisOps from a **single-pass, deterministic, Gemini-bound LangGraph pipeline** into a
**production-grade intelligent operations platform** in which a governed agent runtime — the
**Agent Harness** — autonomously pursues operational objectives across CloudOps, DevOps, and SREOps,
under explicit, enforced boundaries.

AegisOps must behave like a capable Cloud Engineer, DevOps Engineer, and SRE operating across real
infrastructure: it inspects before it acts, plans before it mutates, asks only when genuinely blocked,
obtains approval where policy demands it, observes and verifies everything it does, diagnoses and
re-plans when reality disagrees with the plan, and stops when its budgets or boundaries say stop.

## 2. North Star

AegisOps autonomously completes complex operational objectives while **dynamically** determining when
it must:

`inspect · reason · plan · ask · obtain approval · select tools · execute · observe · diagnose · retry · re-plan · delegate · verify · continue · stop`

The goal is **not** unrestricted autonomy. The goal is **bounded operational intelligence** with explicit:

| Boundary class | Enforced where |
|---|---|
| Permission boundaries | Policy engine, evaluated per action selection (never per prompt) |
| Security boundaries | Tenancy, RBAC/ABAC, credential scoping, redaction — inherited from today's governed core |
| Approval policies | Durable approval interrupts; four-eyes for production; deviation re-approval |
| Cost budgets | Token/dollar ledger with halt-at-safe-boundary semantics |
| Iteration budgets | Hard per-run and per-loop iteration ceilings |
| Tool limits | Per-run tool-call budgets; per-tool timeouts, retry policies |
| Runtime limits | Wall-clock ceilings; cancellation honored at step boundaries, never mid-apply |
| Mutation/risk limits | Risk-class budgets (e.g., N mutations per run; destructive ops always gated) |
| Auditability | Every action, observation, approval, and verification is a durable, replayable record |
| Verification | Tool success ≠ task success; every objective ends in evidence-backed goal validation |

## 3. Core architectural principle: HARNESS-FIRST

**Do not make the existing CloudOps agent smarter by adding more deterministic code.** That path
produced today's architecture: 1,500+-line domain agents that hand-roll planning, prompting,
verification, and error handling per domain, around a reasoning layer that cannot iterate.

Instead, invert the structure. The **Agent Harness owns generic intelligence and runtime capability**;
domains become **thin specialist layers** that contribute knowledge, tools, and verification
strategies — never control flow.

```
Agent Harness  (owns: loop, model routing, tool execution, memory, policy checks,
    |           budgets, hooks, subagents, verification orchestration, durable runs)
    +-- CloudOps   (thin: cloud knowledge, cloud tools, cloud verification strategies)
    +-- DevOps     (thin: GitHub/CI knowledge, repo/workflow tools)
    +-- SREOps     (thin: telemetry knowledge, diagnosis playbooks, K8s tools)
    |
    +-- Tools · Skills · Memory · Models · Policies · Approvals
    +-- Verification · Evaluation · Observability
```

A capability implemented in the harness is available to every domain for free. A capability
implemented inside a domain agent is a bug in the making — it will be duplicated, drift, and
eventually contradict its siblings.

## 4. The intelligent execution loop

The core runtime is a genuine loop, not a retrying pipeline:

```
OBSERVE → REASON → PLAN → SELECT ACTION → POLICY CHECK → ACT → OBSERVE RESULT → VERIFY
   ↑                                                                    |
   |          failure / incomplete progress                             |
   +--- OBSERVE FAILURE → DIAGNOSE → GATHER EVIDENCE → RE-PLAN ---------+
                    → RETRY | ALTERNATIVE ACTION | ASK USER → VERIFY
```

Non-negotiable loop properties:

1. **Failed tool calls are first-class observations.** An error string enters the model's next
   context; it never crashes the run and is never silently swallowed.
2. **The agent can change its approach based on tool results.** Re-planning is implicit in
   re-reasoning over accumulated observations — not a bolted-on "replanner" that returns `None`
   (today's `exec_loop.py:46` literally does this).
3. **Every action selection passes a policy check** before execution — reads flow freely under
   READ_ONLY-compatible policy; mutations route through approval according to mode and risk class.
4. **Budgets are enforced inside the loop**, not observed after it.
5. **The loop is durable.** Its state survives process restarts; an approval can be granted days
   later from a different worker and the run resumes exactly where it paused.

Explicitly banned: a "fake loop" where a fixed deterministic workflow retries itself and calls that
iteration.

## 5. Multi-cloud requirement

AWS, Azure, and GCP are **equal first-class platforms**. The design must not treat AWS as the
reference implementation with Azure/GCP adapters bolted on. The harness is cloud-neutral;
cloud-specific knowledge and tools live in cloud-specific capability packs.

Required core service coverage (verbs per service: discovery · inspection · creation · update ·
lifecycle · scaling · deletion · troubleshooting · verification — applicability per service defined
in `03-Platform-Features.md`):

| AWS | Azure | GCP |
|---|---|---|
| EC2 | Virtual Machines | Compute Engine |
| S3 | Blob Storage | Cloud Storage |
| RDS | Azure SQL | Cloud SQL |
| VPC | Virtual Network | VPC |
| EKS | AKS | GKE |
| ECS | Container Apps | Cloud Run |
| Lambda | Azure Functions | Cloud Functions |
| IAM | Entra ID / RBAC | IAM |
| CloudWatch | Azure Monitor | Cloud Monitoring |
| ELB/ALB/NLB | LB / App Gateway | Cloud Load Balancing |

**No giant deterministic service workflows.** Coverage is expressed as tools + knowledge + templates
+ verification strategies that the loop composes per objective.

## 6. Objective-driven design

The architecture organizes agent reasoning around **operational objectives**, not resource-CRUD
functions:

`provision workload · modify infrastructure · deploy application · investigate failure ·
restore service · scale workload · migrate workload · diagnose connectivity ·
remediate incident · verify system health`

`create_ec2 / create_vm / create_gke` are tools an objective may use — never the organizing unit of
the platform. The agent dynamically determines which cloud, services, and tools an objective
requires. The workflow classes this must naturally support (AWS/Azure/GCP/cross-cloud/DevOps/SRE
examples) are enumerated in `03-Platform-Features.md`.

## 7. What must be preserved (the constitution)

The current system's governance core is its moat and survives every change in this redesign:

1. **Terraform-only mutation through the approved template catalog** — the LLM proposes data;
   deterministic code executes it. No LLM-authored HCL. No SDK mutation outside the audited
   day-2 verb registry.
2. **Durable human-approval interrupt** — resumable cross-process, days later.
3. **Plan guard re-asserted at the mutation choke-point** (create may not delete, etc.).
4. **Strict tenancy, RBAC, four-eyes for production** — re-checked at routes, approval core,
   mutation choke-point, and per gateway interaction.
5. **Per-step idempotency; cancel at boundaries, never mid-apply; honest partial reporting.**
6. **Redaction on every egress; trace_id == run_id; immutable approval and audit records.**

The redesign moves these from *implicit properties of a LangGraph topology* to *explicit contracts
of the harness* — which is precisely why the migration must be strangler-style and eval-gated
(see `07-Migration-and-Implementation-Plan.md`).

## 8. What success looks like

Input: *"Create an EKS cluster and deploy the application from my GitHub repository."*

Unacceptable (today's shape): `router → cloudops → create_eks() → deploy() → done`.

Required runtime behavior:

```
understand objective → inspect GitHub repo → inspect AWS account/region
→ inspect VPC/subnets/IAM → inspect existing Terraform state
→ determine deployment requirements → build plan
→ ask ONLY for genuinely missing information
→ select CloudOps + DevOps capabilities → policy check → approval
→ Terraform execution → observe
→ on failure: diagnose → gather evidence → re-plan → retry (bounded)
→ deploy application → verify workload
→ SRE verification (health, endpoints, telemetry)
→ persist useful operational knowledge (as governed memory proposals)
→ return an evidence-backed result
```

Measurable acceptance criteria for the redesigned platform:

- **A second model provider is a configuration change**, not a rewrite (today: rewriting `agents/llm.py`).
- **A failed tool call visibly alters the agent's next action** in the trace (today: impossible — nothing loops).
- **Every mutation in every trace shows: policy evaluation → approval artifact → execution →
  verification evidence** — no exceptions.
- **A run interrupted by process death resumes** from its last durable step on another worker.
- **A run halts on budget** (cost/iteration/runtime) with an honest partial report, at a safe boundary.
- **Azure and GCP objectives exercise the same harness code paths as AWS** — zero AWS-shaped branches
  in the harness.
- **A prompt/policy change cannot ship without passing the behavioral eval gate** (dataset + judge +
  regression thresholds in CI).

## 9. Non-goals

- Unrestricted autonomy or auto-approval of destructive operations.
- Replacing the governed Terraform mutation path with agent-authored infrastructure code.
- Framework maximalism: no technology is added because it is fashionable; every current technology is
  re-justified in `08-Architecture-Decision-Records.md` (KEEP / REFACTOR / ISOLATE / REPLACE / REMOVE).
- Copying any reference architecture wholesale. Hermes, OpenClaw, Pi, and Waku supply *patterns*;
  AegisOps' governance requirements reshape all of them.
- Big-bang rewrite. Migration is incremental, reversible, and gated (07).

## 10. Document map

| Doc | Contract |
|---|---|
| `01-Current-State-Architecture.md` | What exists at `a974290`, traced through real execution paths; defects; technology roles |
| `02-Redesigned-Architecture.md` | Target system: layers, planes, diagrams, review of the user-supplied reference diagram |
| `03-Platform-Features.md` | Objective classes, multi-cloud service coverage matrix, DevOps/SRE capability catalog |
| `04-Agent-Harness-Specification.md` | The loop, model/provider layer, hooks, budgets, permissions, subagents, verification |
| `05-Tool-and-Agent-Contracts.md` | Typed tool registry, tool/agent/policy/verification contracts |
| `06-Memory-Context-and-Execution.md` | Memory tiers + lifecycle, context assembly, durable Task/Run/Step execution |
| `07-Migration-and-Implementation-Plan.md` | Strangler phases, eval gates, risk register, rollback strategy |
| `08-Architecture-Decision-Records.md` | Per-technology and per-structure ADRs; decisions requiring human sign-off |
| `09-Architecture-Readiness-and-Traceability.md` | Readiness gate: requirement traceability, PLAN MODE resolution, parity + security readiness, verdict |
| `10-Behavioral-Acceptance-Matrix.md` | Executable acceptance scenarios (A–W) and intelligence proof tests (IP-1..4) |

Internal consistency rule: where documents disagree, the more specific document wins for detail, but
**this mandate wins on boundaries** — no downstream document may relax §2, §4, or §7.
