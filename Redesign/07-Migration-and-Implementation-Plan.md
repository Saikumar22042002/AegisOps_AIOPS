# 07 — Migration and Implementation Plan

> Strangler-style, eval-gated, reversible. No phase touches the constitution (00 §7). Sizes:
> S ≤ 2 days · M ≤ 1 week · L ≤ 3 weeks (single engineer at the existing test discipline).
> Every numbered item is one PR with tests; nothing merges that widens the mutation surface.

## 0. Rules

1. **Rule zero:** the behavioral eval gate must be green **before any change to the LLM dispatch
   path** — including the internal re-routing in P1.3, not just provider swaps. (Resolves the
   prior suite's sequencing ambiguity.)
2. **Rule one:** governance invariants ship unchanged through every phase; the invariant suites
   (`test_safety_invariants`, `test_tenancy`, exec-loop tests) must stay green *unmodified* —
   editing an invariant test is itself a change-management event.
3. **Rule two:** the kernel enters production via **read paths only**; mutation paths adopt it
   last, behind the engine's compile/approve gate.
4. **Rule three:** every phase has a rollback lever (flag or shim); a phase is not done until its
   rollback is demonstrated in staging.

## Phase 0 — Protect & repair (foundation for everything)

| # | Item | Size |
|---|---|---|
| 0.1 | Eval dataset (`backend/evals/dataset.jsonl`): per-purpose cases scoring AegisOps observables (domain/action/target, template key, guard outcome) incl. negative cases; one shared pure scorer | M |
| 0.2 | LLM-judge suite + CI release gate (exit 1; verdict history; judge semaphore; retry-transient-only) | M |
| 0.3 | `llm_usage` ledger table + writes at existing generation/stream record points, incl. embeddings (D3), `agent_kind`, `prompt_version` (registry lands P2; column nullable until then) | S |
| 0.4 | Defect sweep: D1 (bad_location/bad_region), D2 (dead fallback + discarded models.list), D4 (menu fetches GET /models), D5 (remove `applying` **reads**; full status machine lands P3 — interim machine: running→awaiting_approval→executing→completed/failed/cancelled, no hole), D7 (dead code), F-10 (module-level `select` import → approval-wait metric records at last) | S |
| 0.5 | Governance-flag stamping on approval artifacts + `/healthz` (D9); **align approval posture to the HITL default** (four-eyes optional org policy, code default off — operator-directed correction) | S |
| 0.6 | Security hygiene: authenticate `/metrics` (F-16); Redis-backed rate limiting (F-17); remove secrets/state residue from the working tree + pre-commit guard (F-21); verify realm export secret-free | S |
| 0.7 | Flip `aegisops_event_bus` default memory→redis (memory = unit tests only) | S |
| 0.8 | Worker-role flag: background sweeps (reconciler/retention) run in exactly one role (F-18) | S |

**Exit:** CI blocks a deliberately-broken router prompt; spend queryable in SQL per org/purpose;
approval cards show governance posture; D1–D9 + F-10/16/17/18/21 closed with tests.

## Phase 1 — Provider layer (`app/llm`)

1.1 types/errors/stream events [S] · 1.2 `google_` adapter (thread-offloaded, typed errors,
120s timeout, generation params) [M] · 1.3 `service.py` + byte-compatible `agents/llm.py` shim,
`purpose=` threaded from the 8 call sites; delete dead `generate()` [M — **gate green first, rule
zero**] · 1.4 catalog + `models.yaml` + capability registry (boot validation; `tools_native` vs
`tools_emulated`; every binding needs a validated fallback chain or explicit `fallbacks: none`)
[M] · 1.5 `anthropic_` + `openai_compat` adapters + recorded-fixture contract tests [L] ·
1.6 router + resilient executor (retry/jitter/Retry-After; Redis breaker; two-stage failover with
credential-profile rotation; turn-local fallback, visible hops; planner/judge never silent-
fallback; budget gate) [L] · 1.7 `model_bindings` + Settings UI (live catalogs, health probes,
eval-gated promotion, audit rows) + served-by badges [L] · 1.8 **first native tool calling**:
structured output for router/extract; native FC verified per adapter with an opt-in $0.01 canary
[M] · 1.9 import-linter contract (bans SDK imports outside adapters; bans `langgraph.*` outside
`harness/`) [S]

**Exit:** UI rebind `knowledge → claude-sonnet-5` with zero code change; evals green on two
providers; staged provider-key kill shows visible fallback badges; `grep -r "google.genai|
anthropic|openai" app/agents app/packs` → 0.

## Phase 2 — Harness kernel on read paths

2.1 kernel loop (budget governor incl. wall-clock + grace call; stuck detector; iteration
checkpoints to `run_events`; honest-partial exits) + AgentSpec + ToolRegistry v2 middleware [L] ·
2.2 **INV loop**: kernel drives the frozen read-only investigation registry; first callers
`sre._collect_telemetry` (replacing the hardcoded `list_deployments`) and `cloudops._read_path`
[M] · 2.3 retrieval gate (fails open; deterministic overrides; observable decision) [S] ·
2.4 per-iteration context reassembly via existing recipes [S] · 2.5 `run_events` table + replay-
resume for loops [M] · 2.6 consolidation → human-accepted proposals (`memory_items` table) [M] ·
2.7 subagent spawn (typed results, shared pool, ledgered) [S] · 2.8 prompt registry + hash-
recorded PromptRefs [S] · 2.9 SRE read-tool expansion (pod logs, events, per-service PromQL —
fixes F-15's self-referential signal) [M]

**Exit:** SRE triage chases a symptom across ≥3 tools with a recorded evidence trail and a visible
hypothesis revision; a runaway investigation halts on cost with an honest partial; retrieval
skip-rate dashboarded; kernel resumes a killed investigation on worker 2.

## Phase 3 — Workflow engine (`app/engine`)

3.1 exec_loop → engine package (Step contract; compile closures kept verbatim: catalog, bounded,
wiring, guard; + compensation & lock closures; wave scheduler with disjoint-output compile check)
[L] · 3.2 saga rollback (pre-approved compensation in the artifact; reverse order; compensation-
failure freeze + page) [L] · 3.3 VerifyPlan/EvidenceCard on every step; cross-cloud verify
strategies (kills the AWS-only verify skew) [M] · 3.4 day-2 registry + executor (stop/start/
restart, K8s rollout verbs; blast-radius-tiered approval) [M] · 3.5 K8s executor (pinned
chart/manifest catalog; server-side dry-run diff artifact; rollout verify; `rollout_undo`) [L] ·
3.6 change windows + full status machine (`scheduled`/`verifying`/`rolled_back`/`awaiting_input`;
`applying` fully dead; stale-approval re-validation) [M] · 3.7 deviation taxonomy (precondition
fail, verify fail + proposed fix, retry-exhausted alternative via fixed `suggest_retry`) [M] ·
3.8 real policy predicates replacing `_todo` stubs, per template, parity-gated (F-11); MPP
promoted modules require real predicates before registration [M] · 3.9 `max_steps` 5→8 behind
config + per-run step concurrency cap [S]

**Exit (demo script):** VPC+VM+S3 under one approval showing the full artifact incl. rollback
plan; forced step-3 failure auto-compensates in reverse, truthfully reported; `kill -9` mid-wave
resumes on second worker with no double-apply; "stop the billing VM" runs as a governed day-2
action with evidence; K8s deploy shows dry-run diff at approval and rolls back a failed rollout.

## Phase 4 — The harness-first inversion (the mandate's core move)

4.1 objective model + goal validation in the kernel; admission runs `router` purpose as a
classification call (graph branch retired) [M] · 4.2 **capability packs**: extract cloudops/
devops/sre knowledge+tools into `packs/`; `cloudops.py` (1,531 LOC) dissolves — regex
interceptors become deterministic pre-classifiers feeding the objective model, plan/read/destroy
node code becomes pack tools + kernel behavior [L] · 4.3 main loop (`loop.main` purpose) drives
end-to-end read objectives in production; mutation objectives: loop → `propose_goal_dag` →
engine (LangGraph node graph reduced to interrupt/checkpoint substrate behind `harness/
interrupts.py` + `graph_glue.py`) [L] · 4.4 planner/critic purposes (critic = fast tier,
advisory, pre-compile) [M] · 4.5 permission modes (READ_ONLY/PLAN_ONLY/APPROVAL_REQUIRED/
AUTONOMOUS) as policy matrix + ESTOP sentinel [M] · 4.6 verification-stop nudges + goal
validation wired to evidence cards [S] · 4.7 eval expansion: tool-selection, failure-recovery,
policy-adherence, unnecessary-action dimensions [M]

**Exit:** the EKS+GitHub representative workflow (02 §8) runs end-to-end in staging with the
loop as spine; a failed tool call demonstrably changes the next action in the trace; behavioral
evals green across the inversion (same dataset, both topologies — the inversion ships **dark**
behind a flag until eval parity is proven).

## Phase 5 — Parity, credentials, channels, incident pipeline

5.1 multi-cloud read/verify parity per the 03 §3 matrix (Azure/GCP read packs to AWS's level;
parity gate in CI) [L] · 5.2 DevOps capability completion (Actions log download, failed-job
diagnosis, reruns, PR-first change flow — direct default-branch pushes banned; registry
inspection replaces the CI-poll fiction) [L] · 5.3 **credential brokering**: per-org cloud
credentials, short-lived (AssumeRole / workload identity / service principals), vault-backed;
retires the global key set (F-20) [L] · 5.4 Alertmanager webhook ingress → incident runs → INV
triage → gated remediation (pre-approved tier: org-listed, rate-limited, audited, verified) →
postmortem draft → memory proposals [L] · 5.5 Slack + Teams transports on the Transport Protocol
(click-time re-check identical) [M] · 5.6 live flow console + stalled-step tell + spend/eval
dashboards; chart the 7 unchartered metrics [M] · 5.7 drift expansion beyond aws-ec2, reconciler-
scheduled (F-22) [M] · 5.8 offline model arena (mocked tools) feeding binding promotion [M]

## Decision gates (owner-signed, measured)

| Gate | Trigger to act | Default |
|---|---|---|
| **Temporal** | workflows regularly hours-long, high fan-out, or needing versioned mid-flight migration AND PG-checkpoint redrive shows strain | stay: LangGraph-checkpoint substrate wrapped by harness; Step/Executor contracts keep the swap cheap |
| **Harness-native durability** (drop LangGraph entirely) | after P4 has run ≥1 quarter: if `run_events` replay covers every resume case the checkpointer serves | keep the checkpointer wrapped; revisit with data (ADR-04) |
| **LiteLLM adapter** | a customer demands a vendor not worth an owned adapter | ship disabled |
| **Neo4j** | measured at end of P5: if world-model/impact/drift queries remain ≤2 hops and low-volume → fold into PG recursive CTEs and **remove**; if impact_of-on-every-mutation grows real graph traversals → keep | fold-in is the *expected* outcome (ADR-06) |
| **Per-step approval UX** | enterprise change-board feedback on SINGLE_DAG | SINGLE_DAG + PER_STEP_HIGH available |
| **Plugin sandboxing** | third-party pack demand | no in-process plugin code (ADR-13) |

## Highest-risk migrations (ranked)

1. **P4.3 — loop becomes the spine.** Risk: subtle behavior change across every objective class.
   Mitigation: ships dark behind a flag; eval parity on both topologies; read objectives first;
   mutation entry unchanged (engine + approval); rollback = flag flip.
2. **P3.1/3.2 — exec_loop → engine + saga.** Risk: the best-audited code in the platform is being
   generalized. Mitigation: the keep/generalize/new table is the PR checklist; invariant tests
   unmodified; waves off (sequential) until disjoint-output checks prove out.
3. **P5.3 — credential brokering.** Risk: touches every cloud call. Mitigation: dual-path
   (broker with global-key fallback per org) until every org migrates; broker outage = refuse new
   mutations, never fall back silently.
4. **P1.3 — dispatch re-route.** Risk: silent quality regression. Mitigation: rule zero; byte-
   compatible shim; canary + eval on the same provider before any second provider binds.
5. **P2.5/P3.6 — run_events + status machine.** Risk: reconciler/resume edge cases. Mitigation:
   replay-based resume tested with kill -9 drills; old checkpointer path remains authoritative
   until parity drills pass.

## Risk register (standing)

Provider swap changes behavior subtly → P0 gate + per-binding eval smoke + sticky RoutePlan.
Kernel regression on governed paths → rule two (read-first) + invariant suites untouched.
Adapter drift vs vendor APIs → recorded-fixture contract tests + opt-in boot canary.
Engine scope creep → keep/generalize/new table is the contract; extras are Phase 6+.
Flagship-model cost → purpose tiering; budgets enforced from P0's ledger; visible spend.
Two sources of model truth → yaml = what CAN run; DB = who runs what; boot cross-validation.
Inversion stalls half-done → each phase leaves a *coherent* system (P1–P3 are valuable even if
P4 never ships — they are the Brainstorming blueprint, which stood on its own).

## What is explicitly removed, when

| Item | Phase |
|---|---|
| `integrations/gemini.py` singleton + `integrations/llm/` validate-only seam | P1.8 → shim; deleted end of P1 |
| Hardcoded frontend model menu | P0.4 |
| `"applying"` phantom status | reads P0.4; literal fully dead P3.6 |
| Dead code register (D7) | P0.4 |
| `agents/llm.py` shim | end of P2 |
| `exec_loop.py` (as a module; invariants live on in engine) | P3.1 |
| Fat domain agents (`cloudops.py` et al.) | P4.2 |
| 12-node graph as the outer spine | P4.3 (substrate retained per ADR-04) |
| Global cloud credential set | P5.3 |
| `_todo` blanket policy rows | P3.8 |
