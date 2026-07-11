# AegisOps — Target Architecture Plan (v1, consolidated)

> The single authoritative plan, synthesizing: `ANALYSIS.md` (ground truth), `FIX.md` (remediation plan), the owner's critiques (LangGraph underuse, Temporal, Neo4j world model, Terraform state, memory, multi-step autonomy), external brainstorms (Executive Runtime, Terraform strategy hierarchy), current research on deepagents/LangChain, and context-engineering doctrine. This document supersedes conflicting guidance in earlier brainstorms; it AMENDS (does not replace) FIX.md.

---

## 1. The architecture philosophy — Split-Trust

AegisOps is two systems with one boundary:

**The Governed Core (deterministic — zero LLM trust).** Everything that mutates infrastructure. The LLM never authors HCL, never selects unapproved code, never calls a mutating tool directly, never bypasses a guard. Mutation flows through exactly one pipeline: approved Terraform module → Pydantic validation → plan → plan_guard → durable human approval → apply in isolated state → verify → record. This already exists and is the strongest part of the codebase (ANALYSIS finding #1–#3). It is the moat: the March-2026 Claude-Code-destroys-production incident happened *with* a human in the loop; AegisOps's deterministic plan guard blocks that class of plan before a human can rubber-stamp it. Nothing in this plan weakens the core.

**The Intelligent Shell (LLM-driven — full autonomy where blast radius is zero).** Planning, investigation, observation, adaptation, conversation. Here the Claude-Code-style loop is not only safe but the whole point: loop until the goal is met, spawn sub-agents, observe and replan. The shell's only path into the core is a single tool whose interior is the governed pipeline.

**The boundary:** `execute_governed_step(cloud, resource, action, params)` — the one mutating tool. Its interior is the existing deterministic pipeline. The shell decides *what* and *when*; the core alone decides *how* and *whether*.

Rejected models (decided, not open): trust-the-LLM tool loops for mutation (deepagents' own security model: "the agent can do anything its tools allow"); ephemeral agent-per-tool swarms (16 LLM wrappers around tool clients = cost/latency/failure surface with no capability gain); runtime LLM-generated HCL execution; SDK/imperative mutation paths ("emergency" tiers become the norm); replacing LangGraph with Temporal today (defer — see gates).

---

## 2. Target system, layer by layer

```
┌──────────────────────────────────────────────────────────────────┐
│ CLIENTS  Web UI (Next.js SPA) · REST · (later: CLI/Slack)       │
├──────────────────────────────────────────────────────────────────┤
│ API — FastAPI, stateless, N workers                              │
│  auth→org/user resolution · require_initiator on /chat          │
│  require_approver+4-eyes on /approvals · authorize_run on reads │
│  credential reveal: owner/approver + fresh-auth + audit          │
├──────────────────────────────────────────────────────────────────┤
│ INTELLIGENT SHELL (LLM reasoning — LangGraph/create_agent core) │
│  Router (context-aware) · Governed Executive Loop (goal→DAG→    │
│  observe→adapt, bounded) · Read-only Investigation Agents        │
│  (SRE/knowledge/discovery; sub-agent spawning OK)                │
│  Context Engine: retrieval·compression·memory·routing·verify    │
├────────────────────────── the boundary ─────────────────────────┤
│ GOVERNED CORE (deterministic — unchanged invariants)             │
│  execute_governed_step → approved module → validate → plan →    │
│  plan_guard → durable approval interrupt → apply (isolated,     │
│  remote-locked state) → verify (cross-cloud) → record (same-txn)│
├──────────────────────────────────────────────────────────────────┤
│ HARNESS  Redis Streams event bus · RunSupervisor (heartbeat) ·  │
│  Stranded-run Reconciler · idempotency wait-or-abort ·          │
│  LLMProvider factory (honest model selection)                    │
├──────────────────────────────────────────────────────────────────┤
│ WORLD MODEL  Neo4j: live infra graph (resources, dependencies,  │
│  runs, incidents) + Reconciliation Engine (actual vs recorded,  │
│  drift detection, orphan sweep)                                   │
├──────────────────────────────────────────────────────────────────┤
│ DATA  Postgres (authoritative: runs/messages(+embeddings)/       │
│  approvals/resources/audit/user_memory) · Redis (ephemeral) ·   │
│  pgvector (RAG + conversation retrieval) · object refs for      │
│  offloaded artifacts                                              │
├──────────────────────────────────────────────────────────────────┤
│ OBSERVABILITY  Langfuse (trace_id=run_id) · OTel→Prom/Grafana · │
│  real Traces tab (run_steps-derived + Langfuse deep-link)        │
└──────────────────────────────────────────────────────────────────┘
```

### 2.1 The Governed Core (keep + harden)
- **Keep LangGraph** as the durable checkpointed state machine. The verdict from the harness analysis stands: the failures are in the wrapper layers, not the engine; the Postgres-checkpointed approval interrupt is the one thing that must never be rebuilt on a pre-1.0 dependency.
- **Terraform strategy hierarchy (locked):** (1) approved enterprise module → (2) approved internal template. That's it at runtime. No tier 3/4/5 at execution time. New capability enters ONLY via the Module Promotion Pipeline (§2.5).
- Hardening (all from FIX.md, unchanged): idempotency wait-or-abort (A1); plan_guard re-asserted at the approval choke-point (A2); unique plan-file per run + **remote state backend with locking** (A3, answers the local-state/durability/disk critique); org-scoped dup check (A4); initiator recorded + 4-eyes for prod (A5); verification always terminates, cross-cloud (B4); real policy checks evaluated against plan JSON (U1); SRE remediation real or explicitly "proposed, not executed" (U2).

### 2.2 The Harness (fix the three broken layers)
- **Redis Streams event bus** replaces in-process `_channels` (B1): worker-agnostic streaming, exactly-once by stream id, TTL-evicted on terminal. Fixes the leak and enables horizontal scale.
- **RunSupervisor** (B2): tracked tasks + Redis heartbeat; graceful drain on shutdown.
- **Stranded-run Reconciler** (B3): periodic sweep of non-terminal runs with stale heartbeats → resume from checkpoint (idempotency makes it safe) or mark failed honestly.
- **No blocking I/O on the loop** (B6): thread-offload every sync SDK call (the boto3-in-coroutine bug; also the Gemini sync-constructor fix, P18). This resolves the "Gemini is synchronous" critique — the fix is targeted, not a rewrite.
- **LLMProvider abstraction** (U3): honest model selection; UI menu trimmed to what's real.

### 2.3 The Intelligent Shell — the Governed Executive Loop (the big addition)
Replaces/absorbs FIX.md U6 (bounded planner). This is where the Claude-Code feel lives.

- **Goal → DAG:** for a multi-step request ("VPC → EC2 → EFS → NGINX → ALB → Route53 → verify"), the executive loop drafts a **goal DAG** (each node a desired outcome mapped to an approved module + params, or a read-only verification).
- **One approval for the DAG:** the whole plan is the approval artifact — per-step plan summaries, policy checks, cost signal. The human approves the *plan*, not seven interrupts (approval fatigue is the ThinkPol failure mode).
- **Execute with observation:** deterministic code walks the DAG; each step goes through `execute_governed_step` (full core pipeline; per-step plan_guard); structured observations (new VPC id, mount-target status, health checks) feed back into the loop.
- **Adapt with deviation-gating:** on failure the loop may replan — but any step **not in the approved DAG** triggers a fresh approval interrupt. Approve the plan once; re-approve only surprises.
- **Bounded:** max steps, max replans per step, budget ceilings. Never an unbounded loop.
- **Build on stable primitives:** LangGraph/`create_agent` (LTS) for the loop itself; borrow deepagents *patterns* (todos/plan tool, context offloading, sub-agent spawning). Adopt the deepagents *package* only for read-only investigation agents where pre-1.0 churn is acceptable.
- **Read-only autonomy is unrestricted:** SRE triage, knowledge research, multi-cloud discovery run as loop-until-done agents with read-only tools (Prometheus, describe/list, RAG, GitHub reads, world-model queries). Sub-agent spawning allowed here — context isolation is genuinely valuable. Mutation is never delegated to a spawned agent.

### 2.4 The World Model (resolves D3 = INVEST)
Neo4j stops being a best-effort mirror and becomes load-bearing — the answer to the "Neo4j isn't storing a world model" critique:
- **Contents:** live cloud inventory (all clouds), Terraform state refs, resource dependency edges (VPC⊃subnet⊃EC2, SG attachments, DNS→ALB→targets), run/session provenance, incident↔deploy links (later).
- **Reconciliation Engine:** continuous compare of recorded vs actual (extends inventory.reconcile beyond AWS EC2 to all clouds/types); drift surfaced as first-class events; orphan detection (real resource, no inventory row — closes P14's spend leak).
- **Consumers:** the executive loop plans against it ("which VPC?" answered from the model, not asked); impact analysis ("what breaks if I destroy X?") gates destroys; memory verification (§2.5) grounds recall in reality.
- **Honest gate:** if after building this the graph queries stay 1–2 hops, fold into Postgres and drop Neo4j. The investment is conditional on the world model actually being used.

### 2.5 The Context Engine (five-layer memory) + Module Promotion
Memory, mapped to the five-layer doctrine (retrieval/compression/memory/routing/verification):
1. **Retrieval:** embed every message on write (pgvector, existing infra); top-k semantic retrieval over the session + deterministic `get_turn` positional recall ("my 20th question" → verbatim). Retrieval also spans operational history and runbooks. (FIX M2 — the honest fix for the lossy-recall critique; note the current failure is *under*-sending, not costly over-sending.)
2. **Compression:** rolling LLM summary of older turns (replaces 160-char truncation) + **context offloading** — plans/logs/discovery dumps live in storage as references, not in the prompt; agents fetch on demand.
3. **Persistent memory:** per-user/per-org standing context (preferences, naming conventions, defaults, decisions) — AegisOps's CLAUDE.md equivalent; org/user-scoped under S0. (FIX M4.)
4. **Routing:** `build_context(session, budget, purpose)` — purpose-shaped slices: router gets summary+retrieval (fixes the 8-turn window); CloudOps gets inventory+user memory+prior params; SRE gets telemetry+runbooks; the executive loop gets goal DAG+observations. Threaded into EVERY LLM call (FIX M1/M3).
5. **Verification:** memory answers are store-grounded (inventory rows, message rows), never LLM recall; the Reconciliation Engine verifies remembered state against actual cloud state — self-verifying memory, a differentiator no chat product has.

**Module Promotion Pipeline (new, Phase 3):** when no approved module exists, the agent may DRAFT one — generate → `fmt`/`validate` → Checkov/tfsec scan → open as a **proposal for platform-engineer review** (PR-style). Only after human promotion does it join the approved library. Generation and execution never happen in the same turn. This grows organizational IaC knowledge (the good half of the strategy-hierarchy idea) without ever letting runtime-generated HCL touch a cloud.

### 2.6 Security & tenancy (unchanged from FIX.md — first, non-negotiable)
S0 real multi-tenancy (org from principal, `Session.user_id`, org predicates everywhere) → S1 credential reveal (owner/approver + org + mandatory fresh-auth + always-on audit) → S2 read/stream authorization (404 on mismatch) → S3 `/chat` initiator gate → S4 persist-time redaction backstop → S5 execute-node capability assertion. Plus honesty labels (P7/P8/P9) so no surface lies to an approver even before the real implementations land.

---

## 3. Execution phases

**Phase 1 — Trustworthy (blocking; mostly S/M items).**
S0–S4 security · A1/A2/A4/A5 safety · B5/B6 reliability · D1 indexes · D4 repo/state hygiene · U4 ask-which-cloud · honesty labels on P7/P8/P9 (cheap label now, real later) · O2 Langfuse project assert. Exit: two orgs fully isolated; no double-apply; no un-gated secret; no surface claims what it didn't do.

**Phase 2 — Production harness + context engine.**
B1 Redis bus · B2 supervisor · B3 reconciler · A3 remote locked TF state · latency pass (init-skip, plugin cache) · M1/M2/M3 context engine core · U1 real policy checks · U2 real/labeled SRE · O1 real Traces tab · U3 LLMProvider · D2 same-txn inventory + orphan sweep · B4 cross-cloud verify. Exit: survives worker kill mid-apply; recalls turn 20 of 100 verbatim; multi-worker streaming; policy checks evaluate the actual plan.

**Phase 3 — Intelligence layer (the competitive edge).**
World Model + Reconciliation Engine (D3=invest, conditional gate) · Governed Executive Loop (goal DAG, one-approval, deviation-gating, bounds) · read-only investigation agents (deepagents-pattern; package adoption allowed here) · Module Promotion Pipeline · M4 user/org memory · U7 retry-with-fix + undo · modify-beyond-ports · cost estimation feeding policy checks.

**Decision gates (explicit, owner-signed):**
- **Temporal:** trigger = when DAG workflows become long-running (hours+), high-fan-out, or need versioned migrations mid-flight. Until then LangGraph+PG checkpoint suffices. Revisit at Phase-3 exit with real workflow data.
- **Neo4j:** invest now (Phase 3), but fold-to-Postgres if world-model queries stay shallow after 1 quarter of real use.
- **deepagents package:** read-only agents only; re-evaluate for wider use at 1.0/LTS.

---

## 4. Why this wins (the honest competitive claim)
Claude Code has the loop but no governance, no multi-tenant control plane, no world model, and no organizational memory — it regenerates from scratch every session and will happily run a destroy plan a tired human approves. Big-4/cloud-native tools have governance but no conversational agent and no cross-cloud loop. AegisOps after Phases 1–3 is the only shape that has all four in one system: **a Claude-Code-grade adaptive loop, behind a deterministic mutation boundary, over a live self-verifying world model, with memory that compounds into organizational knowledge.** The pitch is earned at Phase-3 exit — not claimed before.
