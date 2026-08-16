# P1 Implementation Report — Multi-Provider LLM Substrate

> Branch: `feature/cloudops-v3` · base: `a35a9d5` · Date: 2026-08-10
> Scope: Redesign/07 P1.1–P1.9, per the operator's P1 prompt (provider-neutral, multi-provider real, frontend+backend one product).
> Status: **implemented and verified, uncommitted, awaiting operator acceptance** (the P0 pattern).
> Control ledger: Redesign/11 §29 + updated rows L-01…L-06, T-01/T-02, DB table, DEF-15/16.

## 1. Executive Summary

AegisOps now has the approved provider-neutral model substrate: canonical contracts (05 §11) → LLM service → catalog/bindings → deterministic router → resilient executor → three wire-family adapters (google, anthropic, openai_compat), with **OpenRouter as a pure-configuration provider over the openai_compat family — the "new provider = config + adapter + capability declaration" proof executed inside P1 itself.** The Gemini SDK singleton and the validate-only seam are gone; the google-genai/anthropic/openai SDKs import **only** inside `app/llm/adapters/` (AST-enforced + import-linter + CI). Every model call — success, stream, embedding, error — records identically on the `llm_usage` ledger (served vs requested model, provider, purpose) and as a Langfuse `llm.<purpose>` generation. Existing behavior is preserved through a byte-compatible `agents/llm.py` shim (eval gate 10/10 before AND after the re-route), the frontend reflects the multi-provider backend (enabled-filtered menu, served-by badge, Settings → Model routing), and no P2+ concept was implemented: P1 routing is selection + resilience, never Observe→Reason→Act.

## 2. P1.1 — Canonical Contracts

`app/llm/types.py` + `errors.py` implement 05 §11 verbatim: `CanonicalMessage` (role-shape-validated), `ModelRequest` (purpose-validated, 120s default timeout), `ModelResponse`, `Usage` (five token kinds), `ServedBy` (honest serving + `fallback_hop`), `StreamEvent` (terminal contract: exactly one `done` after `usage`+`served_by`, XOR one `error`), `ToolCall` (canonical-JSON `args_hash`), `ToolResult` (failed ⇒ typed error), `RoutePlan`; `ModelError` with the 04 §4 taxonomy, `RETRIABLE`/`FAILOVER` sets, secret redaction at construction. Purposes: the 13-value 04 §4.3 enum; `GOVERNED_PURPOSES = {router, planner, loop.main, judge}`. No provider concept appears anywhere in the layer. Pins: `test_p1_contracts` 12/12 (construction, validation, JSON round-trip, invalid payloads, streaming shapes, errors, tool call/result).

## 3. P1.2 — Gemini Adapter

`adapters/google_.py` carries the retired singleton's behavior verbatim (async client, `system_instruction`, usage totals off the final stream chunk, 768-d pinned embeddings) plus the architecture's additions: canonical message mapping (incl. tool_use/tool_result round-trip), typed errors, timeouts, generation params, native FC + structured output. The retired `integrations/gemini.py` now holds ONLY the transitional stub: `GeminiError` + the `set_run_model` contextvar (consumers: three agent nodes, the shim, chat admission) — removal condition end of P2 (T-01). A live finding hardened the adapter: Google reports bad keys as HTTP 400 `API_KEY_INVALID` → mapped to `auth_permanent` (breaker-opening), fixture-pinned.

## 4. P1.3 — LLM Service / Compatibility

`app/llm/service.py` is the one entry point: `generate()`/`stream()` (separate — no `stream=` flag), `classify_json()` (schema-aware with the tolerant fenced-JSON parser as the net), `embed()`, `configured()` (the provider-neutral replacement for `gemini.enabled` guards), `extract_json` (byte-identical to the historical parser — the eval runner replays through it, rule zero). The service owns observability + accounting uniformly for every provider. `agents/llm.py` is now a **byte-compatible shim**: same signatures, same `GeminiError` semantics, same streaming-resilience policy (retry-transparently / truncate-cleanly / raise-after-exhaustion — the three behaviors re-pinned at the new seam in the migrated `test_stream_resilience`). `purpose=` threaded at all 8 call sites (`router`, 4× `extract`, `general`, `knowledge`, `sre.triage`). Consumers migrated in the same milestone: rag embeddings + retriever → `service.embed`, `evals/judge` → `service.generate(purpose="judge")`, chat admission + `GET /models` + integrations grid → catalog.

