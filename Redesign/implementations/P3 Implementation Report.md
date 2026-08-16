# P3 Implementation Report — Durable Execution / Workflow Engine

> Branch: `feature/cloudops-v3` · base: `a35a9d5` (P0/P1/P2 uncommitted beneath) · Date: 2026-08-11
> Scope: Redesign/07 Phase 3, per the operator's P3 prompt.
> Status: **implemented and verified, uncommitted, awaiting operator acceptance.**
> Control ledger: Redesign/11 §31.

## 1. Implemented P3 Capabilities

A new package `app/engine/` turns the P2 harness's intelligent execution into a durable,
restart-safe Task/Run/Step system. A compiled goal-DAG runs as a `Workflow` of `Step`s in
dependency-ordered **waves**; every step's lifecycle and events are persisted; a process
death **recovers from durable state and continues without repeating completed work**; and a
mid-workflow failure **compensates completed steps in reverse (saga)**, freezing and paging
if a compensator fails. The full run-status machine (06 §8.3) replaces the phantom
`applying` (D5 now fully dead). The P2 Agent Harness is unchanged and remains authoritative
for reasoning — the engine orchestrates durability, nothing more.

Delivered against 07 Phase 3: (1) durable Task/Run/Step model (0015); (2) durable step
lifecycle + guarded state transitions; (3) wave-scheduled workflow engine; (4) step
ownership + idempotency keys; (5) restart/recovery semantics; (6) idempotent step execution
(no double-apply); (7) durable background-recovery entry (`recover_run`); (8) dependency-aware
waves with a disjoint-output compile check; (9) saga/compensation reverse-order + freeze;
(10) P2 harness integration for read/verify/gate steps; (11) `run_events` as the durable
record; (12) budgets (max_steps 5→8) + verification integrated; (13–14) approval/HITL and
tenancy/RBAC/redaction/idempotency preserved unchanged; (15) Redis per the approved policy;
(16) frontend surfacing of the new lifecycle; (17) additive API/events; (18) Langfuse/ledger/
run correlation preserved; (19) observability on every path; (20) the mandatory durable-
workflow acceptance demo.

## 2. Architecture Mapping

| Capability | Frozen source | Module |
|---|---|---|
| Task/Run/Step model | 06 §8.1 | `db/models.py` (`Task`, `RunStep`+cols, `Run.task_id`), migration 0015 |
| Full run-status machine (`applying` dead) | 06 §8.3, 07 P3.6 | `engine/status.py` |
| Workflow compile + waves + disjoint-output | 05 §5, 07 P3.1 | `engine/dag.py` (reuses `exec_loop.validate_dag`) |
| Durable step store + idempotency | 06 §8.1 | `engine/steps.py` (P0/A1 claim-or-recover) |
| Wave scheduler + recovery + single-writer status | 06 §8, 07 P3.1 | `engine/engine.py` |
| Saga reverse-order + freeze/page | 07 P3.2 | `engine/saga.py` |
| Durable run log (event-sourced, replay) | 06 §8.2, ADR-16 | reuses P2 `harness/run_log.py` |
| P2 harness integration (read/verify) | 00 §4, 07 P3.1 | `engine/driver.py` |

## 3. File / Change Map

New: `app/engine/{__init__,status,dag,steps,engine,saga,driver}.py`; migration
`0015_durable_execution`; models `Task` + `RunStep` durable columns + `Run.task_id`;
`tests/test_p3_engine.py`. Touched (additive): `app/db/models.py` (+`Task`, +6 RunStep cols,
+`Run.task_id`, `UTC` import), `app/settings.py` (+`aegisops_durable_engine`, +`aegisops_max_steps`),
`app/api/artifacts.py` (timeline: `rolled_back` as an honest terminal), frontend
`components/ArtifactPanel.tsx` (`statusColor` +compensated/rolled_back/verifying/scheduled/
awaiting_input). No existing execution path was rewritten; exec_loop and the LangGraph spine
are untouched.

## 4. Task / Run / Step Lifecycle

