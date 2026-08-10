# 12 — Phase Roadmap: Operator Narrative & Implementation Understanding

> **Status:** ORIENTATION DOCUMENT — recorded 2026-08-10 from the operator's phase-roadmap
> briefing, together with the implementing agent's grounding analysis against the frozen
> architecture. This document introduces **no new architecture** and authorizes **no
> implementation**. Authority order is unchanged: 00 wins on boundaries, 07 on sequence and
> gates, 08 on decisions, 11 on migration state; where this document's narrative phrasing and
> 07/11's item-level assignments differ, **07/11 govern** (the differences are catalogued in §6).
>
> **Purpose:** every future phase prompt will be executed against this shared understanding —
> what each phase is *for*, what proves it *done*, and what must never be pulled forward.

---

## 1. The journey in one picture

```text
P0  Foundation / Safety / Evaluation            (DONE — accepted + committed 2026-08-10)
 ↓
P1  Multi-Provider LLM Substrate                (NEXT — P1 ENTRY READY, gate record 11 §28)
 ↓
P2  TRUE AGENT HARNESS — Observe → Reason → Act
 ↓
P3  Durable Execution / Workflow Engine
 ↓
P4  CloudOps + DevOps + SREOps Migration (the inversion)
 ↓
P5  Production Hardening / Enterprise Scale
```

The question each phase answers:

| Phase | Main question |
|---|---|
| **P0** | Can we safely evolve AegisOps? |
| **P1** | Can AegisOps use multiple models/providers properly? |
| **P2** | Can AegisOps genuinely reason, use tools, recover and continue? |
| **P3** | Can those actions execute durably and reliably? |
| **P4** | Can it operate AWS + Azure + GCP + GitHub + SRE systems like real engineers? |
| **P5** | Can enterprises safely run it at production scale? |

The strategic transformation:

```text
CURRENT: deterministic orchestration + 1,531-line CloudOps agent + single-pass execution
         + Gemini-centric LLM + weak memory + limited tool intelligence

TARGET:  Task/Run Manager → Agent Harness → Observe→Reason→Act
         (Memory · Models · Tools) → Policy/Budget → Workflow Engine
         → AWS | Azure | GCP capability packs + DevOps + SREOps
```

---

## 2. Phase-by-phase understanding (grounded in 00–11)

### P0 — Foundation (DONE)

Not intelligence — the things that make future intelligence **safe and measurable**: the
behavioral eval gate (rule zero substrate), the authoritative `llm_usage` ledger, governance
stamping, Redis coordination posture, worker-role foundation, defect sweep, security preflight,
the 998/786/53/159/0 baseline, and the migration control ledger (doc 11). Evidence:
`implementations/P0 Recovery and Implementation Report.md`, commits `31f7c16` + `3e6f971`.

**Standing operator decision (P1 entry gate):** Four-Eyes is completely removed. The approval
model is single-user Human-in-the-Loop — `Initiator → Plan → same user reviews → Approve/Reject`.
No phase may reintroduce second-approver semantics (11 §28.1).

### P1 — Multi-Provider LLM Substrate (NEXT)

Replace Gemini-centric access with a provider-neutral model layer:

```text
AegisOps → Canonical LLM API → LLM Service → Model Catalog/Registry → Provider Router
                                            → Gemini | Claude | OpenAI-compat adapters
```

Covers (07 P1.1–P1.9; canonical shapes normatively in 05 §11): `CanonicalMessage`,
`ModelRequest`/`ModelResponse`, `Usage`, `ServedBy`, `StreamEvent`, `ToolCall`/`ToolResult`,
`ModelError`; the Google adapter behind the boundary; `service.py` + byte-compatible
`agents/llm.py` shim with `purpose=` threaded from the 8 call sites; `models.yaml` catalog +
capability registry with boot validation; anthropic/openai_compat adapters with recorded-fixture
tests; deterministic routing + resilient executor (retry/jitter/Retry-After, Redis breaker,
two-stage failover with visible `fallback_hop`, governed purposes never silent-fallback, budget
gate); `model_bindings` + Settings UI + served-by badges; the **native tool-calling substrate**
(structured output for router/extract; native FC verified per adapter, opt-in canary); the
import-linter boundary (SDKs only in adapters; `langgraph.*` confined).

**P1 deliberately does NOT make AegisOps intelligent.** P1 routing is model/provider selection
and resilience only — never Observe→Reason→Act, never retry-with-different-approach. That
distinction is the #1 boundary to police in the P1 diff.

### P2 — The Real Agent Harness (the biggest architectural transformation)

This is where the original complaint — *"AegisOps doesn't think, observe failures, revise its
approach, and continue"* — is addressed. The kernel loop (04 §3, loop laws L1–L7):

