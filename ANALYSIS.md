# AegisOps — Ground-Truth Engineering Analysis

> **Scope & method.** This document is built by reading **every source file in the repository**, not from `PROGRESS.md`. `PROGRESS.md` was used only as an orientation pointer; where code and `PROGRESS.md` disagree, the **code wins** and the discrepancy is called out. Every claim is grounded in a real file with a `path:symbol` or `path:line` citation. Nothing in this pass modified application code — the only files created are these analysis documents.
>
> Analysis date: 2026-07-07. Branch: `feature/cloudops-v1`. Commit at read time: `6b45eec CloudOps V1 changes`.

The analysis is split across `docs/analysis/` because it is large. This file is the index and the executive summary; open the linked files for the detail.

## Table of contents

| # | Document | What's in it |
|---|----------|--------------|
| 00 | *(this file)* | Executive summary + the 10 most important findings |
| 01 | [Architecture](docs/analysis/01_architecture.md) | Component map, request lifecycle, SSE contract, LangGraph graph, deployment topology |
| 02 | [Technology choices](docs/analysis/02_technology_choices.md) | Why each tech, critical assessment, keep/reconsider/replace verdicts |
| 03 | [AWS operations](docs/analysis/03_aws_operations.md) | EC2/S3/RDS/VPC/EKS — inputs, full call chain, outputs, failure paths |
| 04 | [Azure operations](docs/analysis/04_azure_operations.md) | VM/Storage/Resource Group/PostgreSQL/AKS |
| 05 | [GCP operations](docs/analysis/05_gcp_operations.md) | Compute/GCS/Cloud SQL/GKE |
| 06 | [Memory & continuity](docs/analysis/06_memory.md) | Conversational memory, the "message #20 in a 100-message thread" test, resource memory, cross-session recall |
| 07 | [Datastores](docs/analysis/07_datastores.md) | Postgres / Redis / Neo4j — schema, who reads/writes, how they interact, consistency risks |
| 08 | [Observability, security, RBAC](docs/analysis/08_observability_security_rbac.md) | Langfuse/OTel/Prometheus wiring, redaction, Keycloak OIDC + RBAC enforcement points |
| 09 | [Serious problems](docs/analysis/09_problems.md) | Ranked, evidence-backed defect list with fixes (the most valuable section) |
| 10 | [Seamless-agent gap analysis](docs/analysis/10_gap_analysis.md) | Where AegisOps falls short of Claude Code / ChatGPT / Antigravity, and what to change |
| 11 | [Input/Output reference](docs/analysis/11_io_reference.md) | "What to type / what you get" cheat sheet per operation |
| 12 | [Roadmap to production-grade](docs/analysis/12_roadmap.md) | Phased plan: must-fix → competitive → nice-to-have |
| 13 | [Files read (coverage checklist)](docs/analysis/13_files_read.md) | Every directory and file inspected for this analysis |

---

## 1. Executive summary

**What AegisOps is.** An AI-native, multi-cloud "agentic AIOps" platform. An engineer types an intent in natural language; a FastAPI backend runs it through a **LangGraph** multi-agent graph (Router → CloudOps / DevOps / SRE / Knowledge / General, plus Approval / ServiceNow / Notify sub-nodes) using **Google Gemini** as the reasoning engine. CloudOps selects a **curated Terraform module**, collects and validates parameters, runs read-only cloud-SDK discovery, produces a real `terraform plan`, and **pauses at a human-approval interrupt** before any `apply`/`destroy`. The whole run streams to a pixel-exact Next.js UI over SSE, and is recorded in Postgres, a Neo4j context graph, Langfuse traces, and Prometheus metrics. Auth is real Keycloak OIDC + RBAC.

**Current maturity.** This is a genuinely substantial, mostly-real build — far past a prototype. The core loop (chat → classify → collect params → plan → approve → apply → verify → record) is implemented end-to-end with real integrations, not mocks. The safety engineering around the destructive path is unusually careful (multiple independent guards; per-resource Terraform state isolation). But it is **not yet production-grade**: it has a hard multi-tenancy gap, an RBAC hole on credential reveal, a single-process streaming design that breaks under horizontal scaling, several "decorative" surfaces that look real but aren't (policy checks, the Traces tab, SRE remediation), and dead code paths. See §8/09 for the ranked list.

### The 10 findings that matter most

**Good — real and well done**
1. **The destructive-safety architecture is real and layered.** Per-resource Terraform state workspaces (`TF_WORKSPACE=res-<slug>`, `tools/terraform.py:state_slug` + `ensure_state_workspace`) genuinely fix the "a second create destroyed my first instance" class. On top of that sit a deterministic regex intent guard (`agents/intent_guard.py`), an action-vs-plan hard guard (`agents/plan_guard.check_plan_actions`), and a same-name-create refusal (`agents/cloudops.py:314`). This is the strongest part of the codebase.
2. **The human-approval interrupt is a true LangGraph durable interrupt**, not a UI fiction — `agents/approval.py:interrupt()` pauses a Postgres-checkpointed graph and `POST /approvals/{id}` resumes the *same* `thread_id` (`agents/runner.py:63`), so it survives an API restart.
3. **Terraform is the only thing that mutates; the LLM never authors HCL.** All 14 modules are pre-written, version-pinned, secure-by-default (IMDSv2, encrypted volumes, TLS floors, private endpoints, sensitive-output credentials). User input reaches Terraform strictly as `-var` (`tools/terraform.py:_var_args`). The runtime-HCL escape hatch that once existed was removed.

