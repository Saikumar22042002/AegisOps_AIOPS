# 09 — Architecture Readiness and Traceability

> The final gate before implementation. This document does not redesign anything: it verifies the
> Redesign/ suite (00–08) is precise, internally consistent, testable, and implementation-ready.
> Scenario IDs (A–W, IP-*) refer to `10-Behavioral-Acceptance-Matrix.md`.

---

## 1. Requirement traceability matrix

Every mandate requirement traced: requirement → architectural decision → document → component →
acceptance test → migration phase.

| # | Requirement (00) | Architectural decision | Doc | Component | Acceptance test | Phase |
|---|---|---|---|---|---|---|
| R1 | Real iterative agent loop | Kernel loop, laws L1–L7; ≤500-line loop module | 04 §3, §11 | `harness/loop.py` | IP-1..3; scenarios F, I | P2 (read), P4 (spine) |
| R2 | Failed tools become observations | Law L3; middleware returns `ToolObservation`, never raises | 04 §3.1; 05 §3 | `tools/middleware.py` | F; IP-1 asserts observation row precedes changed action | P2 |
| R3 | Re-planning on failure | Law L4 (re-reason over observations) + deviation proposals for approved plans | 04 §3.1; 07 P3.7 | kernel + `engine/steps.py` | I, J, T | P2/P3 |
| R4 | Native tool calling | ADR-15; FC is the spine; emulation only read-effect | 08 ADR-15; 04 §4.5 | `llm/adapters/*` | A (tool_use blocks in trace); P1.8 canary | P1 |
| R5 | Multi-provider LLM (Gemini/OpenAI/Anthropic/Azure/Bedrock/OpenRouter/Ollama/compat/future) | 6 wire families × provider data rows; ADR-14 | 04 §4.1 | `llm/adapters/`, `models.yaml` | P1 exit: UI rebind zero-code; evals green on 2 providers | P1 |
| R6 | Purpose-based routing by task characteristics | purposes × Needs × capability facts; RoutePlan pinned per run | 04 §4.3–4.4 | `llm/router.py` | per-purpose eval dataset; V | P1 |
| R7 | Cost ledger | `llm_usage` append-only, tokens=truth, `agent_kind`, `prompt_version` | 06 §8.2 | `llm/usage.py` | R; SQL spend-by-org/purpose query in CI smoke | P0 |
| R8 | Hard budgets (token/cost/iteration/runtime/tool/mutation) | Budget governor inside the loop; grace call; halt-at-safe-boundary | 04 §5 | `harness/budgets.py` | R | P2 |
| R9 | Permission modes | READ_ONLY / PLAN_ONLY / APPROVAL_REQUIRED / AUTONOMOUS (§2 below) | 03 §6.1; 04 §8.1 | `harness/policy.py` | G; PLAN_ONLY variant of A | P2 (pipeline), P4.5 (modes) |
| R10 | Approvals (durable, HITL — initiator may approve; optional four-eyes policy; deviation) | kept constitution + artifact + args-hash binding | 04 §8.4 | engine approval service | S, T | exists; artifact P3 |
| R11 | Lifecycle hooks (12 named) | one ordered chain, fail-closed, approval hooks observers-only | 04 §6 | `harness/hooks.py` | hook-failure blocks action (unit); P compaction hooks | P2 |
| R12 | Subagents, structured results | contract: isolated ctx, typed size-capped results, untrusted, depth 1, shared budgets | 04 §10; 05 §6 | `harness/subagents.py` | U, N | P2.7 |
| R13 | Background execution | worker role; supervisor/reconciler; durable parks | 06 §8.4 | supervisor, reconciler | O | P0.8, P2 |
| R14 | Compaction | context engine; pairs unsplit; tail floor; flush-first | 06 §7 | `harness/context.py` | P | P2 |
| R15 | Durable runs | event-sourced `run_events`; ADR-16 | 06 §8.2 | db + kernel | O | P2.5 |
| R16 | Resume after restart | replay + checkpointer parity drills | 06 §8.4; 08 ADR-04 | reconciler | O (kill -9 drill) | P2.5/P3 |
| R17 | Cross-session continuation | Task entity spans runs; memory tiers | 06 §1, §8.1 | task manager, `memory_items` | Q | P2.6 |
| R18 | Memory tiers (working/task/episodic/semantic/procedural) | 5 tiers w/ scope/provenance/confidence/importance/lifecycle/retrieval/promotion/expiration | 06 §1 | `memory/` | Q; tier-attribute schema test | P2.6 |
| R19 | Memory governance (no blind persistence) | gate; consolidation→human-accepted proposals; write-path boundary; supersede-not-coexist | 06 §2, §4 | `memory/gate.py`, `consolidation.py` | Q variant: agent cannot write `memory_items` directly (negative test) | P2.3/P2.6 |
| R20 | Verification ≠ tool success | 4 levels; EvidenceCard; goal validation; verification-stop nudges | 04 §7; 05 §8 | `harness/verification.py`, engine | M; every scenario's verification column | P3.3 |
| R21 | Behavioral evaluation plane | dataset+judge+regression gate; 9 dimensions; ADR-18 | 04 §9 | `backend/evals/` | P0 exit: deliberately-broken prompt blocked by CI | P0 |
| R22 | AWS/Azure/GCP parity | parity gate per service family; cloud-neutral harness | 03 §3; §5 below | `packs/cloudops/*` | A–D × 3 clouds; CI parity manifest check | P5.1 |
| R23 | GitHub DevOps first-class | capability catalog (logs, reruns, PR-first, registries) | 03 §4 | `packs/devops/github` | L | P5.2 |
| R24 | SREOps (evidence-driven diagnosis/remediation) | INV loop + telemetry read pack + gated remediation | 03 §5 | `packs/sreops/k8s` | M, N | P2.2/2.9, P5.4 |
| R25 | Kubernetes | K8s executor: pinned catalog, dry-run diff, rollout verify, `rollout_undo` | 07 P3.5 | `engine/executors/k8s.py` | K | P3.5 |
| R26 | Terraform preserved as mutation engine | ADR-07; runner wrapped unchanged | 08 ADR-07 | `engine/executors/terraform.py` | J; invariant suites unmodified | exists; P3 wrap |
| R27 | Credential isolation per tenant | ADR-17 broker, short-lived, dual-path migration | 08 ADR-17; 07 P5.3 | `security/broker` | W | P5.3 |
| R28 | Framework neutrality | every tech re-justified; import-linter contract | 08; 07 P1.9 | CI lint contracts | contract tests (grep-zero assertions) | P1.9 |
| R29 | LangGraph evaluated per-usage (not kept/replaced by default) | ADR-04: reduce+isolate now, measured exit gate | 08 ADR-04 | `harness/interrupts.py`, `graph_glue.py` | O (interrupt resume); parity drills | P1.9→P4 + gate |
| R30 | Objective-driven design (not CRUD functions) | O1–O10 objective taxonomy; success_criteria drive goal validation | 03 §1–2 | objective model (P4.1) | N (objective without named cloud); M (goal validation) | P4.1 |
| R31 | Ask only genuinely missing info | L7 ask-as-action after discovery fails | 04 §3.1 | kernel | E | P2 |
| R32 | Honest partial reporting | constitution; grace-call partials | 00 §7; 04 §5 | kernel + engine | H, R | exists; kept |
| R33 | Model routing never forces strongest model | tiered purposes; cheap gate/consolidation | 04 §4.3 | router | ledger shows fast-tier majority in P2 exit metrics | P1 |
| R34 | Tool limits (timeout/retry/cancellation/audit per tool) | ToolDef fields + middleware | 05 §1, §3 | registry | F (timeout observation); audit-row-per-call test | P2 |