## 5. P1.4 — Model Catalog

`app/llm/config/models.yaml` (7 models, 4 providers, 13 purpose defaults) + `catalog.py`. Yaml = what CAN run; DB bindings = who runs what; boot cross-validation refuses startup on an invalid catalog in EVERY env. Rules enforced at load: every purpose bound; fallback chains validated or explicit `fallbacks: none`; governed purposes structurally fallback-free; capability requirements checked against defaults; wire families must have adapters. Provider config is data-driven (`settings_field`/`base_url`/`wire_family` in yaml — zero provider branches in code). Pins: `test_p1_catalog` 6/6 including five invalid-catalog refusal cases.

## 6. P1.5 — Provider Adapters

`anthropic_.py` (Messages API: system param, tool_use/tool_result blocks, mandatory max_tokens defaulted, 529→upstream_rate_limited, Retry-After honored, cache token kinds) and `openai_compat.py` (Chat Completions: JSON-string tool args encoded/parsed defensively, `stream_options.include_usage`, `response_format: json_schema`). **OpenRouter** is a catalog provider entry (`wire_family: openai_compat`, own key + fixed base_url) — added with zero adapter code. SDKs added to pyproject, imported lazily inside `_make_client()` (an uninstalled SDK is a typed call-time error, never an import-time crash). No stub adapters: 18 recorded-fixture contract tests exercise real mapping layers; real SDK imports verified in the Linux container (anthropic 0.121.0 / openai 2.53.0). The old validate-only seam `integrations/llm/` is **deleted**; its test rewritten against the catalog including the deliberate inversion of the old "all-gemini catalog" pin into a multi-provider pin.

## 7. P1.6 — Routing / Resilience

`router.py`: deterministic resolution pin → org binding → yaml default; governed purposes ignore user pins with a visible log; fallback hops to unconfigured providers are pruned at plan time; binding lookup is an injected resolver (no phantom table references — the audit lesson). `executor.py`: bounded same-binding retries (exp backoff + jitter, Retry-After honored), Redis breaker per binding (in-memory fallback; availability state, never a record), two-stage failover with `ServedBy.fallback_hop` + `aegisops_llm_failover_total`; streams fail over only before the first token (no duplicated answers); `context_overflow` never retries/fails over; org daily budget gate (`AEGISOPS_LLM_DAILY_BUDGET_USD`, default off, fails open on check errors, refusals loud). Explicitly NOT a reasoning loop. Pins: `test_p1_routing_executor` 17/17.

## 8. P1.7 — Model Bindings / Settings

Migration **0011 `model_bindings`** (PK(org_id,purpose), eval_state CHECK, audit fields) — applied to the dev DB and schema-verified. `bindings.py`: catalog-validated writes (capability + configured-provider checks refuse dead-end bindings), `eval_state` starts `pending`, `failed` never routes, every write lands an `audit_log` row; the resolver registers into the router at startup. API: `GET/PUT/DELETE /models/bindings` (writes gated to org-admin/platform-admin — stricter than approver), `GET /models/providers` (live health probes). Frontend: Settings → **Model routing** panel (per-purpose effective model, governed tags, eval state, reset; 403s surface inline), TopNav menu now enabled-filtered multi-provider, `served_by` SSE event → per-message model badge with visible fallback hops. Pins: `test_p1_bindings` 5 + 1 live-tier (round-trip incl. audit row + router consumption); Vitest 45/45; tsc clean.

## 9. P1.8 — Native Tool Calling

Substrate only (the intelligent loop stays P2): ToolDef→wire translation + ToolCall parsing + `args_hash` in all three adapters; structured output per family (Gemini `response_schema`, Anthropic forced-tool pattern, OpenAI `json_schema`); schemas threaded at the router call site (permissive — semantics stay in the prompt, `normalize_classification` remains the one normalizer) and the ports-extract site; eval gate green after threading. Opt-in live canaries (`AEGISOPS_FC_CANARY=1` + credentials; skip honestly otherwise) prove FC round-trip + schema adherence per provider for ~$0.01.

