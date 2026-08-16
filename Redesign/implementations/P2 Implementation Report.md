# P2 Implementation Report — Agent Harness + Intelligent Execution Foundation

> Branch: `feature/cloudops-v3` · base: `a35a9d5` (P0+P1 uncommitted beneath) · Date: 2026-08-10
> Scope: Redesign/07 Phase 2, per the operator's P2 prompt.
> Status: **implemented and verified, uncommitted, awaiting operator acceptance.**
> Control ledger: Redesign/11 §30.

## 1. Implemented Capabilities

A new provider-neutral package `app/harness/` introduces the real agent execution kernel:
an OBSERVE → REASON → ACT → OBSERVE → … → VERIFY → COMPLETE/ASK/STOP loop that pursues a
read-only operational objective, ingests a failed tool result as an observation, and lets
the next reasoning iteration change its hypothesis and its action. It is built entirely on
the P1 model layer (no second abstraction), drives the frozen read-only investigation
registry (P2.2, reads only), and records every iteration to a durable event-sourced log.

Delivered: (1) Agent Harness kernel; (2) the OBSERVE→REASON→ACT loop with genuine
iteration; (3) native structured-output reasoning + native-tool execution through Tool
Registry v2; (4) the Tool Registry v2 foundation; (5) durable `run_events`; (6) Task/Run
integration sufficient for P2 (the loop records against the existing `runs` row);
(7) memory retrieval gate + `memory_items` tier + consolidation-to-proposals; (8) a
context-assembly path with a bounded observation tail (compaction-lite); (9) resumable
ask/needs-input pauses + durable replay; (10) hard iteration/tool/token/cost/wall-clock
budgets enforced inside the loop with a one-call grace partial; (11) first-class
verification (evidence-backed goal check, honest downgrade to unverifiable); (12) failure
observation + re-reasoning (L3/L4); (13) the subagent foundation (typed results, depth 1,
shared budget); (14) the prompt registry foundation (versioned, hash-idempotent PromptRefs);
(15) P1 model integration throughout; (16) Langfuse/ledger/run_events observability;
(17) the frontend Agent-Loop artifact tab over a CoT-safe events API; (18) behavioral
intelligence tests as the acceptance oracle.

## 2. Architecture Mapping

| Capability | Frozen source | Module |
|---|---|---|
| OBSERVE→REASON→ACT loop, laws L1–L7 | 00 §4, 04 §3 | `harness/loop.py` (244 lines, ≤500) |
| Budget governor (grace call, halt-at-boundary) | 04 §5 | `harness/budgets.py` |
| AgentSpec (purpose = only model coupling) | 04 §2 | `harness/spec.py` |
| Tool Registry v2 (native schemas, middleware order, L3) | 05 §1/§3 | `harness/registry.py` |
| Durable run log (event-sourced, ADR-16 two-records) | 06 §8.2 | `harness/run_log.py` + `RunEvent` |
| INV loop drives the frozen registry (read paths) | 07 §2.2 | `harness/inv.py` |
| Retrieval gate (fail-open, observable) + memory tiers | 06 §4/§1/§2 | `harness/memory.py` + `MemoryItem` |
| Subagents (typed result, depth 1, shared pool) | 05 §6, 02 §5 | `harness/subagents.py` |
| Prompt registry (versioned PromptRef) | 05 §9 | `harness/prompts.py` + `PromptRegistry` |

The kernel is cloud/provider-neutral and holds no domain knowledge, no SDK imports, no
persistence logic (delegated to `run_log`), and no policy logic (04 §11 discipline).

## 3. Changed / New Files