Coverage check: every requirement section of 00 (§2 boundary classes, §3 harness-first, §4 loop,
§5 multi-cloud, §6 objectives, §7 constitution, §8 acceptance) maps to ≥1 row above. No orphan
requirements found; no orphan architecture components (each component column appears in ≥1 phase
deliverable in 07).

## 2. PLAN MODE — resolved unambiguously

**Decision: the mode set is `READ_ONLY | PLAN_ONLY | APPROVAL_REQUIRED | AUTONOMOUS`.**
`ASSISTED` is **removed as a mode** — it conflated execution *ceiling* with approval
*granularity*. Granularity is already the orthogonal `ApprovalPolicy` axis
(`PER_STEP_HIGH | SINGLE_DAG | PRE_APPROVED`); the old "ASSISTED" is exactly
`APPROVAL_REQUIRED × per-step granularity`. Two clean axes instead of four tangled modes.
(03 §6.1 and 04 §8.1 are updated accordingly.)

**PLAN_ONLY semantics (normative):**

- The run uses **the same iterative loop** — inspect, reason, iterate on read tools, delegate,
  ask — with the policy ceiling clamped so that no proposal can proceed past artifact creation.
- The loop produces the **complete approval artifact**: compiled plan (all engine closures run),
  cost estimate, blast radius (`impact_of`), verification plan, rollback/compensation plan,
  governance-flag stamp. Everything an approver would see — with **no approval requested**,
  because there is nothing pending execution.