## 10. P1.9 — Import Boundaries

Three AST-enforced laws in `test_p1_import_boundary` (always-on, dependency-free): provider SDKs only under `app/llm/adapters/`; `langgraph` confined to the six spine modules (ADR-04 — new spread fails); `app/llm` never imports the agent layer. The formal `.importlinter` contract + a CI step run the same laws via the standard tool. 07's exit grep is satisfied **repo-wide**, stronger than the agents/packs criterion.

## 11. GitNexus Impact Findings

Pre-change impact recorded per touched symbol (`classify_json`/`stream_answer` static-floor 0 vs grep-true 8 — the documented false-floor class; `get_gemini` historical CRITICAL/19 fully consumed by the migration). `detect-changes` before the exit: 30 files / 94 symbols / 135 processes — exactly the intended scope. Index re-analyzed after implementation. Cross-verified with repo-wide search throughout.

## 12. Backend Integration

The dependency chain holds end-to-end: agents → shim → service → router/executor → adapters; admission validates models against the catalog (unknown → 400 naming the served list); `GET /models` and the integrations grid are catalog-driven (one honest row per provider); `main.py` boot-validates the catalog and registers the bindings resolver; `usage_ledger` is now written from exactly one place (the service) for every provider — live-proven on the error path (`general/google/gemini-3.5-flash/error:400…` landed from the real failed call).

## 13. Frontend Integration

The complete vertical slice shipped in-phase: types (`servedBy` additive on `ChatMessage`), store (additive `served_by` SSE case), Workspace badge (model + `fallback ×N` amber when hopped), TopNav enabled-filtered multi-provider menu, Settings Model-routing panel. No mock providers; unconfigured providers render honestly disabled. Vitest 45/45; tsc clean; the FE-05 wire shape `{id,provider,enabled,default}` unchanged (the `provider` VALUE is now the wire-family id — display-only text).

## 14. Database Impact

One additive migration (0011), applied + verified on the dev docker DB (5433); no production/shared DB exists or was touched; downgrade ships with it; absence-of-row = default (no seeding). `llm_usage` untouched structurally; `requested_model`/`provider` now populated meaningfully by every path.

## 15. Observability

Preserved and extended: trace==run untouched; one Langfuse generation per call, renamed provider-neutrally to `llm.<purpose>` (the one live-tier assertion pinning the old `gemini.generate` name is superseded — noted for the container tier); ledger rows on success/stream/error/embedding paths (P0/D3 labels verbatim); SSE vocabulary grew by exactly one additive event (`served_by`); metrics +2 (`llm_failover_total`, `llm_budget_refusals_total`), F-10's approval-wait metric untouched. Langfuse-down still degrades without touching accounting.

## 16. Security

No governance surface changed: HITL/approval/plan-guard/tenancy/RBAC/redaction/idempotency untouched (suites green, invariant files unmodified except the two seam-migrated fakes documented below); binding writes admin-gated + audited; `ModelError` redacts secrets at construction (live-proven: the leaked Google key rendered as `key=[redacted]`); no new credentials handling — provider keys live in Settings/env exactly like the Gemini key; secret scanning untouched.

## 17. Behavioral Parity

Verified: streaming resilience policy identical (three behaviors re-pinned); `GeminiError` contract for the three catching nodes; truncation note verbatim; model menu source + default model unchanged; admission 400 message still names served models; embedding degrade-to-keyword path identical; ledger purpose labels for embeddings verbatim; approval flow untouched (live smoke: healthz stamp + full run lifecycle events). Intentional, documented differences: user model pins no longer reach governed purposes (04 §4.4 — visible log; identical outputs while only Gemini binds); no pointless retries on permanent auth errors (taxonomy improvement); ledger purpose labels for chat paths now the canonical purposes (`general`/`knowledge`/`sre.triage`/`router`/`extract` instead of `answer_stream`/`classify` — append-only table, old rows keep old labels).

## 18. Tests