`Task` (objective container spanning runs) → `Run` (gains `task_id`) → durable `RunStep`
rows carrying `wave`, `depends_on`, `idempotency_key`, `kind`, `compensation_of`, `evidence`.
Step lifecycle: pending → running → done | failed | cancelled | **compensated**. Run status
(06 §8.3): running → scheduled/awaiting_approval/awaiting_input → executing → verifying →
completed, or → failed / **rolled_back** (saga) / cancelled. Transitions are guarded by
`can_transition`; the engine is the single writer for its runs; `applying` is not a state.

## 5. Workflow / Recovery Model

`compile_workflow` (Kahn layering + disjoint-output check + verbatim catalog guard) →
`execute_workflow` runs waves; each step: **claim-or-recover** (Redis A1 claim + durable DB
row) → execute (injected `StepExecutor`) → persist + `run_events` → next. On restart the same
function loads durable step state, skips `done` steps (and any whose idempotency claim is
already stored), and continues from the first incomplete wave. On step failure it compensates
completed steps in reverse via the injected `Compensator`; a compensator failure raises
`CompensationFrozen` → the run ends `failed` with a page-worthy signal (never a silent
half-rollback).

## 6. P2 Harness Integration

`engine/driver.harness_step_executor` runs read/verify/gate steps through the P2 INV loop
(`harness.inv.investigate`) — the harness reasons, the engine records durability. No second
loop, no LLM abstraction, no provider logic in the engine (verified: `app/engine` imports no
SDK and no langgraph; reasoning flows through the P2 harness → P1 service). Mutation kinds
(module/day2/k8s) return an honest "not wired in P3" outcome rather than pretending to apply —
real Terraform apply stays the untouched exec_loop/approval path.

## 7. Frontend Integration

The new terminal `rolled_back` renders as an honest, distinct state (amber "rolled back", not
red "failed") in the timeline API and `statusColor`; `compensated` steps and the new
transient statuses (verifying/scheduled/awaiting_input) have colors. The durable step trail
(waves, compensation) surfaces through the existing Timeline (run_steps) and Agent-Loop
(run_events: step_started/step_finished/deviation/verification) tabs — no new fetch needed.
tsc clean.

## 8. DB / Redis Changes

Migration 0015 (additive): `tasks` table, `runs.task_id`, six nullable `run_steps` columns;
downgrade present; applied to the dev DB only (:5433); no existing column removed (P3 rule).
Redis: durable idempotency claims reuse the P0/A1 keys under a `dstep:` namespace — the
no-double-apply guard; no new queue/framework (no Temporal — ADR gate unchanged).

## 9. Observability

Every step and status transition is a typed, redacted `run_events` row (step_started,
step_finished, deviation, verification, steering, run_finished); run_id correlation preserved;
reasoning steps remain P1 `llm.<purpose>` generations on the ledger. Recovery is visible
(`run_finished.recovered` lists skipped steps). No raw chain-of-thought anywhere.

## 10. GitNexus Impact

New surface with a single flag-gated integration; `app/engine` imports no SDK/langgraph
(boundary test scans it). The six LangGraph spine files and `terraform.py` show zero diff.

## 11. Tests and Acceptance Evidence

`test_p3_engine.py`: 4 pure pins (compile/waves, cycle/dup/non-catalog/collision rejection,
status machine) + 3 live durability proofs. **The mandatory acceptance demo**
(`test_durable_workflow_recovers_after_crash_without_repeating_work`): wave-0 steps
(`vpc`,`s3`) complete and durably store their results; the process "dies" before wave 1; a
restart re-runs the full workflow; `vpc`/`s3` are **recovered (skipped)** and only `eks`
executes — `calls == {"eks": 1}`, `recovered == {vpc, s3}` — proving completed work is not
repeated. Plus: saga compensates in reverse completion order; a failing compensator freezes
+ pages. All 7 green in the container tier (live PostgreSQL + Redis).

## 12. Regression Reconciliation

