# Architecture Analysis — AegisOps Today vs Waku vs the Modern Harness Field

> Ground-truth comparison that the rest of the blueprint stands on. Every current-state
> claim was audited against commit `a974290` (branch `feature/cloudops-v3`, 2026-08-03) —
> not against README/PROGRESS claims. Waku claims come from a first-hand read of
> `waku-agent` source plus the repo's own code-cited gap analysis
> (`aegisops_production_kit/docs/waku-agent/GAP_ANALYSIS.md`).

---

## 1. The two systems, honestly framed

| | AegisOps | Waku |
|---|---|---|
| Kind | Multi-tenant governed CloudOps platform | Single-user local assistant harness |
| Mutation surface | Real Terraform against real clouds, behind human approval | None (calendar/notes/messages, local files) |
| Trust architecture | Split-trust: deterministic Governed Core + LLM shell | Trust-the-loop: model drives tools directly |
| Core harness LOC | ~4,100 (agents) over LangGraph | ~2,800 (loop+memory+runtime+tools), zero framework |
| Providers | 1 (Gemini), 3 model ids, validation-only seam | 11 provider entries over 2 wire formats |
| Native tool calling | **zero** | the spine of every turn |
| Governance | tenancy, RBAC, four-eyes, plan guard, idempotency, audit | none (different job) |

They are not competitors; they are complementary halves. **Waku is a reference for the
reasoning engine; AegisOps is the reference for everything around it.** The redesign
thesis: put a Waku-grade loop and provider layer *inside* AegisOps-grade governance.

---

## 2. Current AegisOps architecture (audited, post-GW-1)

### 2.1 What it is

FastAPI + LangGraph + Next.js. One compiled 12-node graph
(`backend/app/agents/graph.py:83-110`):

```
START → router →(conditional)→ cloudops_plan | devops_plan | sre_analyze | knowledge | general
cloudops_plan/devops_plan/sre_analyze →(conditional)→ approval | finalize | general
approval →(decision)→ execute | finalize ;  execute → verify → finalize
finalize → servicenow_update → notify → END
```

- **Checkpointer:** `AsyncPostgresSaver`, `thread_id == run_id` — the approval interrupt
  survives restarts and resumes from another worker (`agents/checkpointer.py`,
  `runner.py:40,63`).
- **Interrupts (3 sites):** the approval gate (`approval.py:58`), the whole-DAG approval
  for multi-step runs (`exec_loop.py:154-175`), deviation re-approval mid-execution
  (`exec_loop.py:303-308`).
- **Eventing:** dual-mode channel — in-process queue or Redis Streams
  (`agents/events.py:34-155`), selected by `aegisops_event_bus` (code default `memory`,
  compose posture `redis` on both workers). Typed `Emitter` with 11 event kinds; STAB
  P0-3 cursor semantics make approval continuations tail from *now* on any worker.
- **Governed mutation:** 20 catalog templates + 1 alias (7 AWS / 7 Azure / 6 GCP,
  `templates.py:454-483`), Terraform-only mutation with per-resource state workspaces
  via `TF_WORKSPACE` env isolation, vars strictly `-var` (`tools/terraform.py:31-36,
  125-133`), plan-file-per-run, plan_guard hard guard re-asserted at the approval
  choke-point, policy checks evaluated against real `terraform show -json` output with
  honest `evaluated: False` rows for unverifiable controls (`templates.py:40-68`).
- **Exec loop (more evolved than earlier analyses recorded):**
  `exec_loop.py` validates DAGs against the catalog only, plans each step in its own TF
  workspace, wires step outputs (`resolve_wires`: `"<out>"`, `"<out>[i]"`,
  `"input:<field>"`), raises **one** interrupt for the whole DAG, treats any revision as
  a deviation requiring fresh approval, claims per-step idempotency keys, honors cancel
  only at step boundaries, and reports partial outcomes honestly. Bounds: `MAX_STEPS=5`,
  `MAX_REPLANS_PER_STEP=1` (default replanner returns `None` — no replan ever happens).
  Flag default `off`; **this install's `.env` sets it `on`.**
