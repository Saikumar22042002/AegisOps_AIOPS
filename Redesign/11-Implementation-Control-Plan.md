# 11 — Implementation Control Plan

> **Status:** ACTIVE OPERATIONAL LEDGER — this is the migration control plane for implementing
> P1 → P5 against the live AegisOps repository. It is not an architecture document and does not
> restate 00–10; where it cites them, they win on intent and this document wins on *current
> migration state*.
>
> **Grounding:** branch `feature/cloudops-v3`, HEAD `9fa6d83`, plus the accepted-but-uncommitted
> P0 work (33 modified / 28 staged / 18 untracked paths — see §19). P0 was accepted by the
> operator on **2026-08-10** (see `Redesign/implementations/P0 Recovery and Implementation
> Report.md`). GitNexus index rebuilt clean 2026-08-10: **8,214 symbols · 15,929 relationships ·
> 300 flows**, covering the working tree including all P0 files.
>
> **Authority order:** 00 (boundaries) → 07 (sequence & gates) → 01–06, 08–10 (design detail) →
> this document (migration state, evidence, control). The mandate's constitution (00 §7) is not
> negotiable in any row of any table below.
>
> **The one question this document answers:** *how do we change the existing system without
> breaking existing behavior — frontend, backend, database, observability, CloudOps/DevOps/SREOps,
> or previously implemented phases?*

---

## 0. How to use this ledger

- Before starting any migration item: find (or add) its row in §2, run the §7 dependency
  procedure, and check §12 (phase entry) and §26 (change control).
- After finishing any migration item: run §24 (implementation checkpoints), update the row's
  Status/Evidence, and update §9/§8/§21 if the item created a transitional component, killed
  code, or discovered later-phase work.
- **Status vocabulary (fixed):** `CURRENT` · `ANALYZED` · `MIGRATION_PENDING` · `IN_PROGRESS` ·
  `COMPATIBILITY_LAYER` · `MIGRATED` · `VERIFIED` · `DEPRECATED` · `REMOVAL_READY` · `REMOVED` ·
  `DEFERRED` · `BLOCKED`.
  - `CURRENT` = live production component, not yet dependency-analyzed for its migration.
  - `ANALYZED` = consumers/deps mapped (GitNexus + repo-wide search) and recorded here.
  - `VERIFIED` = migrated **and** behavioral parity + consumer verification passed.
- Nothing may move to `REMOVED` except through `REMOVAL_READY`, and nothing reaches
  `REMOVAL_READY` except through the removal gates in §8/§9.

---

## 1. Current repository baseline

Source of truth: the repository itself (inspected 2026-08-10), cross-checked against
`Redesign/01` (audited at `a974290`) and the P0 report. All paths relative to
`aegisops_production_kit/backend` unless noted. Backend ≈ 17.9k LOC app / 101 test files;
frontend ≈ 4.2k LOC (Next.js 14 App Router, single zustand store).

### 1.1 LLM / model substrate

| Component | Path (LOC) | Owner / consumers / dependencies |
|---|---|---|
| Agent LLM facade | `app/agents/llm.py` (121) | Exposes `classify_json`, `stream_answer`, `_extract_json` (+`_TRUNCATION_NOTE`). **8 production call sites in 6 modules:** `router.py:162`, `cloudops.py:44,67,198`, `devops.py:61` (classify_json); `general.py:69`, `knowledge.py:53`, `sre.py:121` (stream_answer). Non-app consumers: `evals/runner.py:29` (`_extract_json`), `tests/test_stream_resilience.py`, `tests/test_p0_defects.py`. Emits Langfuse `generation(name="gemini.stream")` + `usage_ledger.record_usage(purpose="answer_stream")`. Dead `generate()` already deleted (P0). |
| Gemini singleton | `app/integrations/gemini.py` (170) | `get_gemini(settings)` lazy singleton; `GeminiLLM.astream/agenerate/aembed`; run-model pin via **contextvar** `_run_model` (`set_run_model`/`get_run_model`); `usage_of(resp)`. GitNexus: **CRITICAL, 19 impacted** (memory.build_context, rag retrieve, devops_plan, api/integrations, cloudops extract fns, both facade fns). Langfuse generations at L120/L129. |
| Validate-only provider seam | `app/integrations/llm/` — `base.py` (50), `gemini_provider.py` (47), `registry.py` (42), `__init__.py` | `get_provider(settings, model)` is the single model-resolution choke point; raises `UnknownModelError`, no silent fallback. Consumers: `api/integrations.py` (`GET /models`), chat model validation, `tests/test_llm_provider.py`. **Validates only — never dispatches.** |
| Provider failure triage | `app/agents/provider_errors.py` (177) | `classify_provider_error`, `suggest_retry`, `failure_message`; kinds `credentials_expired · api_disabled · iam_denied · name_taken · quota_exceeded · bad_location`. D1 (`bad_location`/`bad_region` mismatch) fixed at P0, pinned in `test_p0_defects.py`. Consumers: cloudops retry seam, exec_loop deviation path, frontend `error.retry`. |
| Cost ledger | `app/integrations/usage_ledger.py` (254) + `llm_usage` table (migration `0010`) | P0 output. `bind_run` contextvar attribution; `record_usage` never raises; idempotent insert → bounded retry (0.2/0.8/1.8s) → fsync'd spill journal (`settings.aegisops_ledger_spill_path`) → reconciler replay. Wired at generate, stream, and embedding sites, success + error paths. `task_id`/`prompt_version` intentionally NULL until P2/P3. |
| Embeddings | `gemini.py:139 aembed` (768-d, `gemini-embedding-001`) ← `app/rag/embeddings.py` and `api/chat.py:233,319 → memory.embed_message` | 768-d pinned in `db/models.py:32 EMBED_DIM` **and** `settings.gemini_embed_dim` **and** two pgvector columns — rebinding the embeddings purpose is a re-embedding data migration (ADR-02). |

### 1.2 Graph, execution & approval

| Component | Path (LOC) | Owner / consumers / dependencies |
|---|---|---|
| LangGraph spine | `app/agents/graph.py` (123) — `build_graph`, `init_graph` (called `main.py:91`), `get_graph` | **12 nodes:** router, cloudops_plan, devops_plan, sre_analyze, knowledge, general, approval, execute, verify, finalize, servicenow_update, notify. Conditional edges `_after_router` (5-way), `_after_plan` (3-way), `approval_decision` (2-way). No backward edge — single pass. |
| LangGraph surface | 6 importing files: `approval.py`, `checkpointer.py`, `exec_loop.py`, `graph.py`, `runner.py`, `state.py` | APIs used: StateGraph/compile · `interrupt` · `AsyncPostgresSaver` · `Command(resume)` · `aget_state` · `add_messages`. No Send/subgraphs/streams/store (confirms ADR-04: checkpoint substrate, not a framework). LangChain = one import (`HumanMessage`, `api/chat.py:28`) — ADR-05 2-line removal. |
| Checkpointer | `app/agents/checkpointer.py` (54) — Postgres `AsyncPostgresSaver`; `thread_id == run_id` (`runner.py:41`) | Resume via `Command(resume=…)` in `runner.py`. |
| Durable interrupts | **2 real `interrupt()` sites:** `approval.py:62`; `exec_loop.py:42` (`_request_reapproval = interrupt`, invoked at `:306` for deviations — monkeypatchable seam) | Plus **5 client-facing SSE `interrupt` events** (not graph interrupts): `cloudops.py:836,1268,1397`, `devops.py:124`, `sre.py:149`, `exec_loop.py:166`. Doc 01 counts "3 interrupt sites" — see contradiction C-10. Governance stamp (P0) wired at both real sites. |
| Exec loop | `app/agents/exec_loop.py` (358) | `MAX_STEPS=5`, `MAX_REPLANS_PER_STEP=1`, replanner defaults to `None` (never replans); `plan_goal_dag`, `validate_dag` (rejects non-catalog `template_key`, `:96`), `execute_governed_step`, `execute_goal_dag`, `resolve_wires` (wiring grammar `"<out>"`, `"<out>[i]"`, `"input:<field>"`), `_partial_outcome`, `_cancel_requested` (boundary-only). Caller: `execute.py` node (GitNexus: 1 direct). Behind `aegisops_exec_loop` flag (default **off**). |
| Approval core | `app/agents/approval.py` (100) — `_guard_action`, `approval` (interrupt), `approval_decision`; `app/agents/plan_guard.py` (77) — `check_plan_actions` re-asserted at choke-point (`approval.py:44`) and per-step (`exec_loop.py:144,237`) | Approvals row model immutable; **single-user HITL is THE approval model** (`initiator == approver` valid everywhere — 00 §7.4; the four-eyes concept was removed entirely at the P1 entry gate, 2026-08-10 — no flag, no gate branch). Approval concurrency lock `approval:inflight:{run_id}` (Redis NX EX 900). |
| Runner | `app/agents/runner.py` (94) — `run_graph(run_id, channel, initial=, resume=)` | Langfuse trace begin/end per run (trace_id == run_id). |
| Reconciler / supervisor / retention | `reconciler.py` (263, 60s sweep, `EXECUTING_STATES=("running",)`), `supervisor.py` (160, heartbeat 45s/15s, cancel TTL 3600), `retention.py` (79) | Role-gated at `main.py:106` (`aegisops_role in ("all","worker")` + `aegisops_reconciler=="on"`) — P0 F-18 fix. |
| Investigation registry | `app/agents/investigation.py` (156) — `ToolRegistry`, `assert_read_only`, `MAX_CALLS=8`, freeze-at-start | Sole production caller: hardcoded `inv.call("list_deployments", namespace="default")` (`sre.py:84-85`). `Investigator.run/spawn`: zero production callers (§8 DC-04). |

### 1.3 Domain agents (CloudOps / DevOps / SREOps)

| Component | Path (LOC) | Notes |
|---|---|---|
| CloudOps | `app/agents/cloudops.py` (**1,531**) | `cloudops_plan` (:470), `cloudops_execute` (:1409), `_read_path` (:925), `_destroy_resource` (:1122), `_modify_resource` (:1280); 8 regex intent interceptors; world-model destroy gate `_world_model_impact_check` → `impact_of` (`cloudops.py:1105-1107`). Mutation site `cloudops.py:1463-1464` (idempotency key `tf-exec`). |
| DevOps | `app/agents/devops.py` (223) | 6 declared stages, 4 real; F-14 (placeholder Dockerfile/CI push, CI-poll image check) open until P5.2. |
| SREOps | `app/agents/sre.py` (213) | `decision_matrix` thresholds hardcoded; 5 hardcoded PromQL; F-15 (self-referential error-rate signal) open until P2.9. |
| Cloud read tools | `tools/aws.py` (137, 6 services) · `tools/azure.py` (89, 3) · `tools/gcp.py` (79, 2) · `tools/vmware.py` (84, orphan) | F-12 read/verify asymmetry vs the 7/7/6 write catalog. Read-only by docstring contract. |
| K8s / GitHub / Ansible | `tools/kubernetes.py` (155 — **only mutating SDK**: apply/restart/scale/rollback), `tools/github.py` (184), `tools/ansible.py` | Post-approval-only side effects by contract. |
| Templates / tool catalog | `app/agents/templates.py` (562) — `TEMPLATES` (20: 7 AWS / 7 Azure / 6 GCP), `select`, `by_key`, `catalog`, `register_promoted` (runtime, fed by MPP + startup `rehydrate_promoted`) | Policy fns per template; `_ck` real predicate vs `_todo` honesty stub — **~45 `_todo(` sites** (F-11, fixed P3.8). Failure gate `exec_loop.py:241-244`. |
| Terraform runner | `tools/terraform.py` (492) + `tools/console.py` (116) | `TF_WORKSPACE` isolation, `-var` only, plan-file lifecycle, `planned_resources` (`show -json`). **Exactly 3 apply/destroy call sites, all post-approval:** `cloudops.py:1463-1464`, `exec_loop.py:247` (+ destroy variant), each idempotency-guarded (`tf-exec` / `loop-step` keys). |
| Module pipeline (MPP) | `app/agents/module_pipeline.py` (229) | draft→checks→propose→review→promote; promoted modules get blanket `_todo_policy` (F-11). Runtime registration = dynamic dependency invisible to GitNexus. |
| Inventory / drift / world model | `inventory.py` (404), `drift.py` (273, dormant, aws-ec2 only — F-22), `graph_db/world_model.py` (214, `impact_of` load-bearing), `graph_db/context_graph.py` (266, write-mostly) | ADR-06: context-graph writes are near-zero-value; world model is real but narrow. |

### 1.4 API & SSE surface

9 routers / 41 endpoints + inline `GET /metrics` (bearer-gated, `main.py`). Key files:
`api/chat.py` (637 — `POST /chat` SSE, `POST /approvals/{run_id}` SSE, `GET /chat/stream/{run_id}`
SSE w/ Last-Event-ID, `GET /runs/{run_id}`, `POST /runs/{run_id}/cancel`), `api/artifacts.py`
(409 — 8 artifact tabs + `POST /runs/{id}/credentials` step-up), `api/modules.py` (381),
`api/sessions.py` (143), `api/auth.py` (148), `api/integrations.py` (122 — `GET /models`),
`api/gateways.py` (138), `api/health.py` (80 — `/healthz` carries governance stamp since P0),
`api/knowledge.py` (45).

**SSE event vocabulary (owner: `app/agents/events.py` `Emitter`, framed by `chat.py:_sse`):**
`run · step · token · analysis · params · reference · confidentiality · console · interrupt ·
error · done` (11 types). Internal only: `_EOS`, `DONE` sentinel. Event bus: memory or Redis
Streams `run:<id>:events` (`AEGISOPS_EVENT_BUS`; non-local memory refused at startup since P0).