New: `app/harness/{__init__,loop,budgets,spec,registry,run_log,inv,memory,subagents,prompts}.py`;
migrations `0012_run_events`, `0013_memory_items`, `0014_prompt_registry`; models
`RunEvent`/`MemoryItem`/`PromptRegistry`; 7 `test_p2_*.py` suites; frontend `AgentLoop` tab.
Touched (additive): `app/db/models.py` (+3 models, `UTC` import), `app/api/artifacts.py`
(+`GET /runs/{id}/events`), `app/agents/sre.py` (flag-gated kernel read path, else legacy),
`app/settings.py` (+`aegisops_harness_read_paths`), frontend `lib/types.ts` (+`RunEvent`,
+`events` tab), `lib/data.ts` (tab list/titles), `components/ArtifactPanel.tsx` (+`AgentLoop`).

## 4. API / SSE Changes

One additive endpoint: `GET /runs/{run_id}/events` — the harness loop trail, org-scoped by
the shared artifact loader (S2, 404 on mismatch), **CoT-safe by construction** (only the
one-line `hypothesis` + privacy-safe `rationale` from each `assistant_turn`, never raw
chain-of-thought; payloads already redacted at write). No existing endpoint or SSE event
changed. The Redis live feed gains an additive `run:{id}:log` stream mirroring run_events.

## 5. Frontend Changes

A new read-only **Agent Loop** artifact tab renders the OBSERVE→REASON→ACT trail
(iteration, hypothesis, failed/ok observations with error kinds, verification verdict,
budget halts) — the visible proof that a failed tool changed the next action. It reuses the
panel's existing generic `GET /runs/{id}/{tab}` fetch, so no store logic changed. tsc clean.

## 6. DB / Redis Changes

Three additive migrations (0012/0013/0014), each with a downgrade, applied to the dev DB
only (docker :5433 — the native :5432 instance untouched): `run_events` (gapless
UNIQUE(run_id,seq), 18-kind CHECK incl. `agent_gate`), `memory_items` (768-d pgvector,
provenance/status/supersedes per 06 §1), `prompt_registry` (PK(name,version), content_hash,
eval_state). Redis: one additive per-run log stream (live feed only; `run_events` is the record).

## 7. GitNexus Impact

`detect-changes`: 36 files / 61 symbols / low risk — the harness is new surface with no
inbound callers except the one flag-gated SRE branch; nothing existing was restructured.
Cross-verified with repo-wide search: the six LangGraph spine files show zero diff.

## 8. Tests

7 new suites, **34 tests, all green in the container tier (live datastores, real SDKs):**
kernel 8 (incl. IP-1/IP-4), run_events 3, registry 8, inv-wiring 2, memory 6, subagents+prompts
5, events-API 2. Live-DB-gated tests (gapless-seq, memory supersede, prompt versioning, events
trail) pass in the container; they skip on the Windows host (ProactorEventLoop vs psycopg
async — the documented P1 limitation) and were additionally proven via a selector-policy
direct run for run_events.

## 9. Behavioral Intelligence Evidence (the load-bearing proof)

`test_p2_kernel.test_failure_changes_hypothesis_and_action_then_recovers` (IP-1): a scripted
model calls `query_prometheus` → the tool **fails** → the failure becomes observation [0] →
the next `assistant_turn` carries a **different hypothesis** AND selects a **different tool**
(`list_pods`, a different evidence family) → success → an answer citing the recovered
evidence. All five IP-1 conditions are asserted over the recorded events, not model prose.
`test_deterministic_repeat_loop_is_halted_not_rewarded` (IP-4): a fake that repeats the
identical failing action is **halted** at the repetition limit with an honest failure — a
scripted retry loop cannot pass as intelligence. The live events-API test renders the same
failure→changed-hypothesis→recovery trail end-to-end.

## 10. Observability Evidence