- **Gateways (GW-1, new):** a channel-agnostic `Transport` Protocol
  (`gateways/transport.py:44-85`) with Telegram as the first implementation. Identity
  links to real Keycloak-backed users via one-time SHA-256-hashed codes; RBAC re-checked
  on **every message and every button click**; approval buttons carry untrusted opaque
  tokens and `resolve_approval_core` re-runs org scope + four-eyes + state checks at
  click time; outbound text is redacted, High-confidentiality content withheld behind a
  deep link; progressive answer streaming edits one preview message under rate-limit
  discipline. Gateway turns enter **the same** `prepare_run`/`build_drive` pipeline as
  the web UI (`gateways/driver.py:182-225`).
- **Recovery:** RunSupervisor (heartbeat TTL 45s/15s, graceful drain), Reconciler
  (60s sweep; resumes stranded runs from the checkpoint if resumable else fails them
  honestly; TF-plan hygiene; orphan inventory rebuild).
- **Tenancy/RBAC:** strict tenancy is now the default and real — Keycloak `org` claim
  first, refusal for org-less principals, cross-org reads 404, `org_for` raises rather
  than defaulting (`security/tenancy.py:46-100`, `db/repositories.py:16-32`). 8 roles,
  3 capability tiers, re-checked at routes, at the approval core, at the mutation
  choke-point, and per gateway interaction.

### 2.2 July → today delta (what the honesty waves fixed)

| July finding | Status now |
|---|---|
| "Multi-tenancy is fictional (`get_default_org` everywhere)" | **Fixed.** Strict resolver, refusal semantics; the symbol no longer exists |
| "Model menu is a myth — body.model never read" | **Fixed at the model level.** `ChatRequest.model` validated (400 on unknown), bound per-run via contextvar; menu lists only the 3 servable Gemini ids; e2e asserts Claude/GPT/Llama absent |
| "Traces tab is fake" | **Fixed.** Real `run_steps`-derived span tree + browser-resolvable Langfuse deep link |
| "Policy checks are hardcoded True" | **Mostly fixed.** Real predicates over plan JSON; honest "not evaluated" rows; residue: `devops.py:102-105`, `sre.py:146` still hardcode `passed: True` |
| "SRE remediation lies" | **Fixed.** Real PromQL signals, `proposed_not_executed` when no kubeconfig, real K8s actions when configured |
| "SSE breaks under horizontal scale" | **Fixed in shipped posture** (Redis Streams + continuation cursor); code default is still `memory` |

### 2.3 What is still structurally missing (the redesign targets)

1. **The LLM layer validates but does not dispatch.** `get_provider()` is called once in
   `prepare_run` and its provider object discarded; `GeminiProvider.agenerate/astream/
   aembed` have **zero callers**; all real calls go through the `get_gemini()` singleton
   (`agents/llm.py:39,49,97`). A second provider requires rewriting `agents/llm.py`.
2. **Zero native tool calling.** `tools=` reaches the Gemini config (`gemini.py:103`)
   and no caller populates it. Router/extraction are prompt-and-parse JSON
   (`agents/llm.py:21-30`). `CLAUDE.md`'s "native tool-calling" claim is aspirational.
3. **No iterative reasoning anywhere.** The graph is single-pass; the investigation
   registry (frozen, read-only, `MAX_CALLS=8`, spawn shares budget — excellent bones)
   has **no LLM director**: its only caller is one hardcoded `list_deployments`
   (`sre.py:83-91`). The exec loop's replanner returns `None` by design.
4. **One model per run, no purposes.** Router, planner, extraction, chat all read the
   same contextvar. No cheap-model tiering (gate/consolidation-class work), no
   per-purpose binding, no reasoning-effort control — in fact **no generation params at
   all** (no temperature, no max_output_tokens, no per-call timeout anywhere in
   `backend/`).
5. **No cost ledger, no budgets.** Tokens live only in Langfuse (vanish with key
   rotation/retention); embedding calls aren't recorded even there; nothing can stop a
   run on spend.
