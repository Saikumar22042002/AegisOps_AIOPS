# Implementation Roadmap

> Sequencing for the three structural moves, built strangler-style around a running
> system. Rule zero, inherited from the gap analysis and kept absolute: **the eval
> gate lands before any provider swap** — swapping brains without a behavioral gate is
> how a silent quality regression ships. Rule one: no phase touches the governance
> invariants (Terraform-only mutation, durable approval interrupt, plan_guard, strict
> tenancy, four-eyes, idempotency, honest partials).

Phases are dependency-ordered; within a phase, items are parallelizable. Sizes:
S ≤ 2 days, M ≤ 1 week, L ≤ 3 weeks (single engineer, existing test discipline).

---

## Phase 0 — Protect & repair (foundation for everything)

| # | Item | Size | Notes |
|---|---|---|---|
| 0.1 | Behavioral eval dataset (`backend/evals/dataset.jsonl`): per-purpose cases scoring AegisOps observables — expected `domain/action/target`, template key, guard outcome; include negative cases ("how many S3 buckets?" must never plan) | M | waku's shape, our observables; one shared pure scorer so CI and UI can never drift |
| 0.2 | LLM-judge suite + CI release gate (exit 1 on deterministic fail or judge below threshold; verdict history file) | M | carry waku's two judge details: concurrency semaphore, retry-only-transient |
| 0.3 | `llm_usage` ledger table + writes from the existing generation/stream record points; include embeddings (audit D3) and `agent_kind` | S | tokens ground truth, dollars derived at read |
| 0.4 | Defect sweep from the audit: D1 `bad_location`/`bad_region` mismatch (+ a cross-boundary test), D2 dead model fallback + discarded `models.list()`, D4 menu fetches `GET /models`, D5 remove vestigial `"applying"`, D7 dead code | S | independent of redesign; do first, they're landmines |
| 0.5 | Governance-flag stamping: four-eyes/tenancy/exec-loop/event-bus posture rendered on every approval artifact + `/healthz` (audit D9 made this deployment run four-eyes-off silently) | S | honesty rule |
| 0.6 | Flip `aegisops_event_bus` code default `memory → redis`; memory mode stays for unit tests only | S | compose already runs redis on both workers |

**Exit criteria:** CI blocks on a router-prompt quality regression (prove it with a
deliberately broken prompt PR); spend per org/purpose queryable in SQL; approval cards
show governance posture; all D-defects closed with tests.

## Phase 1 — Provider Layer (`app/llm/`)

| # | Item | Size | Notes |
|---|---|---|---|
| 1.1 | `types.py`, `errors.py`, canonical streaming events | S | zero-dependency module first |
| 1.2 | `adapters/google_.py` wrapping today's client: thread-offloaded, typed errors, **per-call timeout (120s default)**, generation params plumbed | M | fixes "no timeout / no params anywhere" |
| 1.3 | `service.py` + rewrite `agents/llm.py` as a byte-compatible shim threading `purpose=` from each of the 8 call sites; delete dead `generate()` | M | agents unchanged in behavior; evals prove it |
| 1.4 | `catalog.py` + `models.yaml` + capability registry with boot-time validation | M | needs ⊆ capabilities enforced at config load |
| 1.5 | `adapters/anthropic_.py` + `adapters/openai_compat.py` + contract-test fixtures (golden request/response, streaming assembly, quirk behaviors) | L | waku's bridge is the reference implementation |
| 1.6 | `router.py` (purpose bindings, resolution order, fallback chains) + `executor.py` (retry/jitter/Retry-After, Redis circuit breaker, visible fallback hops, planner/judge no-silent-fallback rule, budget gate) | L | |
| 1.7 | `model_bindings` table + Settings/Models admin UI (live catalogs via `list_models()`, health probes, eval-gated promotion, audit rows) + `served_by` badges in chat | L | the "switch LLMs from the UI" deliverable |
| 1.8 | Native structured output for `router`/`cloudops.extract` (replace prompt-and-parse), native tool-calling plumbing verified per adapter with a live canary | M | first real use of `tools=` in the codebase |
| 1.9 | Import-linter/ruff contract: agents may not import SDKs/adapters/langgraph | S | makes G1 permanent |

**Exit criteria:** bind `knowledge → claude-sonnet-5` in the UI with zero code change;
evals green on Gemini and Claude; kill one provider's key in staging → visible
fallback badges, no silent degradation; `grep -r "google.genai\|anthropic\|openai"
backend/app/agents/` returns nothing.

## Phase 2 — Agent Kernel (`app/harness/`)

| # | Item | Size | Notes |
|---|---|---|---|
| 2.1 | Kernel loop (budgets incl. cost, stuck detector, iteration checkpoints, honest-partial exits) + `AgentSpec`/`ToolRegistry v2` with middleware chain | L | pi-small core; policies around it |
| 2.2 | **INV loop**: LLM director over the frozen read-only registry; first callers `sre._collect_telemetry` (replacing the one hardcoded `list_deployments`) and `cloudops._read_path` | M | the single biggest capability gap closed inside an already-safe boundary |
| 2.3 | Retrieval gate (cheap-model, fails open, deterministic always-retrieve overrides for positional recall + pending params; decision emitted as event) | S | wraps one call site in `memory.py` |
| 2.4 | Per-iteration context reassembly via existing `build_context` recipes | S | cadence fix, not technique fix |
| 2.5 | Consolidation pass → **human-accepted proposals** into `user_memory` (never auto-write) | M | Notification-based accept flow exists to copy |
| 2.6 | Subagent spawn with shared budget pool + ledger `agent_kind` | S | investigation.spawn generalized |
| 2.7 | Prompt registry (versioned, hashed) wired into ledger + eval verdicts | S | closes "which prompt caused this regression?" |