```text
OBJECTIVE → context assembly → MODEL → tool call → tool result
  success → verify | failure → OBSERVATION → re-reason → NEW action → … → VERIFY → DONE
```

Introduces (04 + 06 + 07 P2.1–2.9): the harness kernel (`harness/loop.py` ≤500 lines),
purpose-routed model calls on the P1 substrate, native tool calling in anger, iterative
reasoning, **failed-tool-as-observation** (L3), hard iteration/cost/wall-clock budgets enforced
*inside* the loop, the policy pipeline, approval interrupts (kept constitution), verification
orchestration + stop conditions, per-iteration context assembly + compaction, subagents (typed
results, depth 1, shared pool), `run_events` + replay-resume for loops (P2.5), memory tiers +
retrieval gate + consolidation-to-proposals (P2.3/2.6), prompt registry (P2.8), SRE read-tool
expansion (P2.9). **Read paths only** (rule two): first callers are `sre._collect_telemetry` and
`cloudops._read_path`; mutation stays on the existing exec_loop/approval path.

Reference systems (Claude Code / Hermes / OpenClaw / Waku) supply **engineering patterns**, not
code — AegisOps governance reshapes all of them (00 §9).

### P3 — Durable Execution / Workflow Engine

P2 gives intelligence; P3 makes it **reliable across minutes/hours/failures/restarts**:

> P2 = "How should the agent think and act?"
> P3 = "How do we reliably execute that thinking across time and process death?"

Covers (06 §8, 07 P3.1–3.9): the durable Task/Run/Step model, the full status machine
(`applying` finally dead), exec_loop → `app/engine` with compile closures kept verbatim, waves +
saga/compensation, day-2 verb registry, K8s executor with dry-run diffs, change windows,
deviation taxonomy, real policy predicates (F-11), retries/resumability/background execution,
kill -9 mid-wave resume with no double-apply, approval/input waiting as durable parks, event
sourcing on `run_events`, cancellation and recovery, execution history.

### P4 — CloudOps + DevOps + SREOps Migration (the mandate's core move)

The current agents are transformed only **after** the harness (P2) and engine (P3) exist:

```text
P2 Harness → P3 Engine → shared Tool Registry → CloudOps | DevOps | SREOps capability packs
```

The signature change is **capability-neutral thinking**: the agent reasons *"I need compute"*,
never *"I need EC2"* — the cloud pack resolves capability → implementation. AWS, Azure and GCP
receive equal architectural treatment (00 §5; parity matrix 03 §3): the ten service families
(compute, object storage, managed DB, network, K8s, serverless containers, functions, identity,
telemetry, load balancing) across all three clouds. DevOps: repos, branches, PRs (PR-first,
direct default-branch pushes banned), Actions, log-download diagnosis, registries, releases.
SREOps: logs/metrics/traces/alerts investigation, diagnosis playbooks, gated remediation,
verification, rollback.