Single full local run: **1155 tests — 931 passed / 50 failed / 174 skipped** (21:45).
**INTRODUCED_BY_P3: 0** (programmatic diff vs the accepted baseline). All 50 failures are
the documented terraform-provider environment tiers (13× `test_modseed_*`,
`test_module_ingress`, `test_pr1_tf_hygiene`, `test_pr2_limits`, `test_rbac_endpoints`,
`test_scanner_waiver_guard`, `test_stab_p01_tfplugins`, `test_safety_invariants::TestStateIsolation`)
— every one requires terraform providers absent on this host, and the file grouping is
identical to the P2 run. **Zero failures in any phase suite** (`test_p3_*`, `test_p2_*`,
`test_p1_*`, `test_p0_*` = 0). Container tier (live datastores, real SDKs): **252 passed /
0 failed** across P3 + all P2/P1 + affected P0/governance/safety suites.

## 13. Transitional Components

T-P3-01: exec_loop mutation path coexists with the durable engine behind
`aegisops_durable_engine` (default off). Owner: engine. Replacement: durable engine + real
executors (P4). Removal condition: durable-path parity with real mutation executors. Rollback:
flag off → exec_loop unchanged. P2's T-P2-01 and P1's T-01 still stand.

## 14. Dead-Code Decisions

None removed. No `PROVEN_DEAD` candidates arose in P3 (the engine is new surface; exec_loop
and its `_todo` policy stubs are retained as the coexisting default, not dead).

## 15. Deferred Work

Recorded in doc 11 §21 (DEF-17, DEF-18): real durable executors (Terraform module apply,
day-2 registry breadth 07 P3.4, K8s executor + dry-run/rollout 07 P3.5, cross-cloud verify
breadth 07 P3.3, `_todo`→real predicates 07 P3.8) — they execute real cloud mutation and need
the CloudOps/K8s domain surface P3's boundary says not to migrate; the durable spine is
executor-injection-ready for them. And reconciler auto-recovery wiring (DEF-18) — recovery is
proven; the 60s-sweep call-site is a small additive follow-up.

## 16. P3 Boundary Verification

No P4 CloudOps/DevOps/SREOps or pack migration; no P5 broker/hardening; no AUTONOMOUS; no new
agent framework; no LangGraph removal/replacement; no new LLM/provider or memory architecture;
no CloudOps rewrite. The P2 harness is unchanged; mutation/governance paths (exec_loop,
approval, plan_guard, terraform, tenancy/RBAC/redaction) are untouched. Product runnable with
the flag off (default).

## 17. Rollback

Entire P3 surface is additive and uncommitted → discard reverts it. Committed:
`aegisops_durable_engine=off` disables the durable path (exec_loop unchanged is the default);
migration 0015 has a downgrade; the new endpoint/columns have no other consumers.

## 18. Known Limitations

The durable engine executes read/verify (harness-backed) and idempotent steps; real cloud
mutation executors are the deferred P4-adjacent slice (DEF-17), so the end-to-end durable
*mutation* workflow (the 07 exit's "VPC+VM+S3 under one approval, auto-compensate, K8s dry-run
diff") is proven at the durability-mechanism level with injected executors, not yet with real
Terraform/K8s. Windows async-loop limitation keeps the live durability tests in the container
tier. The dead sandbox Gemini key still blocks live LLM-driven harness steps on this host.

## 19. P3 Acceptance Verdict

**P3 COMPLETE — READY FOR ACCEPTANCE**

The durable execution engine implements Task/Run/Step with the full 06 §8.3 status machine
(`applying` dead), wave-scheduled dependency-aware execution, idempotent steps, saga
compensation with freeze+page, and restart recovery — and demonstrates the mandatory durable
workflow: start → multiple steps → simulated crash mid-workflow → restart → recover durable
state → continue → verify → complete, **with completed work provably not repeated**. The P2
harness is unchanged and authoritative for reasoning; the engine only orchestrates
durability. Zero regressions introduced; LangGraph and all mutation/governance paths
untouched; product runnable with the flag off (default). Real cloud-mutation executors are
the explicitly-deferred P4-adjacent slice (DEF-17); the durability spine is executor-injection
ready for them. Work is deliberately uncommitted pending operator acceptance (the P0/P1/P2
pattern). Stopping at the P3 gate — P4 is not started, and CloudOps/DevOps/SREOps are not
migrated.
