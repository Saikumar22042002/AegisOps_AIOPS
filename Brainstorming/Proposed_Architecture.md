# Proposed AegisOps Architecture

> The target shape. Split-trust is kept — it is the moat. The redesign makes three
> structural moves and leaves the governance core intact:
>
> 1. **Provider Layer** (`app/llm/`) — agents stop touching SDKs; models become config.
> 2. **Agent Kernel** (`app/harness/`) — one bounded loop powers every reasoning agent;
>    iteration arrives inside the existing read-only boundary.
> 3. **Workflow Engine** (`app/engine/`) — `exec_loop` grows into a wave-scheduled,
>    saga-capable, resumable execution engine behind the same approval gate.

---

## 1. The layered target

```
┌───────────────────────────────────────────────────────────────────────────────┐
│ CHANNELS   Web (Next.js SPA) · Telegram (live) · Slack/Teams (same Transport  │
│            Protocol) · Alert webhooks (Alertmanager→incident runs) · REST/CLI │
├───────────────────────────────────────────────────────────────────────────────┤
│ CONTROL PLANE (FastAPI, stateless, N workers)                                 │
│   Keycloak OIDC → strict tenancy → RBAC guards → rate/concurrency limits      │
│   prepare_run (single admission point) · /approvals (four-eyes core) ·        │
│   /runs + artifacts (org-scoped 404s) · Settings/Models admin · SSE           │
├───────────────────────────────────────────────────────────────────────────────┤
│ AGENT PLANE — Intelligent Shell (LLM; zero mutation authority)                │
│   AgentKernel: bounded loop · stuck detector · budgets(cost!) · checkpoints   │
│   Agents: router · planner(GoalDAG drafts) · investigator(INV loop, frozen    │
│   read-only registry) · sre-triage · knowledge · general · judge(offline)     │
│   Context Engine: per-purpose recipes · per-ITERATION assembly · retrieval    │
│   gate · consolidation→proposals · positional recall · offloaded artifacts    │
├───────────────────────────────────────────────────────────────────────────────┤
│ PROVIDER LAYER (app/llm — the new seam)                                       │
│   service.generate(purpose=…) → Router → RoutePlan → ResilientExecutor        │
│   → Adapters: anthropic · openai-compat · google · bedrock · azure · litellm* │
│   Catalog+Capabilities (models.yaml ⊕ model_bindings DB) · Reasoning mapper   │
│   · streaming normalizer · retry/breaker/fallback · llm_usage ledger+budgets  │
├──────────────────────────────── the boundary ─────────────────────────────────┤
│ GOVERNED CORE — Execution Plane (deterministic; LLM proposes, never executes) │
│   compile_goal_dag (catalog-only · wiring · guard · compensation closure)     │
│   Approval Service (durable interrupt · artifact w/ plans+policy+cost+impact+ │
│   verify+rollback · tiers · deviation re-approval · four-eyes)                │
│   WorkflowEngine (waves · locks · idempotency · cancel-at-boundary · windows) │
│   Executors: Terraform(TF_WORKSPACE isolation) · K8s(dry-run diff/rollout)    │
│   · Day-2 registry(SDK verbs, governed) — each: plan/apply/verify/compensate  │
├───────────────────────────────────────────────────────────────────────────────┤
│ STATE & WORLD                                                                 │
│   Postgres: runs/run_steps/messages(+pgvector)/approvals/resources/audit/     │
│   user_memory/llm_usage/model_bindings/prompt_registry + LangGraph checkpoints│
│   Redis: event streams · locks · idempotency · heartbeats · pending-params    │
│   Neo4j world model: inventory graph · impact_of · drift/orphan reconciler    │
├───────────────────────────────────────────────────────────────────────────────┤
│ OBSERVABILITY & QUALITY                                                       │
│   Langfuse (trace_id=run_id, cross-process spans) · OTel→Prometheus/Grafana   │
│   Live flow console (run_steps-driven diagram + stalled-step tell)            │
│   CI eval gate: dataset + judge thresholds + prompt/binding versioning        │
└───────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Layer responsibilities and contracts

### Channels
- One `Transport` Protocol (exists: `gateways/transport.py`) — Slack/Teams are new
  implementations, not new architectures. Contract stays: transports move text and
  buttons; identity is resolved to a Keycloak-backed principal on every interaction;
  approvals re-run the full check at click time; outbound passes redaction/withholding.
- New channel class: **webhook ingress** (Alertmanager, ServiceNow inbound) — creates
  runs with `source="alert"`, same admission path, no principal shortcut (service
  identity + org mapping, refusal on ambiguity).

### Control plane
- `prepare_run` remains the single admission point (model validation, limits, tenancy,
  session, Run row). It gains: purpose-aware routing snapshot (RoutePlan pinned on the
  run), budget reservation, and window awareness.
- The vestigial `"applying"` status is replaced by a real state machine:
  `running → awaiting_approval → scheduled? → executing → verifying → completed |
  failed | cancelled | rolled_back` — every literal written by exactly one owner.

### Agent plane
- Every agent is an `AgentSpec` on the one kernel; no agent owns a bespoke loop.
- The **planner** is the only agent whose output crosses the boundary — and it crosses
  as *data* (GoalDAG draft) into `compile_goal_dag`, never as execution.
- The **investigator** runs the INV loop over the frozen read-only registry
  (`investigation.py` bones kept: registration-time mutation denylist, freeze,
  MAX_CALLS, budget-sharing spawn). It is the multi-hop triage engine SRE lacked.
- An optional **critic** pass (cheap model) reviews the planner's DAG for missing
  dependencies/rollback gaps *before* compilation — findings attach to the approval
  artifact as advisory notes. Multi-agent stays planning/analysis-side only.

### Provider layer
- Everything in `Agent_Harness.md`. One sentence: purposes are the API, models are
  config, adapters are the only SDK importers, and every answer records who served it.

### Governed core
- Everything in `CloudOps_Harness.md`. One sentence: approved data walks a
  deterministic wave engine with idempotent steps, verified outcomes, pre-approved
  rollback, and interrupts for anything that deviates.

### State & world
- Postgres stays authoritative; new tables: `llm_usage`, `model_bindings`,
  `prompt_registry`, `channel_*` (exist), plus `run_events` if/when the Redis stream
  gets a durable projection (decision gate — start with `run_steps`, which already
  captures the canonical timeline).
- Neo4j earns its keep through `impact_of` on **every** mutation approval (today
  destroy-only) and the drift/orphan sweeps; the fold-to-Postgres exit clause from the
  target-architecture doc stands if queries stay shallow.

### Observability & quality
- Keep: trace=run, deterministic span ids closable cross-process, redaction on every
  egress, Prometheus series, honest Traces tab.
- Add: the live flow console (waku's animated-architecture mechanic over data
  `timing.ORDER` + `run_steps` already emit), `served_by` badges, ledger dashboards
  (spend by org/purpose/model), eval-gate history.

---

## 3. What stays / moves / dies

| Current | Fate |
|---|---|
| `agents/graph.py` 12-node graph, Postgres checkpointer | **Stays** (Phase-boundary: kernel wraps LangGraph; agents stop importing it) |
| `agents/approval.py` durable interrupt + immutable Approval row | **Stays verbatim** — the crown jewel |
| `tools/terraform.py` (workspaces, saved plans, -var only) | **Stays**, wrapped by `TerraformExecutor` |
| `agents/exec_loop.py` | **Grows into `app/engine/`** (invariants kept — see CloudOps_Harness §12) |
| `agents/plan_guard.py`, `security/*`, `agents/templates.py` policy predicates | **Stay**; called from engine compile + step lifecycle |
| `agents/events.py` dual-mode bus + Emitter | **Stays**; default flips `memory→redis`; kernel adds 3 event kinds |
| `gateways/*` Transport seam, identity, stream ladder | **Stays**; more transports |
| `agents/investigation.py` frozen registry | **Stays**; gains its LLM director (the INV loop) |
| `agents/memory.py` build_context/build_transcript | **Stays**; called per-iteration; gains gate + consolidation siblings |
| `supervisor.py`, `reconciler.py` | **Stay**; engine + scheduled runs plug into the same sweeps |
| `integrations/gemini.py` singleton | **Dies** (becomes `app/llm/adapters/google_.py`; contextvar model-binding replaced by RoutePlan-on-run) |
| `integrations/llm/{base,registry,gemini_provider}.py` | **Dies as dispatch fiction**; its validation behavior (unknown model→400) moves into router resolution |
| `agents/llm.py` | **Shrinks to a shim** over `app/llm/service`, then deleted |
| `agents/provider_errors.py` | **Stays** (cloud-side triage; D1 kind-mismatch fixed); gains an LLM-side sibling taxonomy |
| Frontend hardcoded `modelOptions` | **Dies**; menu renders `GET /models` + bindings |
| `"applying"` vestigial status | **Dies**; real state machine (control plane above) |

---

## 4. Target package layout

```
backend/app/
  llm/            types.py errors.py catalog.py router.py service.py executor.py
                  usage.py reasoning.py emulation.py adapters/ config/models.yaml
  harness/        kernel.py spec.py tools.py budgets.py subagents.py interrupts.py
  agents/         router.py planner.py investigator.py sre.py knowledge.py general.py
                  (thin: prompts + AgentSpecs + node glue; no SDKs, no loops of their own)
  engine/         dag.py steps.py engine.py saga.py locks.py windows.py
                  executors/{terraform.py,k8s.py,day2.py}
  gateways/       (as today) + slack/ teams/ webhook/
  security/ db/ rag/ graph_db/ api/ …  (as today)
```

Import direction is law (CI-enforced): `agents → harness → llm`; `engine` imports
neither `agents` nor `llm`; `llm` imports nothing above itself. The graph/LangGraph
dependency lives only in `harness/interrupts.py` + `agents/graph glue`.

---

## 5. Data model deltas

| Table | Purpose | Notes |
|---|---|---|
| `llm_usage` | append-only token/cost ledger | schema in Agent_Harness §4.9; org-scoped; `agent_kind` covers subagents/gates |
| `model_bindings` | org × purpose → model (+params, eval_state, audit) | UI-writable; RBAC platform_admin; never governs without eval pass/waive |
| `prompt_registry` | versioned prompts (hash, owner, changelog) | every generation + eval verdict records prompt version |
| `runs.route_plan` (col) | pinned RoutePlan snapshot | honest serving + cache affinity + audit |
| `runs.status` | + `scheduled`, `verifying`, `rolled_back`; remove dead `applying` | one writer per literal |
| `run_steps` | + `wave`, `evidence` (JSONB), `compensation_of` | engine projection source |

---

## 6. Non-functional posture

- **Scaling:** API workers stateless (Redis Streams + PG checkpoints already prove
  2-worker approval-continuation); engine steps are worker-portable (idempotency +
  locks); heavy executors (TF/K8s) can later move to a dedicated worker pool consuming
  a step queue — the Executor protocol is the seam, no redesign needed.
- **Security:** no new trust: provider keys live in env/secret manager, adapters never
  log payloads pre-redaction; gateway posture unchanged; day-2 registry is the single
  audited SDK-write file; model bindings are RBAC'd + audited + eval-gated.
- **Residency:** org routing policy can pin providers (bedrock/azure/vllm) — the
  capability registry makes "EU org never leaves Azure" a config row, not a fork.
- **Cost:** budgets enforced at call, iteration, and step boundaries; ledger feeds
  chargeback; arena/judge spend is `agent_kind`-tagged.

---

## 7. Rejected alternatives (decided, with reasons)

| Alternative | Verdict |
|---|---|
| Rebuild on Temporal now | Defer (gate unchanged): PG-checkpointed LangGraph covers today's durations; the engine's step contract is substrate-neutral, so the swap stays cheap if the gate trips |
| LiteLLM as *the* provider layer | No — hot path owns its adapters (determinism, taxonomy, capability metadata); LiteLLM remains an optional adapter for the long tail |
| Microservice split (agents svc / engine svc / llm svc) | No — one deployable with enforced package boundaries; split only when the executor pool needs independent scaling |
| While-loop-only harness (waku/pi shape end-to-end) | No — durable cross-process interrupts and reconciler redrive are checkpoint-shaped; the loop lives *inside* that machinery |
| Agent-per-tool swarms; LLM-generated HCL; hot-reload skills; in-product model arena | No — unchanged from prior analysis; all four weaken governance for no capability gain |