6. **Memory is two-tier and ungated.** Rich per-row substrate (pgvector embeddings,
   confidentiality, correlation ids) + genuinely good transcript budgeting (70/30
   recent/digest), but retrieval fires unconditionally every turn, there is no
   fact/episode tier, no consolidation, and context assembles once per node — never per
   observation (there are no observations to react to).
7. **No behavioral eval gate.** 596 real tests, zero that would catch a router-prompt
   quality regression. No dataset, no judge, no score-gated release. This is the
   precondition for *any* provider swap.

### 2.4 Live defects found by this audit (fix-now list, independent of redesign)

| # | Defect | Evidence |
|---|---|---|
| D1 | One-click region retry unreachable: classifier emits `bad_location`, `suggest_retry` matches `bad_region` — both test suites pass on their own side of the boundary | `provider_errors.py:115` vs `:142`; `tests/test_retry_undo.py:26`, `tests/test_provider_errors.py:38` |
| D2 | U3 made the lazy model fallback dead: `prepare_run` always resolves a concrete model → `_effective_model` never reaches `self.model`; `_ensure_model`'s `models.list()` network call still fires and its result is discarded; key-can't-serve-default now hard-fails instead of falling back | `gemini.py:75-108`, `api/chat.py:258-330` |
| D3 | Embedding calls invisible in every sink (no Langfuse generation, no metric) | `gemini.py:164-173` |
| D4 | Frontend model menu is a hardcoded literal; nothing fetches `GET /models` — registry/menu sync is comment-enforced | `frontend/lib/data.ts:37-45`, `api/integrations.py:114-122` |
| D5 | `"applying"` run status is read in five predicates and written by nothing — vestigial state confusing reconciler/cancel logic | `chat.py:116,137,152`; `reconciler.py:33,100`; `artifacts.py:234` |
| D6 | Gateway turns hardcode `model=None` (default model) — fine, but undocumented | `gateways/driver.py:201-203` |
| D7 | Dead code: `agents/llm.py:generate()` (no callers), `modelColor` Claude/GPT/Azure branches | `agents/llm.py:43`, `frontend/lib/colors.ts:15-20` |
| D8 | Hardcoded policy rows remain in DevOps/SRE cards | `devops.py:102-105`, `sre.py:146` |
| D9 | **Deployment governance drift:** this install's `.env` disables four-eyes for production (`AEGISOPS_FOUR_EYES_FOR_PRODUCTION=false`) and enables the exec loop — both diverge from code defaults, silently changing approval semantics | `.env:126,139` vs `settings.py:47,49` |

---

## 3. The Waku harness (first-hand read)

### 3.1 Anatomy

```
gateways (cli/voice/telegram/discord/whatsapp/dashboard) → Waku.respond()
  → [optional triage graph: small-model classify → quick_reply | full loop; fail-open]
  → Session.build_system(): SOUL.md + local time + own-model identity
      + GATED memory retrieval + matched skills            (runtime/session.py:63-88)
  → run_loop(): for i in 1..10:
        llm(messages, tools) → stream deltas → append content blocks
        no tool_use? return reply
        else execute each tool (errors → observation strings) → append results
                                                              (loop/agent.py:63-114)
  → persist exchange (+ [tools used: …] fold-in) → maybe_consolidate() → MEMORY.md
```

- **Provider layer:** 11 providers over **2 wire formats** — Anthropic-native (Anthropic,
  Kimi, GLM, MiniMax) and OpenAI-compatible (OpenAI, Gemini-compat, DeepSeek,
  OpenRouter, xAI, OpenCode×2). The entire bridge is ~110 lines
  (`loop/models.py:197-311`): content-block↔tool_calls translation, streaming with
  tool-arg slot assembly, `max_tokens`→`max_completion_tokens` fallback that retries
  *only* when the error is about that parameter, Gemini `thought_signature`
  round-tripping, key hygiene (strip + latin-1 validation with a plain-English smart-
  quote error), per-provider live model catalogs for the Settings picker, and
  flagship/fast default pairs per provider.