+76 P1 tests across 8 new/rewritten files: contracts 12, catalog 6, adapters 18, routing/executor 17, service/shim 14, bindings 6, import boundary 3, canaries 6 (opt-in). Migrated at the moved seams: `test_stream_resilience` (3), `test_llm_provider` (7, incl. the deliberate multi-provider inversion), `test_stab_p11_honest_os` fakes, two D2/D7 pins upgraded to successor pins. Container live tier: **122 passed / 0 failed** after fixes (tenancy, ledger, governance, gw1, all P1 suites, real SDK imports). Frontend 45/45 + tsc. Eval gate 10/10 + self-test at entry, after the re-route, after schema threading, and at exit.

## 19. Failures Classified

Single full regression (21:33): **1116 tests — 890 passed / 59 failed / 167 skipped.** Reconciliation vs the accepted baseline: 52/53 PRE-EXISTING environment failures unchanged (terraform-provider tiers; unchanged masking caveat — CI is the signal); 1 baseline failure healed; **7 INTRODUCED_BY_P1, all root-caused and closed post-run:** 2× a test fake missing the new `served_by` emitter method (caught by the container tier — the gate doing its job), 2× D2/D7 pins whose pinned subjects P1 legitimately deleted (per their own docstrings), 3× stab-p11 fakes patching the retired `get_gemini` seam. All verified green in targeted re-runs and the container tier. Zero unexplained regressions.

## 20. Transitional Components

T-01 (extended): `agents/llm.py` shim + the `integrations/gemini.py` stub (`GeminiError` + run-model contextvar) — removal end of P2 when callers import `app/llm` directly. DEF-15: `usage_ledger` module move deferred to the same slice. Everything else from the 07 removal table for P1 is DONE (T-02 closed).

## 21. Dead Code

No broad cleanup. Removed under PROVEN-replaced evidence only: `GeminiLLM`/`get_gemini`/`usage_of` (all consumers migrated + suites green + grep 0), `integrations/llm/` (consumers re-pointed), the stab-p11 `_FakeGemini` test fake (its seam died). The §8 removal ledger's held items are untouched.

## 22. Rollback

The old path is one `git revert` away (P1 is uncommitted working tree at report time — the whole change reverts by discarding it; once committed, a single revert restores the singleton + seam wholesale). Binding rows revert per-purpose via the DELETE endpoint (absence = default); migration 0011 has a downgrade; the executor's fallback design means no feature flag is needed — the default route IS the primary path. Rule-zero rollback proof: the eval gate was green before the re-route, so reverting restores a known-green dispatch.

## 23. Architecture Boundary Audit

Hunk-level review against the P1 MUST-NOT list: no Agent Harness, no O→R→A loop, no iterative reasoning, no failed-tool-as-observation (errors become typed events/exceptions, not model context), no subagents, no memory architecture, no workflow engine, no CloudOps/DevOps/SREOps migration (call-site edits are guard/kwarg threading only), no capability packs, no autonomous mutation, no credential broker, LangGraph untouched (6 importers, same 5 APIs — now AST-pinned), no new orchestration framework. Four-eyes remains absent (healthz stamp live-verified). The boundary tests make several of these violations mechanically impossible to reintroduce silently.

## 24. P2 Boundary Verification

What P1 hands P2, and nothing more: purpose-routed calls, native FC + structured output substrate, typed errors ready for failed-tool-as-observation, `ServedBy` telemetry, budget-gate plumbing, eval-gated bindings. The kernel loop, ToolRegistry v2, run_events, memory tiers, subagents, compaction remain unimplemented and unstubbed. P2 must not start without the operator's explicit prompt.

## 25. Final Verdict

**P1 COMPLETE — READY FOR ACCEPTANCE**

Known limitations (§29 of doc 11): live Provider-B round-trip is fixture/container-proven pending real anthropic/openrouter credentials; the sandbox Gemini key on this host is dead (`API_KEY_INVALID` — pre-dates P1, environment); anthropic SDK won't pip-install on this Windows host (long paths — container covers it). Work is deliberately uncommitted pending operator acceptance; commit boundary recommendation: one P1 commit (all app/tests/docs), keeping the standing operator artifacts out, exactly like the P0 flow.
