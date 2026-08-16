# Brainstorming — AegisOps Redesign Blueprint

A practical, code-grounded blueprint for rebuilding AegisOps into a production-grade,
**provider-agnostic** enterprise CloudOps AI platform. Current-state claims are audited
against commit `a974290` (branch `feature/cloudops-v3`, 2026-08-03); Waku claims come
from a first-hand source read of `waku-agent`. Nothing here is aspirational hand-waving:
every proposal names the files it keeps, the files it replaces, and the tests that
prove it.

## The thesis in three sentences

1. AegisOps' governance core (Terraform-only mutation, durable approval interrupts,
   strict tenancy, plan guards, idempotent resumable execution) is the moat — **keep
   it untouched**.
2. Its reasoning layer is the bottleneck: one Gemini singleton behind a
   validation-only seam, zero native tool-calling, zero iteration, no cost ledger, no
   behavioral eval gate.
3. The fix is three structural moves — a **Provider Layer** (`app/llm`), an **Agent
   Kernel** (`app/harness`), and a **Workflow Engine** (`app/engine`) — each adopted
   strangler-style behind an eval gate, so switching LLMs becomes a UI action and
   complex CloudOps workflows become approvable, resumable, rollback-capable DAGs.

## Reading order

| Doc | What it answers |
|---|---|
| [Architecture_Analysis.md](Architecture_Analysis.md) | What exists today (audited), what Waku does better, what the modern harness field (Claude Code, Codex, Cursor, OpenHands, Hermes, pi, Antigravity) teaches, and the adopt/adapt/avoid/redesign verdicts |
| [Proposed_Architecture.md](Proposed_Architecture.md) | The target layered system; what stays/moves/dies; package layout; data-model deltas; rejected alternatives |
| [Agent_Harness.md](Agent_Harness.md) | The provider-agnostic runtime: canonical types, adapters, model router, capability registry, reasoning normalization, streaming, retry/fallback, usage ledger, UI model switching with zero agent changes |
| [CloudOps_Harness.md](CloudOps_Harness.md) | The governed execution engine: step contract, waves, approvals + deviations, saga rollback, day-2 verbs, change windows, incident remediation, and the named workflows (VM lifecycle, VPC+VM, VM+S3, multi-resource TF, K8s) mapped concretely |
| [System_Design.md](System_Design.md) | End-to-end runtime flows, the bounded multi-agent model, scaling, failure matrix, security enforcement points, design patterns used and rejected |
| [Architecture_Diagrams.md](Architecture_Diagrams.md) | The 8 Mermaid diagrams (current architecture + harness, Waku harness, proposed architecture + harness, request flow, execution flow, multi-agent flow) |
| [Implementation_Roadmap.md](Implementation_Roadmap.md) | Phases 0–4 with sizes and exit criteria, decision gates (Temporal, LiteLLM, Neo4j), risk register |

## The invariants (every doc holds these)

Terraform-only mutation through the approved catalog · durable human-approval
interrupt (Postgres-checkpointed, resumable cross-process) · plan_guard re-asserted at
the choke-point · strict tenancy, RBAC, four-eyes · per-step idempotency · cancel at
boundaries, never mid-apply · honest partial reporting · redaction on every egress ·
trace_id == run_id · **the LLM proposes data; deterministic code executes it**.

## Glossary

- **Purpose** — the string an agent binds to instead of a model (`router`, `planner`,
  `inv_loop`…); the only coupling between agents and models.
- **RoutePlan** — resolved model + fallbacks + params for a purpose, pinned per run.
- **GoalDAG / Workflow** — the planner's proposed steps → compiled, validated,
  approvable execution plan.
- **INV loop** — the bounded, read-only, model-directed investigation loop (Waku's
  loop inside AegisOps' frozen registry).
- **Day-2 verbs** — governed SDK lifecycle actions (stop/start/restart) that Terraform
  can't express, in one audited registry.
- **EvidenceCard** — what verify produces instead of a boolean: the SDK reads and
  probes that prove a step did what it claims.
- **Split-trust** — Intelligent Shell (LLM, zero mutation authority) / Governed Core
  (deterministic execution); the platform's constitution.