**Response typing:** pydantic request models exist (`ChatRequest`, `ApprovalRequest`,
`ChatContext`; `schemas/auth.py`; `schemas/workflows.py` 21 input classes) but **run/session/
artifact responses are bare dicts** — only `/auth/login` and `/auth/me` have `response_model=`.
Contract drift is currently caught only by `tests/test_sse_contract.py` and frontend usage.

### 1.5 Frontend (consumer side)

Next.js 14.2 App Router; single zustand store `lib/store.ts` (491); API layer `lib/api.ts`
(no retries/timeouts/interceptors); SSE via `lib/sse.ts` (POST + `fetch` reader — **no
EventSource, no Last-Event-ID resume, no reconnect**). Calls ~27 endpoints (full map in §4).
Status literals known to the frontend: run status only `"awaiting_approval"` is compared
(`store.ts:218`); timeline statuses `done/running/pending/rejected/failed/cancelled`;
approval `pending/approved/rejected` (**global singleton keyed to `activeRunId`, not per-run**).
`GET /models` menu is live since P0 (D4). Governance stamp: **not rendered anywhere** (§4 FE-09).
F-13: the `/approvals/{run_id}` stream handler consumes only `console/token/step/done/error` and
drops `analysis/reference/confidentiality/params/interrupt/run` frames.

### 1.6 Data stores

- **PostgreSQL (+pgvector):** 18 models (`db/models.py`, 402): organizations, roles, users,
  sessions, messages (Vector 768), feedback, runs (status/plan_json/outcome/trace_id/ended_at),
  run_steps, approvals, documents, document_chunks (Vector 768), audit_log, integrations,
  resources, user_memories, channel_identities, channel_link_codes, module_proposals,
  notifications, **llm_usage** (P0). Migrations `0001`–`0010` + LangGraph checkpoint tables.
- **Redis (9 uses):** event bus streams · run heartbeat · cancel flag · idempotency claims ·
  approval in-flight lock · auth sessions/PKCE · pending-parameter cache (TTL 1800) ·
  credential-reveal one-shot · drift dedupe (24h NX). Client `app/cache/redis.py`.
- **Neo4j:** context graph (8 writers, 1 real reader) + world model (`impact_of` gates destroys).

### 1.7 Observability

Langfuse (`integrations/langfuse_client.py`, 339): trace==run_id, generation sites
(`agents/llm.py:79`, `gemini.py:120,129`), tool spans (terraform, servicenow, rag, cloudops
availability), step spans (`timing.py`), `assert_project` at startup, redaction on every payload,
degrades to no-op. Prometheus: **13 metrics** (11 legacy + `LEDGER_RECORDS`, `LEDGER_SPILL`);
`APPROVAL_WAIT` records since P0 (F-10). Grafana: 1 dashboard, 4 metrics charted (gap → P5.6).
Cost/tokens accounting truth = `llm_usage` (P0); Langfuse is observability only (ADR-08).

### 1.8 Security & governance

`security/` — deps (183), tenancy strict (100), idempotency (79), redaction (69), sessions (58),
rbac (48), confidentiality (46), **governance_stamp (46, P0)**. Keycloak OIDC + step-up re-auth.
Gitleaks pinned v8.24.3 (CI + pre-commit, `--redact`); `.gitleaks.toml` structural rules; 5
operator-classified sandbox credentials exact-path allowlisted. `/metrics` bearer-gated (F-16).
Rate limiting Redis-backed when bus=redis (F-17). Open: F-20 global long-lived cloud credentials
(until P5.3); F-21 residue items tracked in §21.

### 1.9 Configuration flags (governance-relevant, `app/settings.py`)

`app_env=local` · `aegisops_tenancy=strict` · `aegisops_event_bus=memory` (non-local memory
refused at startup) · `aegisops_reconciler=on` · `aegisops_drift=off` · `aegisops_exec_loop=off`
· `aegisops_role=all` ·
`default_execution_mode=plan` · `aegisops_metrics_token` · `aegisops_ledger_spill_path` ·
`aegisops_tf_backend=local` · `aegisops_telegram=off` · TF timeouts 600/2700s. (The
`aegisops_four_eyes_for_production` flag was removed entirely at the P1 entry gate — single-user
HITL is not flag-conditional.) F-19:
`AEGISOPS_COST_GUARDRAIL_USD` still raw `os.getenv` (`cost.py:74`) — undiscoverable.

### 1.10 Evals & tests

Evals: `backend/evals/` (runner 67, gate 102, judge 79, dataset.jsonl, judge_dataset.jsonl —
2 cases, thin) + `app/evals/scoring.py` (76, the one scorer). CI `evals` job required.
Tests: 101 files. **Invariant suites (rule one — never modified silently):**
`test_safety_invariants.py` (330), `test_tenancy.py` (599), `test_exec_loop.py` (231); adjacent:
`test_policy_real.py`, `test_rbac*.py`, `test_redaction.py`, `test_idempotency.py`,
`test_confidentiality.py`. P0 pinning: 5 `test_p0_*.py` files (42 tests). Contract/stream:
`test_sse_contract.py`, `test_stream_resilience.py`, `test_event_bus_redis.py`,
`test_langfuse_tracing.py`.

---

## 2. Component migration ledger

Primary control table. `Target` paths are from 02 §9 (frozen). Statuses per §0 vocabulary.
Evidence keys: `[GN]` = GitNexus (fresh index 2026-08-10), `[GREP]` = repo-wide search,
`[P0]` = P0 report §, `[Dnn/Fnn]` = defect register, `[07]` = migration plan item.