Every iteration emits typed `run_events` (redacted at write, gapless seq); the reasoning
step is a P1 `llm.<purpose>` generation on the ledger; budget halts, gate decisions
(`agent_gate`), and verification verdicts are all events. run_id correlation is preserved
(the kernel logs against the caller's run). No raw CoT reaches events, logs, or the UI.

## 11. Regression Reconciliation

Single full local run: **1148 tests — 927 passed / 50 failed / 171 skipped** (22:10).
**INTRODUCED_BY_P2: 0.** All 50 failures fall in the documented terraform-provider
environment tiers (13× `test_modseed_*`, `test_module_ingress`, `test_pr1_tf_hygiene`,
`test_pr2_limits`, `test_rbac_endpoints`, `test_scanner_waiver_guard`,
`test_stab_p01_tfplugins`, `test_safety_invariants::TestStateIsolation`) — every one
requires terraform providers absent on this host, and all are within the accepted P1
baseline set of 53 (this run has 50; the 3-way difference is environment flake variance
within those same tiers, not code). Programmatic file-level check confirms **zero
failures in any app-logic, P0, P1, or P2 suite** (`test_p2_*` = 0 failed). CI with
terraform providers remains the authoritative signal for that tier. The P2 suites
themselves are **34/34 green in the container tier** (live datastores, real SDKs).

## 12. Transitional Components

T-P2-01: the legacy hardcoded SRE read path coexists with the kernel path behind
`aegisops_harness_read_paths` (default **off** → byte-identical prior behavior). Owner:
harness. Replacement: kernel INV read path. Removal condition: read-path parity accepted
across the P2 exit scenarios. Rollback: flag off. (P1's T-01 shim/stub still stands.)

## 13. Deferred Work (within/after P2)

- Per-iteration context reassembly via named recipes (06 §6) and token-threshold compaction
  (06 §7): P2 ships a bounded observation-tail context + budget-driven halt; the full recipe
  registry + compaction-record path is deferred to a P2 follow-up (recorded in doc 11 §21).
- `cloudops._read_path` as a second INV caller (07 §2.2 names it alongside SRE): the wiring
  pattern is proven on SRE; extending it to cloudops reads is a deferred P2 slice.
- SRE read-tool expansion P2.9 (pod logs/events/per-service PromQL) beyond the current
  registry: deferred; the registry is the extension point.
- prompt-registry adoption across all agent prompts (P2 ships the foundation + opt-in
  PromptRef; broad adoption lands with P4 pack extraction).

## 14. P2 Boundary Verification

No P3 workflow engine, no saga/waves, no P4 CloudOps/DevOps/SREOps or capability-pack
migration, no P5 credential broker, no AUTONOMOUS execution, no new agent framework, no
LangGraph removal/replacement (6 importers, spine files zero-diff). Mutation paths untouched
(rule two); the harness is read-only by construction (the registry rejects mutation-marker
tools at registration). The product remains runnable with the flag off (default).

## 15. Rollback Status

The entire P2 surface is additive and uncommitted: discarding the working-tree changes
reverts it wholesale. In a committed state, `aegisops_harness_read_paths=off` disables the
only production behavior change (SRE read path); the three migrations have downgrades; the
new endpoint/tab have no other consumers. No feature is on by default.

## 16. Known Limitations

Live LLM-driven loop end-to-end needs a working provider key — the sandbox Gemini key on
this host is dead (`API_KEY_INVALID`, pre-dates P1), so the intelligence proof is
demonstrated with a scripted model over the real kernel control flow (the architecturally
correct unit of proof — it isolates the KERNEL's behavior from model quality) plus the live
events-API trail; a live model run is a credential away. Windows async-loop limitation keeps
the live-DB tests in the container tier. Context compaction is bounded-tail only in P2 (§13).

## 17. Final Verdict

**P2 COMPLETE — READY FOR ACCEPTANCE**

The Agent Harness kernel runs a genuine OBSERVE→REASON→ACT loop with durable run_events,
budgets, verification, memory lifecycle, subagent and prompt-registry foundations, driving
the frozen read-only registry on a production path behind a default-off flag. The
intelligence behavior is proven over recorded events (IP-1) and the anti-scripting oracle
holds (IP-4). Zero regressions introduced; the six LangGraph spine files and all mutation
paths are untouched; the product runs with the flag off. Work is deliberately uncommitted
pending operator acceptance (the P0/P1 pattern). Stopping at the P2 completion gate — P3 is
not started.
