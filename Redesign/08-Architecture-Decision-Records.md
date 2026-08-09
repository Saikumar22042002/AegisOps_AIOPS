# 08 — Architecture Decision Records

> Every major technology and structure, evaluated as: CURRENT ROLE → ACTUAL VALUE → PROBLEMS →
> TARGET RESPONSIBILITY → VERDICT (KEEP / REFACTOR / ISOLATE / REPLACE / REMOVE) → REASON →
> MIGRATION IMPACT. Verdicts are grounded in the `a974290` audit (01) and the reference studies
> (04 §1). Decisions marked **⚠ HUMAN SIGN-OFF** must be explicitly approved before their phase
> begins.

---

## ADR-01 · PostgreSQL — **KEEP (expanded)**

- **Current role:** system of record (runs, run_steps, messages, approvals, audit_log,
  user_memory, resources, notifications) + LangGraph checkpoints + pgvector host.
- **Actual value:** high — org-scoped, tested (599-LOC tenancy suite), proven cross-worker resume.
- **Problems:** none structural; `runs.ended_at` never written; no ledger/memory/run-log tables.
- **Target responsibility:** everything durable — plus `run_events`, `llm_usage` (partitioned),
  `memory_items`, `prompt_registry`, `model_bindings`.
- **Reason:** it already is the system of record; every alternative adds a store without adding a
  capability.
- **Migration impact:** additive DDL only (P0/P2/P3); zero risk to existing tables.

## ADR-02 · pgvector — **KEEP**

- **Current role:** `messages.embedding` (768-d), document chunks; cosine retrieval + trgm fallback.
- **Actual value:** real and used every turn (currently *over*-used — no gate).
- **Problems:** dimension pinned at 768 → embedding model rebinding is a re-embedding migration;
  embedding calls invisible in telemetry (D3).
- **Target responsibility:** vector store for episodic/semantic `memory_items` + existing uses.
- **Reason:** no scale or capability argument for a dedicated vector DB; the store is not the gap,
  the tiers above it are.
- **Migration impact:** none beyond new tables; admin UI must refuse hot-swapping the embedding
  binding (catalog warning encoded).

## ADR-03 · Redis — **KEEP (availability-critical, never a record)**

- **Current role:** 9 uses — event bus (Streams), heartbeat, cancel, idempotency, approval lock,
  auth sessions/PKCE, param cache, reveal claim, drift dedupe.
- **Actual value:** high; the Streams bus + cursor semantics are what make horizontal scale work.
- **Problems:** rate limiting *isn't* Redis-backed (F-17); code default `memory` diverges from the
  shipped posture; breaker state will need shared storage.
- **Target responsibility:** all of today's + rate limits + circuit-breaker state + lock plan for
  engine waves. Integrity rule: Redis down ⇒ refuse new runs; never a degraded-write mode.
- **Migration impact:** P0.6/P0.7 (rate limit, default flip); additive elsewhere.

## ADR-04 · LangGraph — **REDUCE + ISOLATE now; decision-gated exit** · ⚠ HUMAN SIGN-OFF

The mandate's central framework question, answered per-usage:

| Usage (exact files) | Responsibility today | Category | Appropriate for LangGraph? | Target |
|---|---|---|---|---|
| `StateGraph`/`START`/`END`/`compile` — `agents/graph.py:15,83-110` | 12-node routing topology (the outer spine) | legacy orchestration | **No** — a fixed single-pass DAG is what prevents iteration | **REMOVE as spine** (P4.3): routing becomes admission classify + loop; node code dissolves into packs/kernel |
| `interrupt` — `approval.py:13,59`; `exec_loop.py:24,301` | durable human-approval pause, resumable cross-process | durable task execution | **Yes** — this is the one thing a while-loop cannot replace unaided | **KEEP, ISOLATED** behind `harness/interrupts.py` (P1.9 import ban; P4.3 wraps) |
| `AsyncPostgresSaver` — `checkpointer.py:10,35-36` | durable state snapshots (thread==run) | durable task execution | **Yes** (proven: 2-worker approval continuation) | **KEEP, ISOLATED**; run in parallel with `run_events` replay-resume until parity drills pass |
| `Command(resume=…)` + `aget_state().next` — `runner.py:15,70,72` | resume + interrupt detection; reconciler redrive (`reconciler.py:146-201`) | durable task execution | Yes | KEEP, ISOLATED (same seam) |
| `add_messages` reducer — `state.py:7` | state merge convenience | workflow/state plumbing | Neutral | dies with the spine |

- **Actual value:** concentrated entirely in the durable interrupt/checkpoint/redrive triad. The
  *graph* part delivers negative value (it is the reason nothing iterates). Notably, the codebase
  uses no Send, no subgraphs, no streams, no store — LangGraph is already used as a checkpoint
  substrate, not an agent framework.