- **Memory:** 4 tiers (raw chat_log / semantic facts+FTS5 / episodic / procedural
  SKILL.md files) + two managing passes — a **retrieval gate** (cheap-model "should we
  retrieve at all?", fails open, decision is an observable event) and **consolidation**
  (every 6 exchanges, distills facts + one episode, never loses data on failure).
- **Tools:** registry of name+description+JSON-schema+fn; `wants_notify` lets
  long-running tools stream progress through the loop's observer; failed execution
  returns `Error running X: …` as the model's observation (`tools/registry.py:47-58`);
  MCP bridge mounts external servers from `mcp.json`.
- **Graph workflows (new since the gap analysis):** a 200-line wave-based engine —
  state is one dict, **routers are plain code, never models**; parallel nodes must
  write disjoint keys or the engine raises; per-node `max_visits` + global `max_steps`
  guards; node exceptions surface into state, never crash the run
  (`graph/engine.py`). Used for a triage workflow that fails open to the plain loop.
- **Ops:** JSONL trace always-on + optional OTel; permanent `usage.jsonl` token ledger
  separate from resettable traces; live architecture diagram animated by trace events;
  "TURN NEVER FINISHED" hang tell; model arena racing N models through the real harness
  in throwaway home dirs; LLM-judge evals + a release gate that exits 1.

### 3.2 What Waku gets right that AegisOps must inherit

1. **The loop** — observe→reason→act with failed-tools-as-observations and hard
   iteration budgets. AegisOps has *nowhere* that can chase a symptom across three
   tools and revise. (Lands inside the already-safe investigation registry.)
2. **The provider bridge discipline** — one canonical dialect, thin adapters, quirks
   handled once, catalogs fetched live, keys validated at startup with human errors.
3. **Retrieval gating + consolidation** — memory that decides *whether* to remember and
   distills what matters, with observable decisions.
4. **The eval gate** — deterministic dataset + judge thresholds that can block a
   release. AegisOps can ship a router-prompt regression today and nothing notices.
5. **Honest serving metadata** — every turn records which model/provider answered
   (`app.py:99-104`); a fallback or small-model answer is labeled as such.
6. **Cost as a permanent ledger** — tokens are ground truth, dollars derived, delegated
   spend counted.

### 3.3 What Waku must NOT be copied on

- No tenancy/RBAC/audit/approval — its registry registers anything
  (`tools/registry.py:41-42`); fine for a laptop, disqualifying for CloudOps.
- Hot-reloaded executable skills (mtime-reload prompt bodies) — an ungoverned change
  vector; AegisOps' governed equivalent is the module-promotion pipeline + RAG runbooks.
- Flat 24-message window — AegisOps' 70/30 verbatim/digest transcript is better.
- While-loop-as-the-whole-app — the durable-checkpoint interrupt machinery is the one
  thing a while-loop cannot replace (a different process must be able to resume an
  approval days later).

---

## 4. Pillar-by-pillar verdict (updated to current code)

| Pillar | Winner | One-line reason |
|---|---|---|
| Iterative loop | **Waku, decisively** | AegisOps still has zero iteration anywhere; the director seat is empty by design (`investigation.py:14-16`) |
| Provider layer | **Waku** | 11 providers/2 formats/live catalogs vs a validation-only seam over one singleton |
| Tool calling | **Waku** | native, parallel, streamed vs prompt-and-parse JSON |
| Governance | **AegisOps, no contest** | tenancy/RBAC/four-eyes/plan-guard/idempotency/approval-interrupt vs nothing |
| Execution safety | **AegisOps** | catalog-only DAG, per-step idempotency, deviation re-approval, honest partials |
| Memory substrate | **AegisOps** (rows, budgets, positional recall) — **Waku** (tiers, gate, consolidation) | compose them: their shape on our substrate |
| Evals/release gate | **Waku, decisively** | 208 deterministic + judge + exit-1 gate vs zero behavioral coverage |
| Tracing/ops depth | **AegisOps** | trace-id==run-id, cross-process span closure, redaction, Prometheus |
| Ops immediacy | **Waku** | zero-dep always-on trace, live diagram, hang tell, cost ledger |
| Channels | **AegisOps now** | GW-1's Transport seam + real identity linking + click-time RBAC beats waku's allowlist gateways *on governance*; waku still has more channels |

---

## 5. Idea mining — the modern harness field

Mechanisms worth stealing, with where each lands in the blueprint. (Sources: Claude
Code, OpenAI Codex/ChatGPT agents, Cursor, OpenHands, Nous Hermes, badlogic's pi,
Google Antigravity, plus LangGraph/Temporal/MCP as infrastructure references.)

### Claude Code
| Mechanism | What it is | Where it lands |
|---|---|---|
| Permission modes + tool allowlists | every tool call passes a policy gate; modes change the gate, not the tools | ToolRegistry v2 effect classes + middleware (`Agent_Harness.md §5.2`) |
| Hooks | user-owned pre/post-tool-use interception | policy middleware chain; audit stage writes the `audit_log` rows only 2 call sites write today |
| Subagents (Task tool) | isolated-context workers returning summaries, not transcripts | `spawn()` with shared budgets + typed results (§5.5) |
| Plan mode | separate plan-then-approve phase before mutation | already AegisOps' approval model; generalize to the DAG plan artifact |
| CLAUDE.md / memory files | standing org/user context loaded every session | `user_memory` + org memory recipe in the Context Engine |
| Compaction | summarize-and-continue when context fills | Context Engine iteration-boundary compaction (OpenHands "condenser" is the same idea) |
| Background tasks | long work decoupled from the conversational turn | already the supervisor's shape; extend to long-running executors |

### Codex / ChatGPT agents
| Mechanism | | |
|---|---|---|
| Sandbox tiers with explicit network/file policy | each task runs under a declared capability envelope | executor sandboxes for Terraform/K8s (`CloudOps_Harness.md §7`); tool `effect` + per-env policy |
| AGENTS.md | repo-local operating instructions for agents | org policy packs: per-org standing constraints injected into planner context |
| Best-of-N + judge | parallel attempts, judged selection | offline model arena for binding promotion (never in-product against real clouds) |
| Diff-first review | the reviewable artifact is a diff, not prose | the approval artifact: terraform plan diff + policy table + cost + blast radius (already strong — keep; extend to K8s server-side dry-run diffs) |

### Cursor
| | | |
|---|---|---|
| Plan-model vs apply-model split | expensive model thinks, cheap model executes mechanical edits | purpose-tier routing: planner=flagship, extraction/gate=fast (§4.3-4.4) |
| Shadow workspace | changes verified in an isolated copy before proposing | per-resource TF state workspaces already do this for infra; keep as a named principle |
| Rules files | per-repo standing constraints | org policy packs (same landing as AGENTS.md) |

### OpenHands
| | | |
|---|---|---|
| Event-stream architecture | every action/observation is an immutable event; state = fold(events) | run event log: Redis Streams (live) + `run_steps`/events table (durable, replayable) — formalize what exists |
| Controller/Runtime split | reasoning process separated from execution sandbox | exactly Split-Trust: Agent Plane vs Execution Plane (`Proposed_Architecture.md`) |
| Condenser | pluggable context condensation | Context Engine compaction policy |
| Stuck detector | same action N times → intervene | kernel stuck rule (§5.1): 3rd identical call injects a nudge, 5th ends the run |
| Microagents | keyword-triggered context injections | governed variant: RAG runbook cards, org-curated (never hot-reload executables) |

### Hermes (Nous)
| | | |
|---|---|---|
| Schema-first prompted tool calling | XML/JSON tool grammar for models without native FC | emulation tier in the capability registry — read-only purposes only (§4.6) |
| Explicit reasoning-tag separation | thinking distinct from answer in the wire format | `ThinkingPart` in the canonical model; never persisted raw |

### pi (badlogic)
| | | |
|---|---|---|
| Radical kernel minimalism | the harness core is ~100 lines; everything else is tools/context | kernel stays small; enterprise weight goes into policies around it (§5) |
| Session as JSONL event log | append-only, replayable, greppable | reinforces the event-log formalization |
| Subprocess agent protocol | newline-JSON events over stdio (how waku delegates to pi) | the executor↔engine progress protocol for TF/K8s runners |

### Antigravity
| | | |
|---|---|---|
| Artifacts-first agent work | plans/walkthroughs/screenshots as first-class reviewable artifacts | the approval artifact grows: per-step plans, policy table, cost, impact graph, **verification plan**, rollback plan — one reviewable document |
| Verification before done | agents must produce evidence, not claims | verify steps produce evidence cards (SDK reads, health checks) attached to the run — extends the existing verify node |
| Manager view over parallel agents | fleet-level visibility | ops console: live flow diagram + stalled-step tell (waku's mechanic over `timing.ORDER` + `run_steps`) |

### Infrastructure references
- **LangGraph** — keep for the durable interrupt + checkpointing; wrap it behind the
  harness so agents stop importing it (makes the Temporal question swappable later).
- **Temporal** — decision gate unchanged: adopt only when workflows run hours+, need
  versioned migrations mid-flight, or fan out beyond what PG-checkpointed steps handle.
- **MCP** — mount external tool servers as read-effect tools behind org allowlists;
  mutation-shaped MCP names refused at registration (the investigation denylist,
  reused).

---

## 6. Consolidated verdicts

**ADOPT (build now)**
1. Provider Layer with canonical types + thin adapters + purpose router + capability
   registry (`Agent_Harness.md` §3-4) — dissolves §2.3-1/2/4/5.
2. Bounded LLM director over the frozen read-only investigation registry (the INV
   loop) — dissolves §2.3-3 for read paths; mutation untouched.
3. Eval dataset + judge + CI release gate **before any provider swap**.
4. `llm_usage` ledger + budgets that halt at safe boundaries.
5. Retrieval gate + consolidation-to-proposals on the existing memory substrate.
6. Live flow diagram + stalled-step tell (data already exists in `run_steps`).

**ADAPT (reshape existing)**
7. exec_loop → the full CloudOps Workflow Engine (parallel waves, saga rollback with
   pre-approved compensation, day-2 verb registry, change windows) — `CloudOps_Harness.md`.
8. Gateway seam → add Slack/Teams transports on the existing Protocol; approval stays
   deep-link + click-time re-check.
9. Prompts → versioned registry entries tied to eval verdicts.

**AVOID (explicitly rejected, with reasons)**
- Restructuring around a while-loop (kills the durable interrupt + checkpoint recovery).
- Hot-reload skill files steering agents (ungoverned change vector).
- Agent-authored standing memory writes (unaudited plan-input changes) — proposals only.
- In-product model arena against real clouds (N models racing = N tickets + N applies).
- Per-tool agent swarms; LLM-authored HCL; SDK mutation paths outside the day-2 registry.
- Betting the mutation path on pre-1.0 frameworks (deepagents et al.) — patterns yes,
  dependency no.

**REDESIGN (the two structural moves)**
- `integrations/llm/*` + `gemini.py` singleton + `agents/llm.py` → `app/llm/` (the real
  dispatch seam). The current registry's *validation* behavior (unknown model → 400) is
  correct and survives; it just gains dispatch, streaming, resilience, and purposes.
- `agents/exec_loop.py` (~354 lines, single-branch, sequential) → `app/engine/` with the
  step contract; existing behaviors (catalog-only validation, wiring grammar, deviation
  re-approval, per-step idempotency, boundary-only cancel, honest partials) carry over
  as the engine's invariants — they are the best part of the current design.

**Invariants that survive every change (the constitution):**
Terraform-only mutation via the approved catalog; the durable human-approval interrupt;
plan_guard re-asserted at the choke-point; strict tenancy + RBAC + four-eyes; per-step
idempotency; cancel never mid-apply; honest partial reporting; redaction on every
egress; trace-id == run-id.