**Bad — must fix before it can be trusted**
4. **Multi-tenancy is effectively fictional.** Every endpoint resolves the org via `repo.get_default_org()` (the single oldest org) and ignores the authenticated user's org (`api/*.py`, `db/repositories.py:get_default_org`). `Session.user_id` is never populated on chat (`api/chat.py:117`). "Org-scoped" is real at the row level but there is only ever one org and no per-user scoping. `CLAUDE.md` claims "Multi-tenant: org-scope every query" — the code does not deliver this.
5. **RBAC hole on credential reveal.** `POST /runs/{id}/credentials` — which returns a VM's **private key / admin password** — is gated only by `get_current_user`, not by an approver/owner check, and does no org/ownership verification of the run (`api/artifacts.py:209`). Any authenticated user (including a read-only auditor) who knows/guesses a `run_id` can exfiltrate that run's secret. This directly contradicts the "one-time, RBAC'd" claim.
6. **No session/run authorization.** `GET /sessions/{id}/messages`, `GET /runs/{id}`, all `GET /runs/{id}/{tab}`, and `GET /chat/stream/{id}` require only authentication, with no check that the caller owns or belongs to that session/run (`api/sessions.py:59`, `api/chat.py:228`, `api/artifacts.py`). Combined with #4, this is a latent cross-tenant data-leak the moment a second org exists.
7. **Streaming state is in-process only.** SSE channels live in a module-global dict (`agents/events.py:_channels`) and are **never evicted** (`drop_channel` is defined but only called in a test) — an unbounded memory leak — and reconnect/replay only works if the retry hits the same worker. Any multi-worker/multi-replica deployment breaks streaming and approval-resume. `README`/`CLAUDE.md` claim a "stateless API".
8. **Several surfaces look real but are theater.** Policy checks are hardcoded `True` assertions (`agents/templates.py:_s3_policy` … `_ec2_policy`), not evaluated against the plan. The artifact **Traces tab** returns static span names with `—` durations (`api/artifacts.py:184`), even though real Langfuse traces exist. **SRE remediation** claims `{"applied": True}` after only listing deployments — it never rolls back/scales/restarts (`agents/sre.py:146`). The SRE decision matrix runs on a hardcoded `recent_deploy: True` signal (`agents/sre.py:53`).

**Structural / architectural**
9. **Model-swappability is a myth as shipped.** The frontend model selector offers Claude/GPT/Gemini/Llama and defaults to "Gemini 2.5 Pro" (`frontend/lib/data.ts:42`, `frontend/lib/store.ts:139`), but `POST /chat` **never reads `body.model`** — the backend always uses `GEMINI_MODEL` (`gemini-3.5-flash`) via a global singleton (`integrations/gemini.py:get_gemini`). There is no provider abstraction; switching models is an env change + restart, not a per-request choice.
10. **The "ask which cloud" safety behavior is largely unreachable from the real UI.** `resolve_cloud` falls back to the UI cloud selector, and both `ChatContext.cloud` (`api/chat.py:40`) and the Zustand store (`frontend/lib/store.ts:137`) default to `"AWS"`. So "provision a virtual machine" with no cloud named resolves to AWS via the selector, not to a clarifying question — the documented ambiguity guard only fires in tests / API calls that send `cloud=null`.

**One correctness bug worth surfacing here** (full list in 09): `cloudops_execute`'s idempotency guard executes anyway when a claim is in-flight-but-not-done — `if not claim: done = get_result(); if done: return` falls through to a second `apply` when a concurrent run holds the claim but hasn't stored a result yet (`agents/cloudops.py:935`). And `inventory.reconcile` calls blocking `boto3` directly on the event loop (`agents/inventory.py:229`).

### Honest scorecard (code-verified, not PROGRESS-verified)

| Area | State in code |
|------|---------------|
| Chat → classify → plan → approve → apply → verify → record | **Real, end-to-end** for AWS EC2/S3 (apply-verified path exists in code + fixtures) |
| Multi-cloud plan generation | **Real** — 14 pinned modules, cloud-safe selection, no cross-cloud fallback |
| Human approval (HITL) | **Real** durable interrupt + immutable DB/graph record |
| Conversational memory | **Real but lossy** — DB transcript threaded in; older turns digested/truncated (see 06) |
| Resource memory (inventory + graph) | **Real** — Postgres `resources` + Neo4j mirror, reconciled via SDK (EC2 only) |
| Observability (Langfuse span tree) | **Real** — trace-id == run-id, nested spans, generations with cost |
| RBAC | **Partial + one hole** — approver gate on `/approvals`, but reveal-credential + read endpoints under-guarded |
| Multi-tenancy | **Not implemented** — single default org, no per-user scoping |
| DevOps agent | **Real GitHub calls**, but CI "verify" is a single status read (no polling); K8s deploy needs a user-supplied image |
| SRE agent | **Partial/decorative** — triage + RAG real; telemetry signal hardcoded; remediation is a no-op |
| Policy engine | **Decorative** — hardcoded `True` checks, no OPA/real evaluation |
| Stateless / horizontally scalable | **No** — in-process SSE channels, no channel eviction |
| Tests | **Substantial and real** — ~50 backend test functions across unit + live-datastore integration tiers, frontend vitest + Playwright; but they run against a single org and don't cover the tenancy/authorization gaps |

The rest of this document backs every one of these statements with the code.