- **Problems:** the spine blocks the mandate's loop; agents import langgraph directly; the
  checkpointer format couples resume semantics to the library.
- **Target responsibility:** durable interrupt + checkpoint substrate **only**, invisible outside
  `harness/`.
- **Verdict:** REFACTOR/ISOLATE in P1–P3; REMOVE-as-spine in P4; full removal is a **measured
  decision gate** (07): after P4 runs a quarter, if `run_events` replay covers every resume case
  the checkpointer serves, drop it; if not, it stays wrapped — either outcome is cheap because
  nothing outside `harness/` knows it exists.
- **Reason:** neither "keep because it exists" nor "replace because harness-first" — keep exactly
  the responsibility it demonstrably serves (durable parks), delete the responsibility that
  blocks the mandate (fixed topology). No new framework is introduced to replace it (Temporal has
  its own separate, unchanged decision gate).
- **Migration impact:** P1.9 import-linter; P4.3 spine inversion (highest-risk migration #1,
  ships dark, eval-parity-gated, flag rollback); reconciler redrive logic ports to run-log replay
  incrementally.
- **Risks:** interrupt semantics regression (mitigated: approval invariant tests unmodified);
  double-bookkeeping window while checkpointer + run_events coexist (bounded by the parity drill).

## ADR-05 · LangChain — **REMOVE (trivially)**

- **Current role:** one import — `HumanMessage` (`api/chat.py:28,316`), transitively present via
  LangGraph regardless.
- **Verdict:** REMOVE the direct usage in P1 types work; no decision beyond a 2-line diff.
  It was never actually adopted; no chains/LCEL/agents/tools exist.

## ADR-06 · Neo4j — **ISOLATE now; fold-in EXPECTED at P5 gate** · ⚠ HUMAN SIGN-OFF

- **Current role:** (a) context graph — written from 8 modules, read by ~1, every write
  exception-swallowed: a write-only audit trail; (b) world model — `impact_of` gating destroys +
  drift bookkeeping: load-bearing but narrow (1–2 hop lookups).
- **Actual value:** (a) near zero as implemented; (b) real but small.
- **Problems:** an entire graph database + ops burden for queries Postgres recursive CTEs serve at
  this shape; degrades to "impact unavailable" honestly, proving it is not integrity-critical.
- **Target responsibility:** world-model impact/drift only, behind a `WorldModel` interface;
  context-graph writes redirect to `run_events` (which is the audit trail, queryable, and free).
- **Verdict:** ISOLATE behind the interface in P3 (impact extends to *all* mutations, so the gate
  measures real load); **fold into Postgres and REMOVE at the P5 gate unless** measured traversal
  depth/volume justifies a graph store. Direction restated unambiguously: fold-in is the default
  expected outcome; keeping Neo4j requires evidence.
- **Migration impact:** interface extraction (S); rebuild-from-inventory already exists as the
  data escape hatch; removal deletes an infra service from compose.

## ADR-07 · Terraform — **KEEP (the mutation engine, hardened)**

- **Current role:** all infrastructure mutation via 20 catalog templates; state-workspace
  isolation; plan artifacts; staged timeouts.
- **Actual value:** the moat, with real hygiene (plan sweeps, rc-124 classification).
- **Problems:** ~40 `_todo` policy stubs (F-11); MPP runtime-HCL promotion undercuts the
  "no runtime HCL" claim; catalog coverage gaps vs the 03 matrix; day-2 verbs don't fit TF.
- **Target responsibility:** C/U/X verbs exclusively, via the engine's TerraformExecutor
  (runner wrapped unchanged); day-2 registry covers what TF can't express; K8s executor uses
  pinned charts with server-side dry-run diffs.
- **Reason:** deterministic, planable, policy-checkable mutation is the platform's identity.
- **Migration impact:** P3 wraps; P3.8 real predicates; MPP promotion requires real predicates
  before registration.

## ADR-08 · Langfuse — **KEEP (traces only; cost moves out)**

- **Current role:** genuinely deep tracing (trace==run, cross-process deterministic spans,
  redaction, wrong-project detection) — *plus* the only home of cost data.
- **Problems:** cost in a resettable trace store vanishes with key rotation/retention; embeddings
  uncounted; SDK v2 pinned.
- **Target responsibility:** trace/observability UX only. Cost/tokens live in `llm_usage`
  (ground truth); Langfuse down ⇒ tracing degrades, ledger keeps recording.
- **Migration impact:** P0.3 ledger; Langfuse integration otherwise untouched.

## ADR-09 · Prometheus — **KEEP (fix the dead metric, chart the rest)**