| ID | Component | Current path | Target (path · owner) | Phase | Status | Key consumers / dependencies | Compatibility strategy | Verification | Removal gate | Rollback | Evidence |
|---|---|---|---|---|---|---|---|---|---|---|---|
| L-01 | LLM facade (`classify_json`/`stream_answer`) | `app/agents/llm.py` | `app/llm/service.py` · llm layer; **byte-compatible shim** kept at `agents/llm.py` | P1.3 | ANALYZED | 8 call sites / 6 modules (§1.1); `evals/runner.py`; 2 test files | Shim preserves signatures + `_TRUNCATION_NOTE` semantics; `purpose=` threaded per call site; **rule zero: eval gate green before re-route** | Eval gate exit 0 pre/post; `test_stream_resilience`, `test_p0_defects`, targeted caller tests | Shim deleted **end of P2** after all callers import `app/llm` directly (07 removal table) | Revert shim internals to direct Gemini call (git) | [GN] 0-impact false floor; [GREP] 8 sites; [07 §1.3] |
| L-02 | Gemini singleton | `app/integrations/gemini.py` | `app/llm/adapters/google_.py` · adapters | P1.2→1.3 | ANALYZED | **[GN] CRITICAL / 19 impacted**: facade, `memory.build_context`, `rag/retriever`, `rag/embeddings`, `api/integrations`, `devops_plan`, cloudops extract fns | Singleton survives until P1.3 cutover; contextvar run-model pin (`set_run_model`) must map to RoutePlan run-pin semantics; embeddings path (`aembed`) migrates in the same slice or keeps the singleton alive | Adapter contract tests (recorded fixtures, P1.5 pattern); D2/D3 pinning tests; ledger rows still written at all 3 sites | **Deleted end of P1** with `integrations/llm/` seam (07 removal table) | Reinstate singleton import path (shim keeps it possible until deletion) | [GN] CRITICAL; [P0 §19] passthrough deletions; [07 removal] |
| L-03 | Validate-only provider seam | `app/integrations/llm/` | `app/llm/catalog.py` + `config/models.yaml` · llm layer | P1.4 | ANALYZED | `GET /models` (frontend menu, D4), chat model validation, `test_llm_provider.py` | `GET /models` response shape (`{models:[{id,provider,enabled,default}]}`) is **frozen** until §4 FE-05 verified; catalog must serve it byte-compatibly | `test_llm_provider.py` ported; FE menu manual + Vitest | Deleted end of P1 | Re-point `GET /models` to registry seam | [GREP]; [D4 closed at P0] |
| L-04 | Provider error taxonomy | `app/agents/provider_errors.py` | `app/llm/errors.py` · llm layer | P1.1 | ANALYZED | cloudops retry seam, exec_loop deviation, frontend `error.retry{label,retry_message,kind}` | Failure kinds + `suggest_retry` contract preserved verbatim (D1 pinned); frontend retry payload shape frozen | `test_p0_defects.py` D1 test; SSE `error` contract test | Old module removable only when no `agents/*` imports remain (end P2 with shim) | Keep old module as re-export | [D1]; [GREP] |
| L-05 | Model routing / purposes / bindings | none (contextvar pin only) | `app/llm/router.py` + `model_bindings` + Settings UI | P1.6–1.7 | MIGRATION_PENDING | New consumers: every LLM call site via service | Greenfield; governed purposes (`router/planner/loop.main/judge`) never user-pinnable; two sources of truth rule: yaml = CAN run, DB = who runs what, boot cross-validation | P1 exit: UI rebind `knowledge → claude-sonnet-5` zero-code; scenario V (10) | n/a (new) | Binding rows revert to `models.yaml` default | [04 §4.4]; [07 §1.6-1.7] |
| L-06 | Cost ledger | `app/integrations/usage_ledger.py` + `llm_usage` | `app/llm/usage.py` · llm layer (module move only; table is the contract) | P0 done; move in P1 | VERIFIED (P0) | `agents/llm.py`, `gemini.py` (3 wired sites), metrics, spill reconciler | Table schema frozen; `task_id`/`prompt_version` NULL until P2/P3 by design | 8 ledger tests + durability chain [P0 §10]; live-DB test container-gated | Old module path removable end of P1 after import move | Module re-export shim | [P0 §10]; [0010] |
| L-07 | Eval gate | `backend/evals/` + `app/evals/scoring.py` | same (grows P4.7 dimensions) | P0 done | VERIFIED (P0) | CI required job; rule-zero gate for P1.3 | One-scorer rule enforced by `app/evals/__init__.py` docstring + imports | Gate exit 0; self-test rejects known-bad | n/a — permanent control | n/a | [P0 §9] |
| L-08 | Governance stamp | `app/security/governance_stamp.py` | same; artifact grows at P3 | P0 done | VERIFIED (P0) | `approval.py`, `exec_loop.py`, `api/health.py` | 9 flags incl. `approval_model: hitl`; additive fields only | Stamp tests; `/healthz` assertion updated at P0 | n/a | n/a | [P0 §11]; FE gap → §4 FE-09 |
| L-09 | LangGraph 12-node spine | `app/agents/graph.py` | retired as spine; interrupt/checkpoint substrate behind `harness/interrupts.py` + `graph_glue.py` | P4.3 (**ADR-04 sign-off**) | CURRENT | `main.py:91` init; runner; all 12 node fns | Inversion ships **dark behind a flag** until eval parity on both topologies (07 risk #1) | Behavioral evals on both topologies; IP-1..4 re-run on loop-as-spine (10 P4 exit) | Graph-as-spine removable only after P4.3 parity + sign-off; substrate retained ≥1 quarter (ADR-04 gate) | Flag flip back to graph spine | [ADR-04]; [07 §4.3] |
| L-10 | LangGraph checkpointer | `app/agents/checkpointer.py` | KEEP, ISOLATED (`harness/` only import law) | P2.5 parallel; exit gate post-P4 | CURRENT | runner resume; approval interrupts | Checkpointer stays **authoritative for resume** until `run_events` replay passes kill-drills (ADR-16: two records, one owner each) | kill -9 resume drills on worker 2 (P2 exit; P3 exit wave variant) | Harness-native durability gate: ≥1 quarter post-P4, replay covers every resume case | Checkpointer never removed before gate — rollback is "keep using it" | [ADR-04/16] |
| L-11 | Exec loop | `app/agents/exec_loop.py` | `app/engine/` (dag/steps/engine/saga/locks/windows) · engine | P3.1–3.2 | ANALYZED | [GN] 1 direct caller (`execute.py` node); reconciler resume; `test_exec_loop.py` (invariant suite); flag `aegisops_exec_loop` | Compile closures kept **verbatim** (catalog/bounded/wiring/guard); keep/generalize/new table is the PR checklist; waves off (sequential) until disjoint-output checks prove out; invariant tests unmodified (rule one) | `test_exec_loop.py` green unmodified; P3 demo script (07); kill -9 mid-wave resume | Module deleted at P3.1 **only after** engine passes the full P3 exit + invariants | Old module retained importable until gate; flag routes back | [GN]; [07 risk #2] |
| L-12 | Agent Harness kernel | none | `app/harness/` (kernel/loop/hooks/budgets/policy/subagents/verification/context/interrupts) | P2 | MIGRATION_PENDING | First callers: `sre._collect_telemetry`, `cloudops._read_path` (P2.2) | **Rule two: read paths only**; kernel ≤500 lines, no SDK imports (CI + import-linter from P1.9); mutation paths stay on engine/approval | P2 exit scenarios E,F,I,N,P,Q,R,U,O-read,IP-1..4 | n/a (new) | Kernel-read-path flag off → hardcoded legacy paths (kept until parity) | [04 §11]; [07 §2.2] |
| L-13 | Tool catalog (Terraform templates) | `app/agents/templates.py` | catalog **kept** (constitution); registration surface becomes `tools/registry.py` ToolDef v2 | P2.1 wrap; P3.8 policies | ANALYZED | exec_loop policy gate, cloudops/plan nodes, MPP `register_promoted` (runtime/dynamic) | Template keys + `select`/`by_key` semantics preserved; `_todo` stubs replaced per-template, parity-gated (F-11); MPP-promoted modules require real predicates before registration | `test_policy_real.py`; per-template parity tests at P3.8 | `_todo` blanket rows removed P3.8 | Per-template revert (policy fns are pure) | [F-11]; [GREP ~45 `_todo(` sites] |
| L-14 | Investigation registry | `app/agents/investigation.py` | same registry, driven by kernel INV loop | P2.2 | ANALYZED | `sre.py:84-85` hardcoded call; `MAX_CALLS=8` inherited by budgets (stricter wins, 09 §3) | Registry stays frozen + read-only; kernel becomes the director; denylist + freeze semantics unchanged | Read-only violation tests; INV scenario N | `Investigator.run/spawn` per §8 DC-04 | Hardcoded caller retained until INV parity | [01]; [07 §2.2] |
| L-15 | Task/Run/Step entities | `runs`/`run_steps` tables; no Task concept | 06 §8.1 contract set; status machine 06 §8.3 | P2.5 (run_events) / P3.6 (machine) | ANALYZED | chat.py writers, reconciler, artifacts API, frontend `GET /runs/{id}`; **multiple status writers today** | Additive columns only; new statuses introduced **only with** §4 FE-06 slice; single-writer-per-literal rule adopted at P3.6 | `test_sse_contract`; status-machine tests at P3.6; D5 pinning | `applying` literal fully dead P3.6 (reads already removed P0) | Status writes are data — revert = stop writing new literals | [D5]; [06 §8.3] |
| L-16 | run_events log | none | `run_events` table (17-kind enum, 06 §8.2) · kernel | P2.5 | MIGRATION_PENDING | Kernel, engine, UI projections, Langfuse projections, replay-resume | Dual-record with checkpointer (ADR-16); append-only; redact-at-write; Redis stream stays the live feed | Gapless-`seq` invariant test (10 §0); replay reconstructs loop state | n/a (new) | Table is additive; readers fall back to checkpointer | [06 §8.2]; [ADR-16] |
| L-17 | Memory (conversational) | `app/agents/memory.py` | `app/memory/` tiers + `memory_items` | P2.3 (gate) / P2.6 (tiers) | ANALYZED | chat embed path, all plan nodes via `build_context` | Existing 70/30 transcript budgeting preserved as working-tier recipe; retrieval becomes **gated** (fails open — behavior superset); `user_memory` (M4) human-write-only path unchanged | Scenario Q (recall), P (compaction); gate skip-rate observable | Old unconditional-retrieve path removable after gate parity | Gate fails open = old behavior | [06 §4]; [01] |
| L-18 | Context assembly | `memory.build_context` (+ `_PURPOSE_BUDGET`) | `harness/context.py` recipes (3 bands) | P2.4 | ANALYZED | All LLM call sites via nodes | Per-purpose budgets carried over as recipe params; memory snapshot frozen at run start | Context-recipe unit tests; compaction scenario P | Old builder removable end of P2 after all nodes route through kernel | Recipe flag → legacy builder | [06 §6] |
| L-19 | Redis substrate | `app/cache/redis.py` + 9 uses | KEEP (ADR-03) + breaker (P1.6) + wave locks (P3.1) | P1.6/P3.1 additive | CURRENT | All §1.6 uses | Availability-critical, never a record; Redis down ⇒ refuse new runs (already enforced non-local at startup, P0) | `test_event_bus_redis`, `test_p0_governance_worker_redis` | n/a | n/a | [ADR-03]; [P0 §12] |
| L-20 | Worker role split | `main.py` role gating (`AEGISOPS_ROLE`) | worker owns loop execution, engine steps, reconciler, retention, consolidation (06 §8.4) | P0 done; grows P2.5/P3 | VERIFIED (P0) | compose override (one sweep owner) | Same image, role by env flag; no queues/schedulers until P3 | Role-gating tests [P0 §13] | n/a | Flag to `all` | [P0 §13] |
| L-21 | Langfuse tracing | `integrations/langfuse_client.py` | KEEP, traces only (ADR-08) | done | VERIFIED (P0) | All §1.7 span sites; artifacts deep-links | Cost lives in ledger; Langfuse down ⇒ degrade, ledger keeps recording | `test_langfuse_tracing.py` (354) | n/a | n/a | [ADR-08] |
| L-22 | Prometheus metrics | `app/metrics.py` (13) | KEEP + new metrics (gate skip-rate, fallback hops, budget halts, eval verdicts) | P2+ additive | CURRENT | `/metrics` (bearer), alert rules (7) | Additive only; F-10 fixed | Metric-presence smoke (§16 SM-08) | n/a | n/a | [ADR-09]; [P0] |
| L-23 | CloudOps agent | `app/agents/cloudops.py` (1,531) | dissolves into `packs/cloudops/{aws,azure,gcp}` — regex interceptors → deterministic pre-classifiers; plan/read/destroy code → pack tools + kernel behavior | P2.2 (read path) → P4.2 (dissolve) | CURRENT | graph nodes; world-model destroy gate; templates; params; provider_errors | Strangler: `_read_path` gets kernel-driven twin first (P2.2); mutation entry (`cloudops_execute`) unchanged until P4.3; interceptors retired only at P4.2 with eval parity | Scenario A (AWS) at P4 exit; behavioral evals across inversion | `cloudops.py` deleted at P4.2 gate only | Flag: kernel read-path off | [07 §4.2]; [GN] node-dispatch invisible — grep verified |
| L-24 | DevOps agent | `app/agents/devops.py` | `packs/devops/github/` + P5.2 capability completion | P4.2 / P5.2 | CURRENT | graph nodes; github tool | F-14 practices banned at P5.2 (PR-first, no default-branch push); D8 hardcoded `passed: True` rows die with pack policies | Scenario L (P5.2) | Old agent deleted P4.2 | Same dark-launch flag as L-09 | [F-14]; [D8] |
| L-25 | SREOps agent | `app/agents/sre.py` | `packs/sreops/k8s/` + read expansion P2.9 | P2.2/2.9 → P4.2 | CURRENT | graph nodes; investigation registry; K8s tool | `_collect_telemetry` replaced by kernel INV at P2.2 (first caller); per-service PromQL fixes F-15 at P2.9 | Scenario N; SRE triage ≥3 tools with evidence trail (P2 exit) | Old agent deleted P4.2 | Hardcoded telemetry path retained until parity | [F-15]; [07 §2.2/2.9] |
| L-26 | Terraform executor | `tools/terraform.py` | wrapped **unchanged** as `engine/executors/terraform.py` (ADR-07) | P3.1 | CURRENT | 3 mutation call sites (§1.3); state workspaces; plan-file hygiene sweeps | Runner wrapped verbatim; C/U/X only via engine; TF safety boundary is constitutional | `test_safety_invariants`; modseed tiers (CI with providers) | Old import path removable after engine adoption complete (P3 exit) | Engine delegates to same runner — rollback = call sites revert | [ADR-07] |
| L-27 | K8s executor | `tools/kubernetes.py` | `engine/executors/k8s.py` (pinned charts, server-side dry-run diff, rollout verify/undo) | P3.5 | CURRENT | devops/sre paths | Only mutating SDK today — mutation authority moves under engine gate; reads become pack tools | Scenario K | SDK-mutation outside engine banned at P3.5 | Old path kept until scenario K green | [07 §3.5] |
| L-28 | Cloud read tools | `tools/{aws,azure,gcp}.py` | pack ToolDefs; three-cloud read parity | P2 (registry) / P5.1 (parity gate) | CURRENT | cloudops read/discovery; INV registry | Per-row parity gate (03 §3.4 rule 2): a family ships only when all three clouds reach the same verb set | Parity gate in CI (P5.1) | vmware.py per §8 DC-03 | Additive | [F-12]; [03 §3] |
| L-29 | Day-2 verb registry | none | `engine/executors/day2.py` + `DAY2_ACTIONS` | P3.4 | MIGRATION_PENDING | SRE/CloudOps lifecycle verbs | Greenfield; blast-radius-tiered approval | Scenarios B/C/D (AWS) at P3 exit | n/a | n/a | [07 §3.4] |
| L-30 | Capability packs | none | `packs/` (5 packs, 02 §4 contract) | P4.2 | MIGRATION_PENDING | kernel registry; CI pack validation | Pack registration contract (05 §10); code-reviewed, no runtime plugins (ADR-13) | Pack CI checks; eval per pack | n/a | n/a | [ADR-13] |
| L-31 | Neo4j (both uses) | `graph_db/` | context-graph writes → `run_events`; world model behind `WorldModel` interface | redirect P2.5+; decision at P5 (**ADR-06 sign-off**) | CURRENT | `impact_of` destroy gate (load-bearing); `resource_provenance` (1 reader) | Interface extraction first; fold-in to PG recursive CTEs is the *expected* outcome | Destroy-gate behavior pinned before any change (§15 BP-06) | Neo4j removable only via ADR-06 measured gate | Interface keeps both backends possible | [ADR-06] |
| L-32 | SSE/event contract | `app/agents/events.py` Emitter | unchanged vocabulary through P1–P2; versioned contract authored at P2.5 (see C-02) | P2.5 | ANALYZED | web store (11 events), gateways `driver._consume`, `test_sse_contract.py` | **Additive-only, never rename/retype** until a versioned contract exists; unknown events are silently dropped by FE — additive is wire-safe but every additive event needs an FE decision (§5) | `test_sse_contract.py` extended per change | n/a | Emit both old+new during any transition | [C-02]; [F-13] |
| L-33 | API response contracts | bare dicts (§1.4) | response models introduced opportunistically per §4 slice | per phase | CURRENT | frontend (27 endpoints), gateways | Adding `response_model=` must be shape-identical (serialization audit per endpoint) | Contract tests per endpoint before/after | n/a | Remove response_model | [§1.4] |
| L-34 | Frontend state/types | `lib/store.ts`, `lib/types.ts` | per-slice evolution (§4) | per phase | CURRENT | all UI | Matrix §4 governs; backend contract "migrated" **only when** FE consumer verified | Vitest + Playwright per slice | n/a | git | [§4] |
| L-35 | DB schema | 18 models, `0001–0010` | additive DDL only (ADR-01); new tables per §6 | per phase | ANALYZED | everything | §6 rules; never delete old fields because new code stopped reading them | migration test vs dev DB (§6) | field removal via §8 gates only | alembic downgrade scripts mandatory | [ADR-01] |
| L-36 | Gateways / channels | `app/gateways/` | as today + `slack/ teams/ webhook/` | P5.5 | CURRENT | Telegram poller (role-gated); notify approval pushes | Transport Protocol is the seam (GW-1 preserved); click-time re-check identical on new transports | gw test family (`test_gw1_*`) | n/a | n/a | [02 §9] |
| L-37 | Drift subsystem | `app/agents/drift.py` (dormant) | reconciler-scheduled, beyond aws-ec2 | P5.7 | CURRENT | world model; notifications | Flag stays off until P5.7 | drift tests | n/a | flag | [F-22] |
| L-38 | Credentials | global long-lived set (`terraform.py:170-187`) | per-org short-lived broker (`security/broker`) | P5.3 (**ADR-17 sign-off**) | CURRENT | every cloud call + TF env | **Dual-path** (broker with global-key fallback per org) until all orgs migrate; broker outage ⇒ refuse new mutations, never silent fallback | Scenario W case 2 | Global key set removed at P5.3 gate | Per-org fallback flag | [F-20]; [ADR-17]; [07 risk #3] |
| L-39 | Frontend model menu | `TopNav.tsx` ← `GET /models` | unchanged; source becomes P1.4 catalog | P0 done | VERIFIED (P0) | L-03 shape freeze | `enabled` currently fetched but not filtered — P1.7 must decide (§4 FE-05) | Vitest menu test | n/a | n/a | [D4] |
| L-40 | Subagents | none (`Investigator.spawn` dead) | `harness/subagents.py` (typed `AgentResult`, shared pool, depth 1) | P2.7 | MIGRATION_PENDING | kernel | Blocked verbs in child; child output = untrusted evidence | Scenario U | n/a | n/a | [05 §6] |

---

## 3. Contract ownership registry

Rule: **one owner per contract per era.** Where today's owner is "multiple writers", the row
carries a consolidation requirement — no two components may silently become competing owners.

| Contract | Current owner | Current consumers | Current version | Target owner | Target phase | Compatibility requirement | Breaking-change risk | Verification |
|---|---|---|---|---|---|---|---|---|
| **Task** | *(does not exist — sessions approximate)* | — | n/a | Control-plane Task/Run manager (06 §8.1) | P3 | New concept; must not repurpose `sessions` rows | LOW (greenfield) | New contract tests at P3 |
| **Run** | `db/models.py Run` + writers in `chat.py`, `exec_loop.py`, `reconciler.py` (**multi-writer**) | frontend `GET /runs/{id}`, artifacts, reconciler, gateways | implicit (bare dict) | Run per 06 §8.1 + status machine 06 §8.3, **single writer per literal** | P2.5 / P3.6 | Additive statuses only, coordinated with §4 FE-06; `awaiting_approval` literal frozen (FE restore path depends on it) | **HIGH** — FE compares literal strings | `test_sse_contract`; status-machine suite (P3.6) |
| **Step** | `run_steps` (writers: `timing.py`, exec_loop bookkeeping) | artifacts timeline, reconciler | implicit | engine Step contract (kind: module\|day2\|k8s\|read\|gate) + `wave`, `evidence`, `compensation_of` | P3.1 | Additive columns; existing rows readable forever | MEDIUM | engine step tests; timeline artifact test |
| **Tool invocation** | *(no typed contract — prompt-and-parse; reads via investigation registry)* | domain agents | n/a | `ToolDef`/ToolCall + middleware order (05 §1/§3) | P2.1 | Registry freeze-at-run, 20-marker mutation denylist preserved | LOW (greenfield) | middleware order test; registration-time denylist test |
| **Tool result** | ad-hoc dicts + `Evidence` (investigation) | agents | n/a | `ToolObservation{ok, stage, error}` — failure never raises (L3) | P2 | Error-as-observation must not change existing SSE `error` semantics on legacy paths | LOW | scenario F |
| **Approval** | `approvals` row + interrupt payload (`approval.py`) + `governance_stamp` | frontend card, gateways `approval_card`/`approval_buttons`, audit | implicit | immutable Approval + full artifact (verify+rollback plans, flags) 06 §8.1 | P3 | Additive payload fields only; decision literals `approved/rejected` frozen; HITL semantics constitutional | **HIGH** (governance) | approval tests; scenario S/T |
| **Plan** | `runs.plan_json` + exec_loop DAG dict | frontend terraform tab, approval card | implicit | compiled `Workflow` artifact, **hash-bound to approval** (05 §5) | P3.1 | plan_json shape kept as projection until FE terraform tab migrates | MEDIUM | plan-hash binding test (10 §0 inv. 4) |
| **SSE events** | `agents/events.py Emitter` (single owner ✅) | web store, gateways driver, `test_sse_contract` | implicit v1 (11 types) | same owner; versioned schema authored at P2.5 | P2.5 | **Additive-only; no rename/retype/removal** without §5 sequence | **HIGH** | `test_sse_contract` |
| **API responses** | per-router bare dicts | frontend, gateways | implicit | typed response models per slice | per phase | Shape-identical introduction only | MEDIUM | per-endpoint contract tests |
| **Model invocation** | `agents/llm.py` signatures | 6 modules | implicit | `llm.generate()`/`llm.stream()` canonical messages (04 §4) — **no normative schema in suite (C-01)** | P1.1 | Shim keeps old signatures until end P2 | MEDIUM | contract tests per adapter (P1.5) |
| **Model response** | `usage_of(resp)` ad hoc | facade, ledger | implicit | `ServedBy` + `Usage` + error taxonomy (04 §4.6/4.7) | P1.1 | Ledger fields already match target (P0) | LOW | adapter fixture tests |
| **Memory/context** | `build_context(purpose=…)` | plan nodes | implicit | context recipes (06 §6) + memory tiers | P2.3–2.6 | M4 `user_memory` human-write-only preserved; recall behavior superset | MEDIUM | scenarios P/Q |
| **Run events** | *(none)* | — | n/a | `run_events` 17-kind enum (06 §8.2) — kernel owns | P2.5 | Enum extensions recorded here (see C-05/C-06 already pending) | LOW (new) | gapless-seq invariant |
| **Cost ledger** | `usage_ledger.record_usage` + `llm_usage` (single owner ✅) | SQL/chargeback, budgets (P2), metrics | v1 (0010) | `app/llm/usage.py` (module move) | P1 | Table schema is the contract; writers only via the one module | LOW | 8 ledger tests |
| **EvidenceCard** | *(none — `finalize.verify` reconcile-checks approximate)* | — | n/a | 05 §8 EvidenceCard — verify **produces**, never a bool | P3.3 | Verification claims in current cards must not be silently reinterpreted | LOW (new) | scenario M; goal-validation tests |
| **Terraform execution result** | `TerraformRunner` return values + `planned_resources` JSON | exec_loop policy gate, cloudops, plan guard | implicit | **no typed contract in suite (C-04)** — must be authored with the P3.1 Step contract | P3.1 | `planned_resources` (`change.after`) shape frozen — policy predicates parse it | MEDIUM | `test_policy_real`; modseed tiers |

---

## 4. Frontend/backend compatibility matrix

**Rule (binding):** a backend contract is **not migrated** until every affected frontend consumer
is verified (typecheck is insufficient — most payloads are `any`; see hazards below).

Frontend hazards that shape every row: (a) artifact tab payloads, `GET /runs/{id}`, and
`fromApiMessage` are **untyped `any`** — schema changes will not fail typecheck; (b) several
payloads carry **presentation values** (`color`, `statusColor`, `lvlColor`, `subColor`, `dot`,
`deltaColor`, `indent`) — renaming these breaks rendering silently; (c) unknown SSE events are
**silently dropped** (no default branch, no logging).

| ID | Contract | Backend owner | Frontend consumer | Current shape | Target shape | Compatibility | Phase | Test | Removal gate |
|---|---|---|---|---|---|---|---|---|---|
| FE-01 | `POST /chat` SSE | `chat.py` + `events.Emitter` | `store.ts:340-398` switch (11 events) | 11 event types (§1.4) | same vocabulary + additive events (e.g. future `served_by` badge P1.7) | additive-only; every new event needs an FE handler or explicit ignore-list entry in `store.ts` | P1.7+ | `test_sse_contract` + Vitest store tests | never removed |
| FE-02 | `POST /approvals/{run_id}` SSE | `chat.py` | `store.ts:432-456` if/else (**5 of 11 events only — F-13**) | drops `analysis/reference/confidentiality/params/interrupt/run` | full event parity on approval continuation | fix belongs to the first P2/P3 slice that touches approval streaming; until then no new event may be *required* on this stream | P2.5/P3 | new Vitest case per event | F-13 closed |
| FE-03 | `GET /chat/stream/{run_id}` (Last-Event-ID reattach) | `chat.py` | **nobody** — `sse.ts` parses `id:` but never resumes | backend supports resume; FE reconnect logic absent | 10-O requires reattach for process-restart scenario | FE reconnect work is part of the P2.5 vertical slice (run durability is invisible to users without it) | P2.5 | Playwright reconnect test | n/a |
| FE-04 | `GET /runs/{id}` | `chat.py:582-593` (bare dict) | `store.ts:217,280` (restore path compares `status === "awaiting_approval"`) | `id,status,workflow,plan_json,session_id` | + new statuses `scheduled/verifying/rolled_back/awaiting_input` (06 §8.3) | `awaiting_approval` literal frozen; **new statuses ship only with FE handling in the same slice** — today they fall into the gray default branch | P3.6 | store restore test per status | old statuses never removed |
| FE-05 | `GET /models` | `api/integrations.py` (L-03) | `TopNav.tsx:141-204` | `{models:[{id,provider,enabled,default}]}` | same + bindings/eval_state (P1.7 Settings UI) | shape frozen through P1.4; P1.7 must decide `enabled` filtering (currently fetched, unfiltered) and may only add fields | P1.4/P1.7 | Vitest menu test | n/a |
| FE-06 | Approval card (SSE `interrupt` payload) | `approval.py`/domain agents | `Workspace.tsx:192-273` | `runId, workflow, plan{summary,steps[]}` | + full artifact: verify plan, rollback plan, governance flags (P3) | additive fields; `plan.summary` + `plan.steps[{order,template,name}]` frozen | P3 | approval-card Vitest + Playwright | n/a |
| FE-07 | Approvals artifact tab | `artifacts.py` | `ArtifactPanel.tsx:344-369` | `status,risk,affected,servicenow,cost_impact,decisions[]` | + EvidenceCards (P3.3) | additive | P3.3 | tab render test | n/a |
| FE-08 | Approval decision POST | `chat.py` | `store.ts approveRun` + CommandPalette (approves `activeRunId` **global singleton**) | `{decision}` | + rationale, per-run keying | FE approval state must become per-run before any multi-run concurrency work (P3); backend must reject decision for non-`awaiting_approval` runs (already: in-flight lock) | P3 | multi-run Playwright | n/a |
| FE-09 | Governance stamp | `governance_stamp.py` (stamps every card + `/healthz` since P0) | **not rendered anywhere** (grep: zero hits) | backend-only | approval artifact P3 renders stamped flags | FE rendering lands with FE-06 slice; until then stamp is API-visible only — acceptable, but it is a P0 feature invisible to approvers (flagged in §21 DEF-06) | P3 | stamp render test | n/a |
| FE-10 | Artifact tabs ×8 | `artifacts.py` | `ArtifactPanel.tsx` (`ArtifactTab` type = URL segment = deep-link `?tab=`) | 8 tab names | unchanged; payloads grow | **tab names frozen** (renaming breaks routing + deep links); payloads additive; presentation-value fields frozen | all | Playwright deep-link test | n/a |
| FE-11 | Sessions/messages | `sessions.py` | `store.ts`, `fromApiMessage` (any) | snake_case messages | unchanged until Task concept (P3) | additive | P3 | store tests | n/a |
| FE-12 | Auth `User` | `schemas/auth.py` (typed ✅) | `lib/auth.tsx`, RBAC gates (`can_approve` etc.) | typed | + broker-era claims (P5.3) | field renames forbidden — approve/initiate gating reads these | P5.3 | auth tests | n/a |
| FE-13 | Error/retry payload | `events.Emitter` error + `provider_errors` | `store.ts` retry button | `error{message,retry{label,retry_message,kind}}` | + taxonomy kinds (P1.1) | existing kinds frozen; new kinds additive (FE shows generic retry) | P1.1 | error-path Vitest | n/a |
| FE-14 | `POST /runs/{id}/credentials` | `artifacts.py` (step-up) | `Workspace.tsx CredentialReveal` (401/404/410 special-cased) | one-shot reveal | unchanged until P5.3 | broker must preserve one-shot + step-up semantics | P5.3 | reveal tests | n/a |

---

## 5. API / SSE / event migration control

For every public contract change, this sequence is mandatory:

1. **Record here first** — current contract, target contract, consumers (backend, frontend,
   gateways, tests), and whether gateways' `driver._consume` needs the same handling as the web
   store (it consumes the same stream).
2. **Versioning strategy:** the platform has no wire-version field (C-03). Until one exists, the
   only permitted evolution is **additive** — new event types, new optional fields. Rename/
   retype/removal requires: emit-both transition → consumers migrated → old shape removal-gated.
3. **Compatibility adapter:** where both shapes must coexist, the adapter lives at the emit site
   (`events.Emitter`), never in consumers.
4. **Migration sequence:** backend additive emit → FE handler (or explicit ignore) → tests on
   both → flip → removal gate.
5. **Tests:** `test_sse_contract.py` is the wire pin; extend it in the same PR as any emit change.
6. **Removal condition:** old shape removable only after §4 row verified + one full phase of
   coexistence.

**Never silently change a public event schema.** The silent-drop behavior of the frontend
(FE hazard c) makes violations *invisible*, not safe.

Current inventory to migrate against: 11 SSE event types (§1.4), 41 REST endpoints (9 routers),
Last-Event-ID reattach on `GET /chat/stream/{run_id}` (backend-only today, FE-03).

---

## 6. Database migration control

Rules (binding): inspect existing queries before touching any object · additive DDL only
(ADR-01) · **never delete old fields merely because new code stopped reading them** (precedent:
`runs.ended_at` was "dead" in D7 and is now written on every terminal transition) · every
migration tested against the development DB when possible · downgrade path authored with the
upgrade · data-migration requirements and rollback implications recorded here.

| Schema/Object | Current | Target | Migration | Consumers | Backward compatible | Data migration | Tests | Rollback | Removal gate |
|---|---|---|---|---|---|---|---|---|---|
| `llm_usage` | `0010` **APPLIED to the dev DB 2026-08-10** (alembic head `0010_llm_usage`; 17 columns verified column-for-column; `ix_llm_usage_org_ts` + `ix_llm_usage_run` + PK present; live write/read/idempotency proven — double-insert counts once) | unpartitioned **by decision C-07** (partition triggers recorded in §22) | done | ledger, SQL chargeback, budgets (P2) | yes (new table) | none; spill journal replays into it | `test_p0_ledger` 9/9 in the api-test container (live tier) | drop table (pre-adoption only) | never |
| `model_bindings` | none | PK(org_id,purpose), eval_state, updated_by/reason (06 §8.2) | new migration P1.7 | llm router, Settings UI | yes (new) | seed from `models.yaml` defaults | binding CRUD + eval-gate promotion tests | table drop pre-adoption | never |
| `run_events` | none | 17-kind enum, `UNIQUE(run_id,seq)`, JSONB redacted-at-write (06 §8.2) | new migration P2.5 | kernel, replay, UI projections | yes (new) | none | gapless-seq invariant; replay drill | additive | never |
| `memory_items` | none | DDL per 06 §1 (exact columns recorded in this plan's source extract) | new migration P2.6 | memory tiers, consolidation | yes (new) | none | supersede-not-coexist test (Q-c) | additive | never |
| `prompt_registry` | none | PK(name,version), content_hash, eval_state (05 §9) | new migration P2.8 | PromptRefs, ledger `prompt_version` | yes (new) | backfill `prompt_version` on new rows only | registry tests | additive | never |
| `run_steps` | `0001` | + `wave`, `evidence JSONB`, `compensation_of` | additive migration P3.1/3.2 | engine, timeline tab | yes (nullable adds) | none | engine step tests | column adds are additive; downgrade drops | old columns never |
| `runs.status` values | interim machine (P0): running→awaiting_approval→executing→completed/failed/cancelled | full machine (06 §8.3): + scheduled/verifying/rolled_back/awaiting_input; `applying` literal fully dead | **data values, not DDL** — new literals written only from P3.6, single writer each | reconciler `EXECUTING_STATES`, FE restore path, artifacts | old literals never removed | audit for stray `applying` rows before P3.6 flip | D5 pinning + status-machine suite | stop writing new literals | `applying` dead at P3.6 |
| `messages.embedding` / `document_chunks.embedding` | Vector(768), pinned | same; provider change = re-embedding migration | only via ADR-02 procedure | memory retrieve, RAG | n/a | full re-embed if dim/provider changes; admin UI must refuse hot swap | dim-pin test | restore from old column copy | never hot |
| `llm_usage.task_id`, `.prompt_version` | always NULL (by design) | populated from P2 (prompt_version) / P3 (task_id) | no DDL — writer change | chargeback queries | yes | none | ledger tests extended | writer revert | never |
| LangGraph checkpoint tables | owned by `AsyncPostgresSaver` | unchanged through P4; removal only via ADR-04 gate | none | resume path | — | — | resume drills | — | ADR-04 gate |

---

## 7. GitNexus dependency control

### 7.1 Index state (must be current before any phase's high-impact edits)

| Date | Event |
|---|---|
| 2026-08-03 | Index built at `a974290` (7,606 symbols) — predates P0 files |
| 2026-08-10 | Incremental re-analyze **failed**: `FTS index 'file_fts' is inconsistent … Drop and recreate` — symbol lookup broken |
| 2026-08-10 | `clean --force` + full re-analyze: **8,214 symbols · 15,929 relationships · 300 flows**, working tree incl. all P0 files. `gitnexus status` = fresh |
| 2026-08-10 (P1 entry gate) | Re-analyzed after the four-eyes removal: **8,236 nodes · 15,946 edges · 244 clusters · 300 flows.** Pre-edit impact recorded for every touched symbol: `resolve_approval_core` (3 — resolve_approval + driver callbacks), `governance_stamp` (4 — approval, exec_loop.plan_goal_dag, healthz, stamped), `approval_pending`/`notifiable_approvers` (0 static — module-attr false floor, grep-verified), `posture` (2) |

Operational notes: two repos are indexed on this machine — **every CLI call needs
`-r AegisOps_AIOPS`** (MCP calls need `repo: "AegisOps_AIOPS"`). When the MCP bridge is not
mounted in a session, the CLI is equivalent: `node .gitnexus/run.cjs impact <symbol> -r
AegisOps_AIOPS` (also `context`, `query`, `detect-changes`, `trace`). VECTOR extension not
installed → semantic `query` uses exact-scan fallback (results valid, slower).

### 7.2 Recorded findings (fresh index, 2026-08-10)

| Symbol (phase) | GitNexus result | Repo-wide-search truth | Verdict |
|---|---|---|---|
| `get_gemini` (P1) | **CRITICAL — 19 impacted** (facade fns, `build_context`, rag retrieve/embeddings, `devops_plan`, `list_integrations`, cloudops extract fns) | matches | **P1.2/1.3 is the highest-blast-radius edit of P1** — every consumer listed in L-02 must be in the P1.3 test net |
| `classify_json` (P1.3) | 0 impacted, LOW | **8 call sites, 6 modules** (module-attribute calls: `llm.classify_json`) | **false floor** — GitNexus misses `module.attr` call edges |
| `stream_answer` (P1.3) | 0 impacted, LOW | 3 call sites | false floor (same mechanism) |
| `execute_goal_dag` (P3.1) | 1 impacted (`execute.py` node) | + reconciler resume path, flag gating, invariant tests | floor — runtime/dispatch consumers invisible |
| `cloudops_plan` (P4.2) | 0 impacted, LOW | registered via `graph.add_node` — dispatch is runtime | false floor — LangGraph registration produces no static edge |
| `check_plan_actions` (P3) | 0 impacted, LOW | 3 assertion sites (approval choke-point + 2 exec_loop sites) | false floor |

### 7.3 Binding rules

1. **Impact is a floor, never a ceiling.** A low count is evidence of few *static* edges, not of
   safety. Known-invisible dependency classes in this codebase: LangGraph node registration and
   conditional edges · module-attribute calls (`llm.classify_json`) · MPP runtime registration
   (`register_promoted`/`rehydrate_promoted`) · monkeypatch seams (`_request_reapproval`) ·
   contextvars (`_run_model`, ledger binding) · settings-driven dispatch · SSE event consumers
   (frontend, gateways).
2. Every high-impact migration records: callers · imports · dependents · direction · API
   consumers · frontend consumers · tests · workers · configuration · LangGraph references ·
   registries · discoverable dynamic references — **in its §2 ledger row**, with GitNexus output
   cross-verified by repo-wide search, source inspection, tests, runtime behavior, config, and
   DB schema before the edit.
3. `impact` before editing any symbol; `detect-changes` before any commit (CLAUDE.md contract).
4. Re-run `analyze` after each merged migration batch; a stale or corrupted index (§7.1 shows
   both failure modes) **blocks** the next high-impact edit until rebuilt.
5. Cluster/module labels in tool output are unreliable (e.g. `approval_buttons` labeled
   "Tests") — trust `filePath`, never `module`.

---

## 8. Dead code / removal ledger

Classifications: `PROVEN_DEAD` (all evidence gates passed) · `APPARENTLY_DEAD` (static evidence
only) · `TRANSITIONAL` (alive until replacement + consumer migration + parity + gates).
**Only PROVEN_DEAD may become an immediate removal candidate. Nothing is deleted by this
document.** TRANSITIONAL items remain until: replacement exists → consumers migrated →
behavioral parity proven → GitNexus + grep show no required consumers → runtime verification →
removal gate passes.

| ID | Component | Classification | Evidence | Consumers checked | Replacement | Migration complete | Removal gate | Status |
|---|---|---|---|---|---|---|---|---|
| DC-01 | `agents/llm.generate()` | PROVEN_DEAD | zero callers (D7); deleted at P0 (`llm.py:43` comment) | GitNexus+grep+tests | n/a | n/a | passed | **REMOVED (P0)** |
| DC-02 | `GeminiProvider.astream/agenerate`, `gemini.astream_text` dead passthroughs | PROVEN_DEAD | P0 §19: deletions verified boundary-clean | yes | n/a | n/a | passed | **REMOVED (P0)** |
| DC-03 | `tools/vmware.py` (84 LOC) | APPARENTLY_DEAD | no template targets it (01); orphan | grep pending: investigation `default_registry`, docs, seeds | none planned | n/a | decide at P4.2 pack extraction: port or PROVEN_DEAD it | HELD |
| DC-04 | `Investigator.run/.spawn` | TRANSITIONAL | zero production callers (01) | registry itself is live (sre.py:84) | kernel INV loop (P2.2) + `harness/subagents.py` (P2.7) | at P2.7 | kernel drives registry in prod + scenario U green | HELD until P2.7 |
| DC-05 | `github.create_pull_request` | APPARENTLY_DEAD (retained deliberately) | never called (D7) | grep | P5.2 makes PR-first the default change vehicle — will be rewritten, not deleted | P5.2 | n/a — retention is the decision | HELD |
| DC-06 | `"applying"` status literal | TRANSITIONAL | reads removed at P0 (D5, zero writers proven, regression-tested) | chat.py/reconciler/artifacts read sites cleared | full status machine | P3.6 | 07 removal table: literal fully dead P3.6 | reads REMOVED (P0); literal HELD |
| DC-07 | frontend `lib/data.ts workflowNodes()` seed set | APPARENTLY_DEAD | exported, referenced nowhere (frontend inventory) | grep frontend | none | n/a | frontend cleanup slice (any phase) after Vitest sweep | HELD |
| DC-08 | frontend `store.runError` surface | APPARENTLY_DEAD as UI (written, never rendered; read only by tests) | grep components | n/a | decide: render it (defect fix) or remove writes | P2 FE slice | decision recorded here first | HELD |
| DC-09 | `runs.ended_at` | ~~dead (D7)~~ **ALIVE** | P0 gave it writers on all terminal transitions | — | — | — | — | CLOSED — the misclassification precedent that justifies these gates |

---

## 9. Transitional architecture register

Expected pattern for every entry: `OLD → COMPATIBILITY ADAPTER → NEW → PARITY → CONSUMER
MIGRATION → VERIFICATION → REMOVE OLD`.

| ID | Old implementation | New implementation | Adapter | Current consumers | Remaining consumers (exit condition) | Phase | Owner | Removal condition | Rollback | Evidence |
|---|---|---|---|---|---|---|---|---|---|---|
| T-01 | `agents/llm.py` direct-Gemini internals | `app/llm/service.py` | **byte-compatible shim at `agents/llm.py`** | 8 call sites (L-01) | 0 direct importers of shim | P1.3 → end P2 | llm layer | all callers import `app/llm`; eval gate green | revert shim internals | 07 removal table |
| T-02 | `integrations/gemini.py` singleton + `integrations/llm/` seam | `app/llm/adapters/google_.py` + catalog | service dispatch | 19 impacted (L-02) | 0 | P1.2–1.4 → **deleted end of P1** | llm layer | grep `google.genai\|anthropic\|openai` in `app/agents app/packs` → 0 (P1 exit) | singleton retained until gate | [GN CRITICAL] |
| T-03 | Hardcoded read paths (`sre._collect_telemetry`, `cloudops._read_path`) | kernel-driven INV loop | flag: kernel-read-path (P2, to be created) | SRE/CloudOps read flows | legacy path callers = 0 after parity | P2.2 | harness | INV parity + P2 exit scenarios | flag off | 07 §2.2 |
| T-04 | LangGraph checkpointer resume | `run_events` replay-resume | **dual-record, one owner each** (ADR-16): checkpointer = resume authority, run log = record | runner resume | replay covers every resume case the checkpointer serves | P2.5 → ADR-04 gate (≥1 quarter post-P4) | harness | measured gate + human sign-off | keep checkpointer (default) | ADR-04/16 |
| T-05 | `exec_loop.py` | `app/engine/` | Step contract; compile closures verbatim | `execute.py` node, reconciler, flag | invariant tests green against engine; demo script passes | P3.1–3.2 | engine | full P3 exit + invariants unmodified | old module importable until gate | 07 risk #2 |
| T-06 | Interim status machine (P0) | full machine (06 §8.3) | additive literals, single writer each | reconciler, FE, artifacts | FE handles new statuses (§4 FE-04) | P3.6 | control plane | `applying` dead; stale-approval re-validation live | stop writing new literals | D5 |
| T-07 | 12-node graph as spine | harness loop as spine | **dark-launch flag** (P4.3, mandated) + `graph_glue.py` | all objective classes | eval parity on both topologies | P4.3 | harness | IP-1..4 + scenario A on loop topology; sign-off ADR-12 | flag flip | 07 risk #1 |
| T-08 | Fat domain agents (cloudops/devops/sre) | capability packs | pre-classifiers replace regex interceptors | graph nodes | pack parity per domain | P4.2 | packs | behavioral evals per domain green | agents retained until gate | 07 §4.2 |
| T-09 | Global cloud credential set | per-org broker | **dual-path per org** (broker, global fallback) | every cloud call + TF | all orgs migrated | P5.3 | security | broker outage semantics verified (refuse, never silent) | per-org fallback | ADR-17; 07 risk #3 |
| T-10 | Neo4j world model direct calls | `WorldModel` interface | interface extraction | destroy gate, drift, provenance | fold-in or keep decision | ADR-06 gate at P5 | platform | measured query-shape evidence + sign-off | interface keeps Neo4j backend | ADR-06 |

---

## 10. Feature flag / dark-launch control

Control mechanism only — **no new flags are implemented by this document.**

Existing flags that already gate migration-relevant behavior (all in `app/settings.py`, §1.9):
`aegisops_exec_loop` (off — the P3 substrate's current guard) · `aegisops_event_bus` ·
`aegisops_role` · `aegisops_reconciler` ·
`aegisops_drift` · `aegisops_telegram` · `default_execution_mode`.

Planned control points (to be created *in their phase*, recorded here now):

| Flag (working name) | Old path | New path | Default | Rollout | Comparison | Rollback action | Removal condition |
|---|---|---|---|---|---|---|---|
| P2 `harness_read_paths` | hardcoded `sre._collect_telemetry` / `cloudops._read_path` | kernel INV loop | off | per-purpose, staging→prod reads | side-by-side evidence trails; eval gate | flag off | legacy read paths deleted end P2 after parity |
| P3 `engine_exec` (supersedes `aegisops_exec_loop`) | exec_loop module | `app/engine` | off | staging demo script → prod sequential (waves off) → waves | invariant suites + kill-drills on both | flag back to exec_loop | exec_loop removal gate (T-05) |
| P4 `loop_as_spine` (**mandated dark launch**, 07 §4.3) | graph spine | harness loop spine | off (dark) | dark until eval parity both topologies | same behavioral dataset, both topologies | flag flip | graph-spine retirement per ADR-04 gate |
| P5 `credential_broker` (per org) | global key set | brokered short-lived | off per org | org-by-org | mutation success + audit parity | org fallback to global | global set removed when all orgs migrated (T-09) |

Rules: a risky migration without a lever here does not start (07 rule three: every phase's
rollback demonstrated in staging before the phase is "done"). Planner/judge purposes never
silent-fallback (04) — flags must not create silent degradation paths.

---

## 11. Phase dependency graph

```
P0 (DONE, accepted 2026-08-10)
 ↓
P1  Provider layer (app/llm)
 ↓
P2  Agent Harness kernel on read paths
 ↓
P3  Durable execution / workflow engine (app/engine)
 ↓
P4  Harness-first domain migration (the inversion)
 ↓
P5  Production parity / credentials / channels / hardening
```

Explicit dependencies (07; do not invent new ones):
- **P1 → P2** (kernel needs purpose routing, native tool calling, typed errors, budget-gated executor)
- **P1 + P2 → P3** (engine consumes harness observations + run_events; Step contract needs typed tool results)
- **P3 + P2 → P4** (inversion needs durable runs + engine mutation path + kernel loop)
- **P4 → P5** (parity/credentials/channels harden what the inversion produced)

Standing decision gates that cut across phases (07): Temporal (default stay) ·
harness-native durability (ADR-04, post-P4 quarter) · LiteLLM (ship disabled) · Neo4j fold-in
(ADR-06, end P5) · per-step approval UX · plugin sandboxing (ADR-13, default no).

## 12. Phase entry gates

| Phase | Must be true BEFORE the phase begins |
|---|---|
| **P1** | P0 accepted ✅ (2026-08-10) · migration ledger exists ✅ (this doc) · baseline known ✅ (§19) · provider impact analysis complete ✅ (§7.2: L-01/L-02 mapped, CRITICAL radius named) · **rule zero standing**: eval gate green before any dispatch-path change (incl. P1.3 internal re-route) · GitNexus index fresh ✅ (2026-08-10) · P0 work committed to the branch (currently uncommitted — see R-13) · migration `0010` applied to the dev DB (§6 row 1) |
| **P2** | P1 accepted · provider abstraction verified (two providers green on evals — P1 exit) · model routing verified (UI rebind zero-code; scenario V ×3) · P1 regression clean or classified per §19 · import-linter contract active (P1.9) · `agents/llm.py` shim still byte-compatible (it survives through P2) |
| **P3** | P2 accepted · `run_events` available + gapless invariant holding (P2.5) · harness execution proven on read paths (P2 exit scenarios) · resumability understood: replay-resume drill passed on worker 2; checkpointer parity posture documented (T-04) · keep/generalize/new table authored for exec_loop (07 risk #2 checklist) |
| **P4** | P3 accepted · durable execution verified (kill -9 mid-wave resume, no double-apply) · capability/tool contracts stable (ToolDef/pack contract unchanged for a full phase) · domain migration impact mapped (per-domain ledger rows at ANALYZED with §7 evidence) · **ADR-12 human sign-off obtained** · dark-launch flag exists (T-07) |
| **P5** | P4 accepted · parity evidence available (evals green both topologies; IP-1..4 on loop spine) · production security requirements ready: **ADR-17 sign-off**, broker design approved, F-20/F-21 disposition current · AUTONOMOUS-mode + pre-approved verb list sign-off (08 decision 6) before P5.4 |

## 13. Phase exit gates

A phase is **not complete because its local tests pass.** Every phase exits through all ten:

1. Unit tests (new + existing) green.
2. Integration tests green (Redis/DB/graph paths).
3. API contract tests green (`test_sse_contract` + per-endpoint pins extended in-phase).
4. Frontend tests when any §4 row was touched (Vitest + affected Playwright specs).
5. Behavioral acceptance: the phase's scenario set from 10 §3 —
   P1: **V** (all 3 cases) · P2: **E F I N P Q R U O-read IP-1..4** · P3: **B/C/D(AWS) J K S T H
   O-mutation** · P4: **A(AWS) M IP-1..4 re-run on loop spine + ESTOP drill** · P5: **A–D ×
   Azure+GCP, L, W-2, N**.
6. Observability verification (§17 checklist on every new/changed path).
7. GitNexus impact verification: fresh index, per-row findings recorded, `detect-changes` clean
   against expected scope.
8. Architecture boundary audit: full-diff review against the phase's MUST-NOT list (§25) — the
   P0 §19 hunk-by-hunk audit is the template.
9. Rollback verification: the phase's lever (§10/§23) demonstrated in staging.
10. Migration ledger update: §2 statuses, §8/§9 registers, §19 baseline, §21/§22 registers.

## 14. Vertical slice requirement

Any cross-cutting change is one migration slice: **backend → DB → service → API → SSE/event →
frontend state → frontend UI → observability → tests.** Slices this binds in this plan:

- **Task/Run/Step & status machine** (P2.5/P3.6): new statuses land only with FE-04 handling +
  reconciler `EXECUTING_STATES` update + timeline artifact + gapless events + tests.
- **Model/provider** (P1): re-route lands only with ledger continuity + Langfuse generation
  continuity + `GET /models` shape freeze + error/retry payload freeze (FE-13) + eval gate.
- **Approvals** (P3): artifact enrichment lands only with FE-06/07/08/09 + gateways
  `approval_card`/`approval_buttons` + governance-stamp render + F-13 stream parity + tests.
- **Tool invocation** (P2.1): registry middleware lands only with observation events + policy
  verdict events + Langfuse tool spans + budget wiring.
- **Run events** (P2.5): table + emitters + FE-03 reconnect/Last-Event-ID + projections + replay.
- **Memory/context** (P2.3–2.6): gate + tiers land only with observable gate events + proposal
  review UI path (module proposals pattern) + scenarios P/Q.

Leaving a consumer knowingly broken for a later phase is forbidden unless the compatibility
layer is documented in §9 with an owner and removal condition.

## 15. Behavioral parity register

Parity means **behavior**, not API compatibility. Every replacement must hold these:

| ID | Invariant | Old behavior (evidence) | Expected under new implementation | Known intentional difference | Test | Status |
|---|---|---|---|---|---|---|
| BP-01 | Approval semantics | single-user HITL; `initiator == approver` valid in every env (four-eyes removed entirely, P1 entry gate — pinned by `test_initiator_may_approve_their_own_run` + `test_four_eyes_concept_is_removed`); durable cross-process interrupt; click-time re-check; in-flight lock | identical; artifact grows (additive) | stale-approval >24h re-validation **added** at P3.6 (06 §8.4 — an old approval is not a blank check) | approval tests; scenarios S/T | HOLDING |
| BP-02 | Plan guard | `check_plan_actions` re-asserted at choke-point + per-step; create may not delete | same predicate at engine compile + step boundaries | none | `test_safety_invariants` (unmodified, rule one) | HOLDING |
| BP-03 | Error semantics | provider failure kinds + `suggest_retry` + `failure_message`; failures never crash the run | same taxonomy via `llm/errors.py`; failed tools become observations (superset) | L3: errors additionally enter model context (P2) | D1 pin; scenario F | HOLDING |
| BP-04 | Resource identity | `resources` rows + `state_workspace` isolation + inventory provenance | unchanged; packs read the same inventory | none | inventory tests | HOLDING |
| BP-05 | Terraform safety boundary | 3 post-approval call sites; `-var` only; no LLM HCL; catalog-only templates (`validate_dag`) | engine executors preserve all four properties | none — constitutional | `test_safety_invariants`; modseed tiers | HOLDING |
| BP-06 | Destroy gating | `impact_of` world-model check before destroy (`cloudops.py:1105-1107`) | same check behind `WorldModel` interface | none until ADR-06 gate | pin before P4.2 dissolution of cloudops.py | **PIN NEEDED before P4.2** |
| BP-07 | Idempotency | `tf-exec` / `loop-step` Redis claims; per-step idempotency keys | engine steps carry idempotency keys (06 §8.1); same claim semantics | key naming may change **with migration note** | `test_idempotency` | HOLDING |
| BP-08 | Tenancy | strict org scoping at routes, approval core, choke-point, gateways | middleware `tenancy_scope` first in tool chain (05 §3) | none | `test_tenancy` (599 lines, unmodified) | HOLDING |
| BP-09 | Redaction | every egress + Langfuse payloads | + `run_events` payloads redacted at write (extension, 09) | additive surface | `test_redaction`; 10 §0 inv. 1 | HOLDING |
| BP-10 | Run correlation | trace_id == run_id everywhere | unchanged; run_events keyed by run_id | none | `test_traces_tree` | HOLDING |
| BP-11 | Honest partials | `_partial_outcome` lists not-attempted work | budget/stuck halts produce honest partials (04 §5) | superset | scenario R; 10 §0 inv. 5 | HOLDING |
| BP-12 | Cancel at boundaries | cooperative cancel flag honored at step boundaries, never mid-apply | kernel iteration boundaries + engine step boundaries | superset | exec-loop tests | HOLDING |
| BP-13 | Investigation read-only | denylist + freeze + MAX_CALLS=8 | registry unchanged; kernel budget stricter-wins | none | read-only violation tests | HOLDING |
| BP-14 | Status machine honesty | interim machine, no `applying` reads, `ended_at` on all terminal transitions | full machine, single writer per literal | new states additive | D5 pins; P3.6 suite | HOLDING |

## 16. Runtime verification (smoke tests)

Run after every significant migration (exact set derived from affected components; this is the
floor):

| ID | Smoke | How (today) |
|---|---|---|
| SM-01 | Backend startup, all roles | boot with `AEGISOPS_ROLE=all`, then `api`+`worker` pair; assert startup refusals still fire (memory bus non-local; Redis unreachable non-local) |
| SM-02 | Frontend startup | `next build` + boot against local API; login; composer enabled |
| SM-03 | API health | `GET /healthz` (includes governance stamp), `GET /readyz` |
| SM-04 | SSE | `POST /chat` streams `run→step→token→done`; `GET /chat/stream/{run_id}` reattaches |
| SM-05 | DB | `alembic upgrade head` on dev DB + representative query per new table |
| SM-06 | Redis | event-bus round-trip (`test_event_bus_redis` subset); heartbeat visible |
| SM-07 | Langfuse | `assert_project` passes; one run produces a trace with trace_id == run_id |
| SM-08 | Metrics | `GET /metrics` with bearer; ledger + approval-wait counters present |
| SM-09 | Representative read op | CloudOps read path (e.g. list/inspect flow) end-to-end with evidence in timeline |
| SM-10 | Representative approval op | plan → interrupt → approve → execute in `plan` mode against the `demo-null` template (no cloud creds needed) → verify → finalize |
| SM-11 | Eval gate | `python -m evals.gate` exit 0 + `--self-test` exit 0 |
| SM-12 | Ledger | one generate + one stream + one embed call produce `llm_usage` rows (or spill entries) |

## 17. Observability control

For every execution-path migration, verify preservation of — and record the result in the §2
row: `run_id` · `task_id` (where applicable; NULL until P3 by design) · `trace_id` (== run_id) ·
structured events (SSE today; `run_events` from P2.5 — gapless) · logs (structured, correlation
ids) · metrics (13 present; additions per phase) · latency (step spans via `timing.py`) · token
usage + cost (`llm_usage` rows on success **and** error paths, embeddings included) · error
information (taxonomy kinds; observation records) · approval events (requested/resolved +
stamp) · verification evidence (EvidenceCards from P3.3).

A new path is not "fully observable" until: Langfuse trace present · ledger rows written ·
metrics incremented · SSE/run_events emitted · redaction verified on the new payloads. Langfuse
degradation must never take the ledger down with it (ADR-08 split, verified at P0).

## 18. Security control

Every migration checks, before merge (§24 step 9 uses this list):

| Control | Current enforcement | Migration rule |
|---|---|---|
| Authorization | Keycloak OIDC + `security/deps.py` + RBAC | no new endpoint or tool path without the same dependency stack |
| Tenancy | strict mode; org scoping everywhere | `tenancy_scope` is stage 1 of the P2 middleware order — never reordered |
| Approval policy | single-user HITL (initiator == approver; no second-approver concept); durable interrupts | no mutation path may bypass the interrupt; AUTONOMOUS only via 08 decision 6 + PRE_APPROVED tier |
| Secret redaction | `security/redaction.py` on every egress + Langfuse | extends to `run_events` writes (BP-09) |
| Credential boundaries | global set (F-20, accepted until P5.3); 5 sandbox creds allowlisted by exact path; step-up reveal one-shot | no new code may print/log credentials; broker work only at P5.3 |
| Tool policy | read-only docstring contract + investigation denylist + registration freeze | P2 registry: 20-marker denylist at registration; effect≠policy taxonomies (05 §2) |
| Terraform mutation boundary | 3 call sites, post-approval, idempotency-guarded, catalog-only | count may not grow; new sites only inside `engine/executors/` behind compile/approve gate |
| Audit trail | `audit_log` + immutable approvals + Langfuse | every new decision point (policy verdict, deviation, budget halt) writes an auditable record |
| Secret scanning | gitleaks v8.24.3 pinned, CI + pre-commit, `--redact` | never weakened; new allowlist entries require operator classification |

No new path may bypass existing governance. If a migration cannot preserve a row above, it
stops and lands in §22.

## 19. Regression baseline

**Accepted P0 baseline (authoritative):**

| Metric | Value |
|---|---|
| Tests | **998** |
| Passed | **786** |
| Failed | **53** — all pre-existing environment failures (terraform providers absent on this machine: modseed/ingress/safety/scanner/rbac tiers) |
| Skipped | **159** |
| Errors | **0** |

Post-P0 full regression (reconciled, P0 §17): 1040 tests — 826 passed / 54 failed / 160 skipped;
+42 P0 tests; 52/53 baseline failures unchanged; 1 baseline flake now passes; 2 new failures
root-caused and closed (stale healthz assertion; interrupted-pip env damage). Zero unexplained
regressions.

Rules:
- The 53 environment failures are **baseline** — do not chase them, do not "fix" unrelated code
  to make them disappear. CI with terraform providers is the real signal for that tier.
- Every new failure in P1+ is classified **PRE-EXISTING** or **INTRODUCED_BY_PHASE** before the
  phase proceeds; unexplained = INTRODUCED_BY_PHASE until proven otherwise.
- This section is updated only when a phase proves a failure's root cause (either direction).
- Working-tree note: P0 work is **uncommitted** at HEAD `9fa6d83` (33 M / 25 A staged / 3 D
  staged / 18 untracked). Items to keep out of the P0 commit: `.claude/settings.local.json`
  churn; staged `gcp-gcs/.terraform.lock.hcl` (verify intent). See R-13.

## 20. Risk / issue register

| ID | Risk/Issue | Phase | Impact | Prob | Mitigation | Owner | Status | Exit condition |
|---|---|---|---|---|---|---|---|---|
| R-01 | Silent SSE/event contract breakage (FE silently drops unknown/changed frames) | all | HIGH | M | §5 additive-only rule; `test_sse_contract` extended per change | backend | OPEN | versioned event contract (P2.5) |
| R-02 | Frontend incompatibility via untyped payloads (`any` boundaries, presentation fields) | all | HIGH | M | §4 matrix + verification rule; typed responses per slice | frontend | OPEN | typed boundaries per slice |
| R-03 | Migration 0010 never applied to a live PostgreSQL (verified column-for-column only) | P1 entry | MED | M | applied to the dev DB 2026-08-10; schema/index/constraint verified; live write/read/idempotency proven | backend | **CLOSED (2026-08-10)** | SM-05 green on dev DB ✅ |
| R-04 | run_events dual-write divergence from checkpointer state | P2.5–P4 | HIGH | M | ADR-16 one-owner-each; replay drills; checkpointer stays resume authority | harness | OPEN | ADR-04 gate data |
| R-05 | Duplicate execution if idempotency keys don't survive engine migration | P3 | CRITICAL | L | BP-07 pin; kill -9 drills assert no double-apply | engine | OPEN | P3 exit demo |
| R-06 | Lost events (Redis stream trim vs durable record) | P2.5 | MED | M | `run_events` is the record; stream is live-feed only (maxlen 2000 today) | harness | OPEN | gapless-seq invariant in CI |
| R-07 | Observability gaps on new paths (new loop iterations unobserved) | P2+ | MED | M | §17 checklist is a merge gate | all | OPEN | per-phase §13.6 |
| R-08 | Performance regression from provider re-route (thread-offload, timeouts) | P1.3 | MED | L | canary + eval on same provider before second provider binds (07 risk #4); latency metric watch | llm | OPEN | P1 exit |
| R-09 | Hidden dynamic dependencies (LangGraph dispatch, MPP runtime registration, contextvars, monkeypatch seams) | all | HIGH | M | §7.3 rule 1 known-invisible list; grep cross-verification mandatory | all | OPEN | standing |
| R-10 | Dead-code misclassification (precedent: `ended_at`) | all | MED | M | §8 gates; PROVEN_DEAD bar; nothing removed opportunistically | all | OPEN | standing |
| R-11 | Rollback failure (lever untested until needed) | all | HIGH | L | 07 rule three: rollback demonstrated in staging per phase (§13.9) | phase owner | OPEN | per-phase |
| R-12 | Cloud parity drift (AWS-shaped harness) | P2–P5 | HIGH | M | per-row parity gate (03 §3.4); zero cloud literals in harness (CI) | packs | OPEN | P5.1 parity gate |
| R-13 | Accepted P0 work sits uncommitted — any tree accident loses it; staged unrelated files could contaminate the commit | now | HIGH | M | committed at the P1 entry gate (2026-08-10) in two commits: pure-P0 files, then the four-eyes removal + gate updates carrying the six mixed P0+gate files (itemized in both commit messages); operator artifacts (screenshots, gate-out images, `gcp-gcs/.terraform.lock.hcl`, `GAP_ANALYSIS.md`) deliberately unstaged and left untracked | operator | **CLOSED (2026-08-10)** | P0 committed ✅ |
| R-14 | GitNexus index instability (FTS corruption observed 2026-08-10) masks blast radius | all | MED | M | §7.1 rebuild procedure; index freshness is a §12 entry condition; grep is the floor-verifier | all | MITIGATED | rebuilt clean; recheck per phase |
| R-15 | 53 masked env failures hide real regressions in the terraform tier on this machine | all | MED | M | CI with providers is authoritative for that tier; local runs classify against §19 | CI | OPEN | standing |
| R-16 | Contract-owner ambiguity: run-status literals written by 3 modules today | P3.6 | MED | M | §3 Run row: single-writer-per-literal adopted at P3.6 | control plane | OPEN | P3.6 |

## 21. Deferred work register

Work discovered but belonging to a later phase. **Do not implement opportunistically.**

| ID | Item | Reason deferred | Target phase | Dependency | Risk | Acceptance condition |
|---|---|---|---|---|---|---|
| DEF-01 | `llm_usage.task_id` / `prompt_version` population | Task concept (P3) / prompt registry (P2.8) don't exist yet | P2.8 / P3 | prompt_registry, Task | LOW | columns populated; chargeback queries use them |
| DEF-02 | `gitleaks protect --staged` deprecated-alias migration | works at pinned v8.24.3 | next gitleaks rev bump | version bump | LOW | pre-commit green on new verb |
| DEF-03 | `judge_dataset.jsonl` depth (2 cases) | adequate P0 skeleton, thin quality signal | P1 (grows with provider evals) / P4.7 | eval expansion | MED | judge dataset covers P1 purposes |
| DEF-04 | GitNexus VECTOR extension install | needs network + `GITNEXUS_LBUG_EXTENSION_INSTALL=auto` | any maintenance window | network | LOW | semantic query uses real index |
| DEF-05 | F-13 approval-stream event parity | belongs to approval vertical slice | P2.5/P3 (§14) | FE slice | MED | FE-02 verified |
| DEF-06 | Governance stamp rendering in UI | belongs to approval artifact slice | P3 (FE-06/FE-09) | artifact enrichment | MED | approvers see stamped flags |
| DEF-07 | FE reconnect / Last-Event-ID resume | belongs to durability slice | P2.5 (FE-03) | run_events | MED | scenario O read-variant incl. UI |
| DEF-08 | F-14 DevOps PR-first flow + registry inspection | P5.2 owns DevOps completion | P5.2 | packs | MED | scenario L |
| DEF-09 | F-15 per-service PromQL | P2.9 owns SRE read expansion | P2.9 | kernel INV | MED | SRE triage on target-service signals |
| DEF-10 | F-19 cost guardrail into Settings | budget governor owns cost config | P2 (budgets) | harness budgets | LOW | guardrail discoverable + documented |
| DEF-11 | F-22 drift expansion | P5.7 | P5.7 | reconciler schedule | LOW | drift beyond aws-ec2 |
| DEF-12 | Grafana chart gap (7 uncharted metrics) | P5.6 dashboards | P5.6 | ADR-10 | LOW | dashboards adopted or Grafana removed |
| DEF-13 | Frontend approval state per-run (global singleton today) | multi-run concurrency slice | P3 (FE-08) | status machine | MED | concurrent runs approve correctly |
| DEF-14 | `.claude/settings.local.json` churn + staged `gcp-gcs/.terraform.lock.hcl` disposition | unrelated to any phase; keep out of P0 commit | operator, now | none | LOW | resolved at commit time (R-13) |

## 22. Architecture decision / contradiction register

Discovered contradictions between code, Redesign docs, diagrams, tests, or runtime behavior.
**None of these authorizes changing the architecture silently.**

| ID | Conflict | Sources | Impact | Decision required | Temporary workaround | Affected phase |
|---|---|---|---|---|---|---|
| C-01 | No normative ModelRequest/ModelResponse schema in the suite — 04 §4 delegates canonical types to the external Brainstorming `Agent_Harness.md` | 04 §4 vs Redesign self-containment | P1 adapters need a normative schema | **RESOLVED (2026-08-10):** canonical minimum contracts authored as **05 §11** — `CanonicalMessage`, `ModelRequest`, `ModelResponse`, `Usage`, `ServedBy`, `StreamEvent`, `ToolCall`, `ToolResult`, `ModelError`; owner `app/llm/types.py` at P1.1; additive-only until C-03 versioning decision | — | P1.1 |
| C-02 | No SSE event contract anywhere in Redesign/ while the frontend hard-depends on 11 exact event strings | 06 §8.2 (run_events only) vs `events.py`/`store.ts` | silent-breakage class R-01 | Author versioned SSE contract at P2.5 (owner: events.Emitter) | §5 additive-only freeze | P2.5 |
| C-03 | No per-contract schema-version fields (05 versions only PromptRef + plan hash) | 05 header claim "typed, versioned" vs its own contracts | evolution strategy undefined | Decide versioning mechanism (envelope field vs endpoint version) before first breaking need | additive-only everywhere | P1+ |
| C-04 | No typed Terraform execution result contract | 05/06 silence vs `TerraformRunner` returns being policy-gate inputs | P3 Step contract would inherit an implicit shape | Author with Step contract | freeze `planned_resources` shape (BP dependency) | P3.1 |
| C-05 | Doc 10 scenario Q asserts an `agent_gate(retrieve)` run-event kind absent from 06 §8.2's 17-kind enum | 10-Q vs 06 §8.2 | acceptance test would fail against spec'd enum | Reconcile enum (add `agent_gate` or re-express Q) at P2.3/2.5 design time | none needed yet | P2.3/2.5 |
| C-06 | IP-1 requires a machine-comparable `hypothesis` field on `assistant_turn` payloads; 06 §8.2 leaves payload untyped JSONB | 10 §4 vs 06 §8.2 | kernel must emit it or IP-1 can't be evaluated | Declare the field in the run-event payload schema at P2.1 | none | P2.1 |
| C-07 | 06 §8.2 specifies `llm_usage` month-partitioned; shipped migration `0010` creates an unpartitioned table (verified: zero `PARTITION` clauses) | 06 vs `0010_llm_usage.py` | repartitioning later = table rewrite | **RESOLVED (2026-08-10): stay unpartitioned.** Rationale: single-tenant dev posture, row volume orders of magnitude below partition benefit, primary read (`org_id, ts` chargeback) fully served by `ix_llm_usage_org_ts`, usage-row retention is off. **Partition triggers (any one):** (a) `llm_usage` approaches ~10M rows; (b) a retention/prune policy for usage rows is enabled (partition-drop beats DELETE); (c) chargeback query p95 degrades past agreed bounds. **Migration path when triggered:** create `RANGE (ts)` month-partitioned twin → backfill → swap in one transaction → note the PK must become `(id, ts)` on the partitioned table, so the spill-replay idempotency conflict target changes from `(id)` to `(id, ts)` — replayed rows carry identical `ts`, so dedupe semantics are preserved, but `usage_ledger._insert` and its pinning test must change in the same PR. 06 §8.2 annotated accordingly | indexes carry current volume | trigger-gated |
| C-08 | Flagship target-architecture SVG is hand-authored and not regenerated by the mermaid loop; README declares divergence from 02 §2.1 "a defect" | diagrams/README | doc drift risk | none — maintenance rule: check on every 02 §2.1 edit | — | all |
| C-09 | GitNexus cluster/module labels unreliable (`approval_buttons` → "Tests") | tool output vs filePath | misattributed impact reads | none — trust filePath; do not generate per-community skill files until fixed | §7.3 rule 5 | all |
| C-10 | Doc 01 counts "3 interrupt sites"; current code has 2 real `interrupt()` sites + 5 SSE-interrupt emit sites (the whole-DAG approval at `exec_loop.py:154-162` in 01 is an SSE event + approval-node flow at HEAD) | 01 §1C vs code | governance-stamp coverage reasoning must use the *code* count (P0 stamped both real sites) | Verify at P3 approval-artifact work; correct 01 if confirmed | code is authoritative | P3 |
| C-11 | CLAUDE.md / `gemini.py:3` historically claimed native tool-calling (aspirational — F-mislabel) | 01 §2.3 | expectation drift | already flagged in 01; P1.8 makes it true | — | P1.8 |

## 23. Rollback / checkpoint strategy

Prefer reversible migration over irreversible replacement — every high-risk migration defines,
**before it starts**:

| Phase | Checkpoint | Rollback trigger | Mechanism | Data implications | Frontend implications | Old-path availability | Verification after rollback |
|---|---|---|---|---|---|---|---|
| P1.3 dispatch re-route | git tag + eval-gate verdict pre-change | eval gate red, canary regression, latency breach | shim internals revert (byte-compatible by design) | none (ledger keeps writing) | none (shapes frozen) | Gemini singleton alive until end P1 | eval gate green on old path; SM-04/11/12 |
| P1.6–1.7 routing/bindings | binding rows snapshot | wrong-model dispatch, silent fallback detected | bindings revert to `models.yaml` defaults | binding rows are data | Settings UI shows defaults | yaml default path permanent | scenario V case 1 |
| P2.2 kernel read paths | flag off state | INV divergence from legacy read results | `harness_read_paths` off | run_events keep (additive) | none | hardcoded paths retained until parity | SM-09; legacy tests |
| P2.5 run_events | checkpointer authority unchanged | replay divergence | readers fall back to checkpointer | table additive, append-only | FE-03 degrades to today's behavior | checkpointer is the authority by default | resume drill via checkpointer |
| P3.1–3.2 engine | exec_loop module + flag retained | invariant suite failure, double-apply, compensation failure | `engine_exec` flag back to exec_loop | run_steps additive columns ignored by old path | plan/timeline render unchanged (projections) | exec_loop importable until removal gate | full P3 demo on old path; `test_exec_loop` |
| P3.6 status machine | literal audit snapshot | FE restore breakage, reconciler misclassification | stop writing new literals | old literals never removed | FE-04 handlers stay | old machine is a subset | store restore tests |
| P4.3 inversion | dark-flag off | eval parity break on any objective class | flag flip to graph spine | run_events continue on both | invisible (same contracts) | graph spine fully alive ≥1 quarter | IP-1..4 + scenario A on graph topology |
| P5.3 broker | per-org fallback registry | broker outage / mutation failures | org flag → global key | credential handles are ephemeral | none | global set until all orgs migrated | scenario W; mutation smoke per org |

DB rollbacks: alembic downgrade authored with every upgrade; destructive down-migrations are
never run against shared environments — roll forward instead.

## 24. Implementation checkpoints

After **every** significant migration, in order — the next migration does not begin until all
ten pass:

1. GitNexus impact analysis (fresh index; `-r AegisOps_AIOPS`; record in §2 row).
2. Repository-wide search for the §7.3 invisible-dependency classes.
3. Targeted tests (the row's Verification column).
4. Integration tests (Redis/DB/graph as affected).
5. Frontend tests if any §4 row touched.
6. Runtime smoke test (§16 subset derived from affected components).
7. Observability verification (§17 checklist).
8. `git diff` review against the phase's MUST-NOT list (§25) — hunk by hunk.
9. Security control pass (§18 table).
10. Migration ledger update (§2/§8/§9/§19/§20/§21/§22 as applicable).

## 25. Phase control targets and boundaries

### P1 — Provider layer (first target after this document)

Owns (07 §1): `app/llm` types/errors/stream events · `google_` adapter · `service.py` +
byte-compatible `agents/llm.py` shim + `purpose=` at 8 call sites · catalog + `models.yaml` +
capability registry · `anthropic_`/`openai_compat` adapters + recorded-fixture tests · router +
resilient executor · `model_bindings` + Settings UI · first native tool calling (router/extract
structured output, $0.01 opt-in canary) · import-linter contract (bans SDK imports outside
adapters; bans `langgraph.*` outside `harness/`).

**P1 MUST NOT introduce:** Agent Harness · Observe→Reason→Act loop · iterative reasoning ·
failed-tool-as-observation · subagents · new memory architecture · workflow engine ·
CloudOps/DevOps/SREOps migration · unrestricted autonomous execution — and must not touch:
`exec_loop.py` (beyond import shims) · `approval.py` semantics · graph topology · templates
catalog · tenancy/RBAC/redaction modules · frontend beyond the P1.7 Settings surface ·
invariant test files.

Exit (07): UI rebind `knowledge → claude-sonnet-5` zero-code · evals green on two providers ·
staged key-kill shows visible fallback badges · `grep -r "google.genai|anthropic|openai"
app/agents app/packs` → 0 · scenario V ×3 · §13 ten-point gate.

### P2 — Harness kernel on read paths

Owns: kernel loop + AgentSpec + ToolRegistry v2 middleware · INV loop (first callers
`sre._collect_telemetry`, `cloudops._read_path`) · retrieval gate · per-iteration context
reassembly · `run_events` + replay-resume · consolidation→proposals (`memory_items`) · subagent
spawn · prompt registry · SRE read-tool expansion · budgets/hooks/compaction/interrupt
integration with P1. **MUST NOT:** touch mutation paths (rule two) · implement P3 engine or P4
inversion · modify invariant suites.

### P3 — Durable execution / workflow engine

Owns: exec_loop→engine (Step contract, compile closures verbatim) · saga rollback · VerifyPlan/
EvidenceCard · day-2 registry+executor · K8s executor · change windows + full status machine ·
deviation taxonomy · real policy predicates (F-11) · `max_steps` 5→8 behind config. **MUST
NOT:** migrate CloudOps/DevOps/SREOps wholesale (that is P4) · retire the graph spine.

### P4 — Harness-first inversion

Owns: objective model + admission classification (graph router branch retired) · capability
packs (cloudops.py dissolves) · loop-as-spine (dark, eval-parity-gated; LangGraph reduced to
interrupt/checkpoint substrate) · planner/critic purposes · permission modes + ESTOP · eval
expansion. **AWS, Azure, GCP receive equal architectural treatment — AWS-specific code must not
become the intelligence architecture** (zero cloud literals in harness, CI-enforced). **MUST
NOT:** pull P5 work forward (broker, channels, parity completion).

### P5 — Production parity / hardening

Owns: three-cloud read/verify parity gate · DevOps completion (log download, rerun, PR-first) ·
credential brokering (per-org, short-lived, vault-backed — ADR-17) · Alertmanager→incident→
triage→gated remediation→postmortem pipeline · Slack/Teams transports · dashboards + the 7
uncharted metrics · drift expansion · offline model arena. **MUST NOT:** enable AUTONOMOUS mode
or pre-approved verbs without 08 decision 6 sign-off.

## 26. Change control

Every significant implementation change answers, in the PR description or the §2 row —
**if any answer is missing, STOP and investigate before implementing:**

1. **WHY?** — which phase item / defect / scenario requires it.
2. **WHAT?** — symbols + files + contracts touched.
3. **WHO CONSUMES IT?** — from §2/§3/§4 rows + §7 verification.
4. **WHAT BREAKS?** — blast radius (GitNexus + grep), worst case named.
5. **HOW IS IT COMPATIBLE?** — adapter/shim/additive strategy per §5/§9.
6. **HOW IS IT TESTED?** — unit/integration/contract/behavioral pins.
7. **HOW IS IT OBSERVED?** — §17 checklist result.
8. **HOW IS IT ROLLED BACK?** — lever per §23, demonstrated where high-risk.
9. **WHEN CAN THE OLD CODE BE REMOVED?** — removal gate per §8/§9.

## 27. Document maintenance

- Update **after every significant migration** (§24 step 10 makes it mechanical). A stale
  control plan is itself a BLOCKED condition for the next phase entry.
- Must always reflect: current migration state (§2) · transitional components (§9) · removed
  components (§8) · deferred work (§21) · risks (§20) · evidence (row citations) · acceptance
  gates (§12/§13).
- Section §19 changes only with proven root causes. Section §22 rows close only with an owner
  decision recorded in 08 (new ADR or amendment), never by silent code drift.
- On acceptance of each phase: add `Redesign/implementations/P<n> Implementation Report.md`
  (P0's report is the template) and reconcile this ledger against it.

---

## 28. P1 Entry Gate record (2026-08-10)

Executed per the operator's FINAL P1 ENTRY GATE directive. Verdict: **P1 ENTRY READY.**

### 28.1 Four-eyes removal (operator directive: the concept does not exist)

**The approval model is single-user human-in-the-loop: the user who initiated the operational
request is the human who reviews and approves or rejects the proposed plan. `initiator ==
approver` is valid in every environment. There is no second approver, no dual-approval workflow,
no four-eyes flag, state, UI, or tests.**

Repository-wide audit: ~185 occurrences across 48 files, every one classified
(A remove / B historical / C test-fixture / D doc-update / E false-positive). Removed/changed:

- **Enforcement gate** deleted: `api/chat.py` (the production self-approval 403 branch).
- **Setting deleted**: `settings.py aegisops_four_eyes_for_production` (field + env binding);
  `.env` + `.env.example` entries removed.
- **Governance stamp**: `four_eyes_for_production` key removed; `approval_model` hard-pins
  `"hitl"` (verified live on `/healthz` post-rebuild).
- **Gateway plumbing** deleted: `notify.approval_pending` initiator-exclusion branch (+ now-dead
  `env`/`initiator_user_id` params, caller updated); `identity.notifiable_approvers
  exclude_user_id` param + filter; Telegram `posture()` banner copy; docstrings/comments in
  `gateways/__init__.py`, `driver.py`, `db/models.py`, frontend `store.ts`/`data.ts`.
- **Tests**: `test_tenancy.test_four_eyes_blocks_prod_self_approval` →
  **`test_initiator_may_approve_their_own_run`** (the inverse pin: initiator's own approval
  passes every authz gate in Production AND Staging — a 403 would mean a second-approver policy
  crept back); `test_p0_governance_worker_redis` → **`test_four_eyes_concept_is_removed`**
  (negative pins: no Settings field, no stamp key) + `approval_model == "hitl"` pin;
  `test_gw1_approvals` four-eyes click-time + push-exclusion tests deleted, replaced by
  `test_push_list_never_excludes_anyone`; `test_gw1_telegram_adapter` banner assertion inverted
  (`"four-eyes" not in text`); frontend `store.test.ts` denial-visibility test re-fixtured
  (generic 4xx); e2e `stab-p03-approve.spec.ts` scenario 1 inverted to **same-user REJECT closes
  honestly** (Scenario B live shape), scenario 2 renamed two-user RBAC flow.
- **Docs corrected** (concept now stated as non-existent): 00 §2/§7 · 01 (4 spots + mermaid) ·
  02 (2 mermaid) · 03 §3.4 · 04 §8.4/§8.5 · 06 §8.2 (C-07 annotation) · 07 P0.5 (historical
  annotation) · 08 decision 5 (superseding entry) · 09 R10/security/phase rows · 10 A/B-C-D
  rows · README (4 spots) · this document · diagrams: 3 `.mmd` sources + 2 regenerated SVGs +
  the hand-authored flagship SVG (2 text nodes) — `grep four.?eyes` over all SVGs = 0.
- **DB**: no second-approver field exists (`approvals` = single decision row); nothing to
  migrate. `runs.initiated_by`/`runs.env` **stay** — they serve S1 credential-reveal authz,
  PR-3 cancel authz, and the audit trail. Applied migration `0004`'s docstring remains as a
  historical record.
- **Historical references intentionally retained** (clearly historical): P0 report ·
  `PROGRESS.md` · `STAB_MATRIX.md` · `FIX.md` · `docs/analysis/` + `docs/fix/` (pre-redesign
  analyses) · `Brainstorming/` (superseded by Redesign/) · `AEGISOPS_TARGET_ARCHITECTURE.md` ·
  `00_PRODUCTION_MASTER_PROMPT.md` · migration `0004` docstring · GitNexus/waku gap audits.

### 28.2 Verification evidence

| Check | Result |
|---|---|
| Migration 0010 on dev DB | applied (`0009 → 0010`); schema column-for-column; both indexes + PK; live write/read; double-insert counts once |
| Containerized live tier (Linux `api-test`, live PG/Redis/Neo4j) | `test_tenancy` + `test_p0_ledger` + `test_p0_governance_worker_redis` + `test_gw1_approvals`: **65 passed, 0 failed, 0 skipped** — includes the new initiator-self-approval pin on Production and Staging |
| Local suites (Windows) | governance/gw1/safety-invariants: 142 passed + 2 failures **pre-existing in the accepted baseline** (TestStateIsolation — no terraform providers; present in both baseline artifacts); tenancy/health/exec-loop/SSE-contract/p0-defects: 23 passed, 20 live-tier skips (env-only: Windows ProactorEventLoop vs psycopg async — the same tests run green in the container) |
| Frontend | Vitest **45/45**; e2e spec updated (on-demand live spec, not CI) |
| Eval gate | `python -m evals.gate` → GATE OPEN 10/10, exit 0; `--self-test` 1/1, exit 0 |
| Security | `test_p0_security_preflight` + `test_redaction`: 20 passed (tracked-path scan green, allowlist semantics intact, unknown credentials still fail); sandbox credentials untouched per operator classification |
| Runtime smoke | api image rebuilt from the edited tree; `/readyz` 200 (DB+Redis+Neo4j+graph init); `/healthz` governance stamp = `approval_model: "hitl"`, no four-eyes key; OIDC login (seeded user) → `GET /models` (live catalog) + `GET /overview` (org-scoped read) green; Langfuse absent → tracing degraded, startup unaffected (ADR-08 contract) |
| GitNexus | pre-edit `impact` on every touched symbol (recorded §7.1); post-edit re-analyze 8,236 nodes / 15,946 edges |
| Boundary | no P1–P5 implementation, no harness/loop/engine code, LangGraph untouched, CloudOps/DevOps/SREOps untouched, no broad dead-code cleanup |

### 28.3 Baseline disposition

The accepted baseline (§19) stands: 998/786/53/159/0. Failures observed during this gate:
2 (TestStateIsolation ×2) — classification **PRE_EXISTING / ENVIRONMENT_ONLY** (present in
`baseline-pytest.txt` and `final-regression.txt`). Zero failures INTRODUCED_BY_CURRENT_OPERATION.
The live-DB skip tier is Windows-environment-only; it runs green in the api-test container
(65/65).

### 28.4 P1 entry checklist

P0 accepted ✅ · P0 changes isolated & committed (two-commit structure; mixed files itemized in
messages) ✅ · 0010 applied to dev DB ✅ · llm_usage verified ✅ · C-01 resolved (05 §11) ✅ ·
C-07 resolved (§22) ✅ · four-eyes completely removed ✅ · HITL intact ✅ · same-user approval
passes every gate (live tier) ✅ · reject path pinned (unit + e2e shape) ✅ · no second-approver
path/config/UI/tests remain ✅ · GitNexus refreshed ✅ · blast radius verified ✅ · baseline
preserved ✅ · eval gate green ✅ · security scan green ✅ · runtime smoke green ✅ ·
observability intact ✅ · no P2–P5 code ✅ · architecture boundary passes ✅

**P1 may begin at P1.1 (canonical model contracts per 05 §11).**