**Exit criteria:** SRE triage chases a symptom across ≥3 tools and revises its
hypothesis (recorded Evidence trail); a runaway investigation stops on cost budget
with an honest partial; retrieval skip-rate visible on dashboards; consolidation
proposals appear and require human accept.

## Phase 3 — Workflow Engine (`app/engine/`)

| # | Item | Size | Notes |
|---|---|---|---|
| 3.1 | Extract `exec_loop` → engine package: Step contract, compile phase (catalog/wiring/guard closure kept verbatim), wave scheduler + disjoint-output check, lock plan | L | invariants table in CloudOps_Harness §12 is the checklist |
| 3.2 | Compensation closure + saga rollback (pre-approved rollback in the artifact; reverse-order; compensation-failure freeze) | L | the "undo my last apply" affordance falls out of this |
| 3.3 | VerifyPlan/EvidenceCard on every step (extend today's AWS-only verify cross-cloud) | M | evidence over claims |
| 3.4 | Day-2 verb registry + executor (stop/start/restart + K8s rollout verbs), approval-tiered by blast radius | M | the honest Terraform carve-out |
| 3.5 | K8s executor: pinned chart/manifest catalog, server-side dry-run diff artifact, rollout verify, `rollout_undo` compensation | L | |
| 3.6 | Change windows + `scheduled` status + stale-approval re-validation; run status machine cleanup | M | replaces vestigial `applying` |
| 3.7 | Deviation taxonomy expansion (precondition fail, verify fail w/ proposed fix, region alternates via fixed `suggest_retry`) | M | |
| 3.8 | Raise `max_steps` 5→8 behind config once waves land; per-run step concurrency cap | S | |

**Exit criteria (the demo script):** VPC+VM+S3 with one approval showing the full
artifact incl. rollback plan; forced failure at step 3 → automatic reverse
compensation, truthfully reported; `kill -9` mid-wave → resume on second worker, no
double-apply; `stop the billing VM` runs as a governed day-2 action with verify;
K8s deploy shows a dry-run diff at approval and rolls back on failed rollout.

## Phase 4 — Multi-agent, channels, incident pipeline

| # | Item | Size |
|---|---|---|
| 4.1 | Planner agent (GoalDAG drafts via GOVERNED_PROPOSE tools) + critic advisory pass attached to artifacts | M |
| 4.2 | Alertmanager webhook ingress → incident runs → INV triage → gated remediation → bake-time re-check → postmortem draft artifact | L |
| 4.3 | Slack (then Teams) transports on the existing Protocol; approval deep-link + click-time re-check identical to Telegram | M |
| 4.4 | Live flow console (run_steps-driven diagram, stalled-step tell) + ledger/spend dashboards | M |
| 4.5 | Pre-approved auto-remediation tier (org-listed low-blast actions, rate-limited, audited) | M |
| 4.6 | Offline model arena against the eval dataset (mocked tool layer) feeding binding promotion | M |

---

## Decision gates (owner-signed, unchanged in spirit from the target-architecture doc)

| Gate | Trigger to act | Default |
|---|---|---|
| **Temporal** | workflows regularly hours-long, high fan-out, or needing versioned mid-flight migration; PG-checkpoint redrive shows strain | stay on LangGraph+PG; engine's Executor/Step contract keeps the swap cheap |
| **LiteLLM adapter** | a customer demands a vendor not worth an owned adapter | ship disabled; owned adapters cover hot path |
| **Neo4j fold-in** | after Phase 3+4, if world-model queries stay 1–2 hops | keep only if `impact_of`/drift earn it |
| **Per-step approval UX** | enterprise change-board feedback on SINGLE_DAG | SINGLE_DAG default, PER_STEP_HIGH available |

## Risk register

| Risk | Mitigation |
|---|---|
| Provider swap changes behavior subtly | Phase 0 gate + per-purpose eval smoke on every binding change; sticky RoutePlan per run |
| Kernel regression on the governed path | kernel enters via read paths only (Phase 2); planner adoption waits for Phase 4; invariant tests (`test_safety_invariants`, `test_tenancy`, exec-loop suite) must stay green untouched |
| Adapter drift vs vendor APIs | contract tests on recorded fixtures + boot-time live canary (opt-in) |
| Scope creep in the engine | CloudOps_Harness §12 keep/generalize/new table is the contract; anything not in it is Phase 5+ |
| Cost of flagship models | purpose tiering defaults (fast for router/extract/gate), budgets enforced, ledger visible from Phase 0 |
| Two sources of model truth (yaml vs DB) | yaml = catalog of what CAN run; DB = who runs WHAT; boot-time cross-validation; admin UI is the only DB writer |

## Suggested branch/PR shape

One PR per numbered item, each landing with its tests + eval-gate green; `app/llm`,
`app/harness`, `app/engine` grow behind the import-linter contract from day one.
Nothing merges that widens the mutation surface — reviewers can hold every PR to the
invariant list at the top of this file.