- Terminal state: `completed` with `outcome.kind = "plan_ready"`; the compiled Plan and its hash
  are retained on the run.
- **Executing a ready plan later is a new run** referencing the `PlanRef`: re-compile,
  re-validate preconditions against current world state (drift ⇒ deviation), then the full
  approval flow under an execute-capable mode. A stored plan is never a blank check — same rule
  as stale approvals (06 §8.4).
- **PLAN_ONLY vs READ_ONLY:** READ_ONLY answers questions and may sketch advisory
  recommendations but never invokes the engine's compile path — no Plan artifact exists.
  PLAN_ONLY produces the real, executable, hash-bound plan artifact.
- **PLAN MODE vs the normal loop:** structurally identical — same loop, same laws L1–L7, same
  tools. Only the policy verdict table (mutation-bound proposals → `plan_ready` instead of
  `approval_required`) and the terminal state differ. There is no separate "plan pipeline."

| Mode | Read tools | Compile plan artifact | Request approval | Execute mutation |
|---|---|---|---|---|
| READ_ONLY | ✅ | ❌ (advisory prose only) | ❌ | ❌ |
| PLAN_ONLY | ✅ | ✅ full artifact, `plan_ready` | ❌ | ❌ |
| APPROVAL_REQUIRED | ✅ | ✅ | ✅ (granularity via ApprovalPolicy) | ✅ post-approval |
| AUTONOMOUS | ✅ | ✅ | only ≥ risk ceiling / non-pre-approved | ✅ within budgets + verb list; destructive always gated |

## 3. Consistency verification (suite-internal)

Checked in this gate: all 13 prior-suite inconsistencies (I1–I13) have named resolutions in
04/05/06/07 ✓ · doc map in 00 §10 matches files on disk (00–10 + README + diagrams/) ✓ · every
cross-reference audited after §4.7/§8.5 renumbering in 04 (two fixed) and 05→"06 §9" (fixed to
07 P3.1) ✓ · mode tables in 03/04/07/README updated to the §2 model in this gate ✓ · status
machine (06 §8.3) needs **no new state** for PLAN_ONLY (uses `completed` + outcome kind) ✓ ·
one loop-budget nuance made explicit: `Budgets.max_tool_calls=24` is the general loop default;
INV mode (frozen investigation registry) keeps the registry's own `MAX_CALLS=8` — the stricter
bound wins when both apply ✓.

## 4. Intelligence proof requirement

Defined as executable tests in 10 §2 (IP-1, IP-2, IP-3). The distinguishing assertion — a tool
failure must produce a *different subsequent action under a changed hypothesis*, and the test
**fails if the agent retries the same action** — is expressed over `run_events` rows, not over
model prose, so it is mechanically checkable and prompt-independent.

## 5. Multi-cloud parity matrix (service × verb × cloud × verification × phase)

Verbs per 03 §3 (D I C U L S X T V). Verification strategy = the evidence source for the V verb.
Phase = when the family reaches full-verb parity across all three clouds.

| Service family | AWS / Azure / GCP | Verbs | Verification strategy (evidence) | Parity phase |
|---|---|---|---|---|
| Compute | EC2 / VM / GCE | D I C U L S X T V | describe/state read + status checks + (if app) HTTP probe | P5.1 (writes exist; reads AWS-only today) |
| Object storage | S3 / Blob / GCS | D I C U X T V | bucket/container exists + policy/ACL read-back + name-availability precheck | P5.1 |
| Managed DB | RDS / Azure SQL / Cloud SQL | D I C U L S X T V | instance state + endpoint reachability probe from context | P5.1 |
| Network | VPC / VNet / VPC | D I C U X T V | CIDR/subnet/route read-back + reachability analysis | P5.1 |
| Kubernetes | EKS / AKS / GKE | D I C U S X T V | cluster state + nodepool ready + API reachability + rollout status | P5.1 (K8s executor P3.5) |
| Serverless containers | ECS / Container Apps / Cloud Run | D I C U L S X T V | service state + revision ready + endpoint probe | P5.1 (new templates) |
| Functions | Lambda / Functions / Cloud Functions | D I C U X T V (S ◐) | function state + test-invoke probe (dry) | P5.1 (new) |
| Identity | IAM / Entra / IAM | D I T V (C U X ◐ destructive-gated) | policy/binding read-back diff | P5.1 reads; ◐ writes post-P5 sign-off |
| Telemetry | CloudWatch / Monitor / Cloud Monitoring | D I T V (C U ◐ alarms) | query round-trip + alarm state read | P5.1 |
| Load balancing | ELB·ALB·NLB / LB·AppGW / CLB | D I C U X T V | target/backend health read + listener probe | P5.1 |