The behavioral bar is the EKS+GitHub flow (00 §8 / 02 §8): understand objective → discover →
plan → approval → execute → observe failure → change hypothesis → corrective action → verify →
deploy → diagnose → fix → verify → evidence-backed report. `cloudops.py` (1,531 LOC) dissolves
at P4.2; the loop becomes the spine at P4.3 — **dark-launched, eval-parity-gated on both
topologies** (07 risk #1), with ADR-12 human sign-off before P4.

### P5 — Production Hardening / Enterprise Scale

Credential broker (`global credentials → broker → tenant/environment-scoped credentials →
provider`, ADR-17), multi-tenant credential isolation, final three-cloud parity gate in CI
(P5.1), DevOps completion (P5.2), alert→incident pipeline (P5.4), Slack/Teams channels,
dashboards + the uncharted metrics, drift expansion, enterprise controls, SLOs, and the **final
removal of transitional architecture** (the ADR-04 LangGraph gate and ADR-06 Neo4j fold-in are
both measured/decided in this era).

---

## 3. The control system that keeps phases honest

The operator runs **three levels of control**; the implementing agent must expect and support
all three:

1. **Architecture checklist (before each phase prompt).** The operator cross-checks the complete
   00–11 package + diagrams/Mermaid sources + the latest implementation reports + the control
   plan before issuing the next prompt — requirements are never reconstructed from memory.
2. **Hard phase acceptance gates.** Each phase exits only through its full gate set (functional,
   integration, API, frontend, behavioral, observability, GitNexus, runtime smoke, architecture
   boundary, regression — doc 11 §13's ten points). Only then does the next phase begin.
3. **Behavioral acceptance is the real proof.** *"AgentHarness class exists"* is never
   acceptance. The standard, verbatim:

   > **Does the resulting system demonstrate the behavior that the phase was supposed to
   > introduce, while preserving everything that already worked?**

   The canonical intelligence proof (10 IP-1..4): inject `Tool A → failure` → the agent records
   the **observation** → **changes hypothesis** (machine-comparable `H(i+1) ≠ H(i)`) → takes a
   **different action** (`Tool B`, different evidence family) → **success**, asserted over
   `run_events` rows, never over model prose. A deterministic workflow that merely retries the
   same thing must **FAIL** the intelligence test (IP-4 is the negative oracle).

Every phase ends with `Redesign/implementations/P<n> Implementation Report.md`, and
`11-Implementation-Control-Plan.md` stays the living migration ledger, updated *during* the
phase, not reconstructed after it.

---

## 4. Standing constraints the roadmap re-affirms

- **Strangler, never big-bang:** OLD → compatibility shim → NEW → parity → consumer migration →
  verification → remove OLD (11 §9). Nothing is deleted because its replacement exists.
- **The constitution (00 §7) survives every phase:** Terraform-only catalog mutation, durable
  approval interrupt, plan guard, strict tenancy/RBAC, single-user HITL, idempotency,
  boundary-only cancel, honest partials, redaction, trace==run, immutable records.
- **No phase pulls a later phase forward.** Discovered later-phase work goes to the deferred
  register (11 §21), never implemented opportunistically.
- **Failures are classified** (PRE_EXISTING / INTRODUCED_BY_PHASE / ENVIRONMENT_ONLY) against
  the accepted baseline; baseline failures are never hidden or "fixed" incidentally.
- **LangGraph is reduced and isolated, never replaced ad hoc** — its end-state is a measured,
  human-signed gate after P4 (ADR-04).

---

## 5. Where the narrative's phrasing and 07/11 differ (nuance register)

The operator narrative groups some items differently than 07's item-level assignments. None of
these are conflicts — the narrative describes *capability eras*, 07 describes *PR sequencing* —
but future prompts should not cite the narrative against the plan:

| # | Narrative says | 07/11 item-level truth |
|---|---|---|
| N-1 | P2 includes "durable task/run state" and "background tasks" | Durability *for loops* arrives at P2.5 (`run_events` + replay-resume, read paths); the full durable Task/Run/Step model, background execution ownership and the status machine are **P3** (06 §8.4, 07 P3.1/3.6) |
| N-2 | P2 includes "plan mode" and "permission modes" | The policy *pipeline* + PLAN_ONLY-compatible behavior land at P2; the full four-mode matrix (READ_ONLY/PLAN_ONLY/APPROVAL_REQUIRED/AUTONOMOUS) + ESTOP is **P4.5** (09 R9) |
| N-3 | P2 includes "episodic/semantic memory" | Correct — P2.3 (gate) / P2.6 (tiers + consolidation-to-proposals); *procedural* memory (skills via PR + eval gate) is P5-era (06 §1) |
| N-4 | P4 lists IAM among cloud capabilities | IAM **reads** yes; IAM C/U stays deliberately ◐ — `destructive` risk class, mandatory human approval regardless of mode (03 §3.4 rule 6); "secrets/configuration where governed" for DevOps likewise stays inside the governed mutation path |
| N-5 | P4 covers the three-cloud capability tables | The *architecture* becomes capability-neutral at P4; the **full three-cloud read/verify parity gate** completes at **P5.1** (per-row parity rule, 03 §3.4 rule 2) |
| N-6 | P1 lists "additional approved providers" | Exactly two beyond Google in P1: `anthropic_` + `openai_compat` with recorded-fixture contract tests (07 P1.5); bedrock/azure_openai are target wire families, litellm ships disabled (ADR-14) |
| N-7 | "P0 covered Human-in-the-Loop approval" | HITL existed pre-P0 (constitution); P0 aligned the posture + stamped it; the four-eyes **removal** happened at the P1 entry gate (11 §28), not inside P0 |

---

## 6. Current state marker (at the time of recording)

- P0: DONE — accepted + committed (`31f7c16`), gates 10/10.
- P1 entry gate: DONE (`3e6f971`) — four-eyes removed entirely; C-01 (05 §11 contracts) and
  C-07 (unpartitioned `llm_usage` + triggers) resolved; migration 0010 applied and verified on
  the dev DB; verdict **P1 ENTRY READY**.
- P1 implementation: **NOT started as an accepted phase.** (A prior session briefly began P1
  scaffolding — canonical contracts, Google adapter, catalog/router skeletons and contract
  tests under `app/llm/` — before the operator paused it for this roadmap-analysis exercise.
  Those files sit uncommitted in the working tree, gate-tested at the P1.1 level only, and
  carry no authority until a P1 prompt re-authorizes the phase.)
- P2–P5: FUTURE. Understood, mapped, and deliberately untouched.