11 real metrics, 7 alerts; F-10 fixed in P0.4; approval-wait finally observes; new metrics:
gate skip-rate, fallback hops, budget halts, eval-gate verdicts.

## ADR-10 · Grafana — **KEEP (earn it)**

1 dashboard charting 4 of 11 metrics today. P5.6 dashboards: spend (org/purpose/model), run flow,
eval history, governance posture. If dashboards aren't adopted by end of P5, fold into the
product's own ops console and remove — small, honest gate.

## ADR-11 · FastAPI — **KEEP**

Stateless, 2-worker proven, SSE mechanics solid. Target adds the worker-role split (F-18) — same
framework, role flag. No alternative offers a capability we lack.

## ADR-12 · Harness-first inversion — **ADOPT** · ⚠ HUMAN SIGN-OFF

The core structural decision (00 §3, 02, 04): kernel owns the loop/policy/budgets/memory/
verification; domains become packs; the graph spine retires. Alternatives rejected: (a) keep the
DAG and bolt an INV loop onto read paths only (the prior Brainstorming stance — preserved as
P1–P3, but it leaves the mandate's objective-driven behavior unreachable: mutation objectives
would still be single-pass); (b) adopt an external agent framework for the loop (adds a
dependency where 500 governed lines suffice — every reference harness proves the loop is the
easy part; the governance around it is ours already). Risk profile and dark-launch mitigation:
07 §highest-risk #1.

## ADR-13 · Extensibility — **code-reviewed packs; NO in-process plugins** 

Packs/skills/prompts enter via PR + eval gate. No runtime-loaded code, no hot-reload, no
self-mutating skills (OpenClaw CVE-2026-25157, Hermes curator, Waku mtime-reload all refused).
Sandboxed plugin surface = future gate, only on third-party demand.

## ADR-14 · Provider layer ownership — **own 6 thin adapters; LiteLLM = optional escape hatch**

Waku (11 providers/2 formats/~300 lines) and Pi (10 APIs × 40 configs) prove the bridge is small.
Owning the hot path buys deterministic streaming/tool semantics, our error taxonomy, capability
metadata co-designed with the router. LiteLLM ships as a disabled adapter for the long tail —
non-exclusive, revisited only on customer demand.

## ADR-15 · Native tool calling + bounded emulation — **ADOPT**

Native FC is the spine (all four references run native at runtime — including Hermes, whose XML
grammar turned out to be its training-export format, not its live loop). Emulation
(prompted-JSON + parser + one repair) exists only for read-effect purposes on `tools_emulated`
models; propose-effect purposes require `tools_native` — a guessed parse is not an audit-grade
record of intent.

## ADR-16 · Durable runs — **event-sourced run log alongside the checkpointer**

`run_events` is the source of truth for resume/audit/UI (Pi lane records, OpenHands stream);
the LangGraph checkpointer remains the interrupt substrate until the ADR-04 gate. Two records,
one owner each: checkpointer = "where can execution resume", run log = "what happened".

## ADR-17 · Credential brokering — **per-org short-lived credentials (P5.3)** · ⚠ HUMAN SIGN-OFF

The audit's most serious enterprise gap (F-20): one global long-lived key set for all tenants.
Target: broker-issued short-lived credentials (AWS AssumeRole / Azure SP + workload identity /
GCP service-account impersonation), org-scoped, vault-backed, dual-path during migration.
Sign-off needed on: vault choice, per-cloud federation design, tenant onboarding flow.

## ADR-18 · Evaluation gate as a release control — **ADOPT (P0, before everything)**

Waku's exit-1 gate, enterprise-shaped: deterministic dataset + judge thresholds + verdict history;
gates prompts, bindings, packs, and the P4 inversion. CI blocks on red. The single cheapest risk
reduction available and the precondition for every other change.

---

## Decisions requiring human approval (consolidated)

| # | Decision | ADR | When |
|---|---|---|---|
| 1 | Harness-first inversion (retire the graph spine) | ADR-12 | before P4 |
| 2 | LangGraph end-state (drop vs keep wrapped) | ADR-04 | gate after P4 +1 quarter |
| 3 | Neo4j fold-in/removal | ADR-06 | gate at P5 |
| 4 | Credential brokering design (vault, federation, onboarding) | ADR-17 | before P5.3 |
| 5 | Approval model: HITL default (initiator may approve); four-eyes demoted to optional org policy, default off — **decided by operator directive, resolved** | 07 P0.5 | resolved |
| 6 | AUTONOMOUS mode availability per env + pre-approved remediation verb lists per org | 03 §6.1 / 04 §8 | before P4.5 / P5.4 |
| 7 | Temporal adoption (standing gate; default: no) | 07 gates | on trigger only |
| 8 | max_steps raise 5→8; per-run concurrency caps | 07 P3.9 | before P3.9 |