**No-AWS-assumptions verification:** (a) CI grep contract — `boto3|azure\.|google\.cloud` imports
permitted only under `packs/cloudops/{aws,azure,gcp}/` and `llm/adapters/` (bedrock), asserted by
the P1.9 import-linter; (b) the harness/kernel/engine modules contain zero cloud-name literals
(lint rule); (c) day-2 verb registry and verify strategies are pack-registered, never
harness-hardcoded; (d) the only current AWS-shaped residue — verify paths and drift readers —
is tracked as F-12/F-22 with parity scheduled P5.1/P5.7. Scenario N exercises a cloud-unnamed
objective end-to-end.

## 6. Security readiness table

Severity: **BLOCKER** = must be resolved before the phase named; HIGH = must have an owner +
date before P1 exits; MEDIUM = tracked, phase-scheduled; LOW = hygiene.

| Item | Finding (01) | Severity | Disposition |
|---|---|---|---|
| **GCP service-account key on disk** (`infra/secrets/gcp-sa.json`) | F-21 | **BLOCKER (P0)** | **Treat as exposed: rotate/revoke the SA key in GCP IAM**, then remove the file; verify no other copies; add pre-commit + CI secret scan. Deletion alone is insufficient. |
| **Live Terraform state + plan files in working tree** (`aws-ec2/.stale_aside/`, `terraform.tfstate.d/`, `*.tfplan`) | F-21 | **BLOCKER (P0)** | State files can embed connection strings/outputs: audit contents for secrets → rotate anything found → relocate state to the remote backend → purge from tree; plan files deleted (they are replayable mutation payloads). |
| **Postgres dump in tree** (`aegisops-before-wipe-*.dump`) | F-21 | HIGH | Contains tenant data + possibly credential hashes: move to controlled storage, purge locally; rotate any secrets it contains. |
| **Four-eyes disabled in shipped `.env`** | D9/F-9 | ~~BLOCKER~~ **RESOLVED — superseded** | Operator directive: HITL is the default approval model (initiator may approve); four-eyes is optional org policy, code default off. The surviving requirement is visibility: governance-flag stamping (P0.5) shows the active posture on every approval card and `/healthz`. |
| **Global long-lived cloud credentials for all tenants** | F-20 | HIGH (BLOCKER for onboarding a second production tenant) | ADR-17 brokering at P5.3; until then: single-tenant posture documented, keys rotated on schedule, `AWS_SESSION_TOKEN` path preferred where possible. |
| Approval drift | — | covered | Args-hash binding + deviation re-approval + stale-approval re-validation (04 §8.4, 06 §8.4); tested by scenario T. |
| Subagent permission inheritance | — | covered | Child = parent's policy **minus** blocked verbs (delegate/ask/memory-write/send/schedule); read/propose only; untrusted results; tested by U. |
| Tool allowlists | — | covered | Layered pipeline + frozen-per-run registry + turn-scoped narrowing (04 §8.3, 05 §4). |
| Hardline deny | — | covered | Unappealable class (04 §8.2); negative tests in evals (policy-adherence dimension). |
| ESTOP | — | covered | Sentinel pauses new runs/mutations, never in-flight applies (04 §8.6); drill in P4.5 acceptance. |
| MCP trust | — | covered | Read-effect only, org allowlist, mutation-name refusal, ingest sanitization (05 §4). |
| Redaction | — | covered (extend) | 7-pattern redaction exists at persist/console/Langfuse; extend to `run_events` writes (06 §8.2 rule) + fail-closed export posture (Hermes lesson). |
| Audit | — | covered | Middleware audit stage writes per-call rows (closes 2-call-site gap); approvals/bindings/identity already immutable rows. |
| Unauthenticated `/metrics`; per-process rate limit | F-16/F-17 | MEDIUM | P0.6. |
| Keycloak realm export tracked in git | F-21 | LOW | Verify secret-free in P0.6; move client secrets to env refs if present. |

## 7. Framework decisions — re-confirmation

No new evidence has emerged since the ADRs were written against the same HEAD; all verdicts
stand. Compact re-confirmation (full reasoning: 08):

| Tech | Current role | Actual value | Problems | Target responsibility | Verdict | Migration impact |
|---|---|---|---|---|---|---|
| LangGraph | checkpoint/interrupt substrate wearing a 12-node spine | high (durability triad) / negative (spine blocks iteration) | agents import it; topology prevents loop | durable interrupt+checkpoint only, inside `harness/` | **REDUCE+ISOLATE; gated exit** (ADR-04) | P1.9 lint; P4.3 inversion; parity drills |
| LangChain | 1 import | none | none | none | **REMOVE** | 2-line diff |
| Neo4j | write-only context graph + narrow world model | low / narrow-real | ops burden vs 1–2 hop queries | world-model behind interface | **ISOLATE; fold-in expected @P5 gate** | interface S; removal deletes a service |
| PostgreSQL | system of record | high | additive gaps only | + run_events/ledger/memory/prompts/bindings | **KEEP** | additive DDL |
| pgvector | message/chunk embeddings | real | 768-pin; uncounted embeds | + memory_items vectors | **KEEP** | none; refuse hot swap |
| Redis | 9 coordination uses | high | rate-limit gap; default flip | + rate limits, breaker, wave locks | **KEEP** (never a record) | P0.6/0.7 |
| Terraform | all mutation | the moat | `_todo` stubs; MPP HCL hatch; day-2 misfit | C/U/X via executor; day-2 registry beside it | **KEEP (hardened)** | P3 wrap; P3.8 predicates |
| Langfuse | traces + (misplaced) cost | high / misuse | cost volatility; SDK v2 pin | traces only; ledger owns cost | **KEEP (scoped)** | P0.3 |
| Prometheus | 11 metrics, 1 dead | high | F-10; 4-of-11 charted | + gate/fallback/budget/eval metrics | **KEEP** | P0.4 fix |
| Grafana | 1 dashboard | low today | under-used | spend/flow/eval/posture dashboards | **KEEP (earn-it gate @P5)** | P5.6 |
| FastAPI | control plane | high | none | + worker-role split | **KEEP** | P0.8 |

## 8. Readiness verdict

### **READY FOR IMPLEMENTATION**

Conditioned on the P0 gate below — the three BLOCKER security items are *inside* P0, not
prerequisites to starting it. No architectural blockers remain: requirements trace (§1), modes
are unambiguous (§2), acceptance behavior is executable (doc 10), parity is scheduled and
CI-enforceable (§5), and framework verdicts are re-confirmed (§7).

### Implementation sequence, dependencies, acceptance gates

| Phase | Depends on | Scope (07 detail) | Acceptance gate (exit) |
|---|---|---|---|
| **P0 — Protect & repair** | — | eval gate; ledger; defect sweep (D1–D9, F-10); governance stamping + **HITL posture alignment** (four-eyes optional, default off); security preflight (sandbox artifacts classified DEV/SANDBOX per operator — scanning controls, no rotation); bus default; worker role | CI blocks a deliberately-broken router prompt; spend queryable; scanner green with sandbox allowlist + fails on unclassified secrets; scenarios G/H pass on current topology |
| **P1 — Provider layer** | P0 (rule zero: gate green before P1.3) | types/adapters/catalog/router/executor/bindings UI/first native FC/import-linter | UI rebind zero-code; evals green on 2 providers; key-kill drill shows visible fallback; grep-zero contract; scenario V |
| **P2 — Kernel on read paths** | P1 | loop+budgets+registry v2; INV loop; retrieval gate; run_events; consolidation proposals; subagents; prompt registry; SRE reads | IP-1..3 pass; scenarios E, F, N, P, Q, R, U; kill -9 loop-resume drill (O read-path variant) |
| **P3 — Workflow engine** | P2 (run_events), P1 | engine extract; saga; EvidenceCards cross-cloud; day-2; K8s executor; windows+status machine; real predicates | demo script (07); scenarios B/C/D, J, K, S, T, O (mutation variant), H |
| **P4 — Harness-first inversion** | P3 + **sign-off #1** | objective model; packs; loop-as-spine (dark); planner/critic; modes+ESTOP; verification-stop | eval parity both topologies; EKS+GitHub workflow (02 §8) in staging; scenarios A, I, M end-to-end on the loop; ESTOP drill |
| **P5 — Parity, creds, channels, incidents** | P4 (packs) + **sign-off #4** for 5.3 | 3-cloud parity; DevOps completion; credential broker; alert pipeline; Slack/Teams; dashboards; drift | parity manifests green in CI (§5 matrix); scenarios A–D × Azure/GCP, L, W; broker dual-path drill |

Standing gates unchanged (07): Temporal (default no) · LangGraph end-state (post-P4 + 1 quarter)
· Neo4j fold-in (P5) · per-step approval UX · plugin sandboxing.

**Do-not-start list** (explicitly out of scope until their phase + sign-off): production code
changes of any kind (this gate authorizes none), AUTONOMOUS mode enablement, pre-approved
remediation verbs, IAM write verbs, second-tenant onboarding before P5.3.
