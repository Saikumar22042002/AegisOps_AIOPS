# CLAUDE.md — AegisOps (read me every session)

You are building **AegisOps**, an enterprise, commercially-deployable agentic AIOps platform.
Build it as if it ships to customers on day one.

## Prime directives
1. **UI = pixel-exact replica** of `design-reference/AegisOps_Workspace_v3.SOURCE_OF_TRUTH.html`
   in dark, light, and mobile. Never redesign. Tokens/fonts/animations/responsive are extracted
   in `design-reference/DESIGN_REFERENCE.*` — copy verbatim; use only those CSS variables.
2. **Everything is real. No mocks, stubs, placeholders, TODOs, or "future implementation."**
   Every function fully implemented with real SDKs/integrations. The only external inputs are
   runtime credentials in `.env` and the services from `docker-compose`. If a service is
   unreachable at runtime, fail loudly with a clear error + structured log — never fall back to
   fake data.

## Stack (fixed)
- Frontend: Next.js 14 (App Router) + React 18 + TypeScript + Tailwind + Zustand; native SSE.
- Backend: FastAPI + Pydantic v2 + SSE (`sse-starlette`); SQLAlchemy 2 + Alembic.
- Agents: **LangGraph** multi-agent (Router → CloudOps/DevOps/SRE/Knowledge/General + Approval/
  ServiceNow/Notify), durable checkpointing, interrupts for human-in-the-loop, native
  tool-calling. **Gemini `gemini-3.5-flash`** is the LLM inside the agents (real, current GA
  model; alias `gemini-flash-latest`).
- Data: PostgreSQL+pgvector, Redis, Neo4j (context graph) — all via docker-compose.
- Auth: Keycloak OIDC + RBAC (8 roles). Observability: Langfuse + OpenTelemetry + Prometheus +
  Grafana. Infra: real Terraform (create/modify/destroy) + Ansible; cloud SDKs (boto3/azure/
  gcp/pyVmomi/kubernetes) for **read-only** discovery/availability/verify only — never to
  provision. GitHub + ServiceNow real clients.

## Hard rules
- **No infrastructure change without passing the human-approval interrupt.** Read-only ops run
  freely. Support execution modes: dry_run / plan / apply(approval) / destroy(approval).
- Terraform does create/modify/destroy. Cloud SDKs do discovery/verification/telemetry only.
- Secrets only via env; never hard-code. `.env`, `terraform.tfstate*` in `.gitignore`. The
  ServiceNow password from the source doc is leaked — never reproduce it; operator rotates it.
- RBAC enforced at every endpoint and per tool, not just in the UI.
- Confidentiality badge on every agent message (real classifier). Analysis tab shows a
  privacy-safe reasoning summary, **not** raw chain-of-thought. Mask secrets in all output.
- Idempotency keys on every tool execution; durable checkpoints; resumable after restart.
- One Langfuse trace + OTel spans + context-graph nodes per run; structured JSON logs with
  correlation ids; never log secrets.
- Multi-tenant: org-scope every query. Stateless API (state in PG/Redis/Neo4j).

## Workflow
- Follow the phased plan in `docs/01_REQUIREMENTS.md §5`. After each phase, run it and verify
  against the source HTML + `docs/06_FEATURE_CHECKLIST.md` before moving on.
- Keep `PROGRESS.md` mirroring the checklist; check items off as you go.
- Write real tests alongside features (pytest + testcontainers, Vitest+RTL, Playwright). No
  skipped tests, no bare `except: pass`, real error handling + retries + timeouts everywhere.

## Done means
A fresh clone: fill `.env` → `docker compose up -d` → `make migrate && make seed` → `make dev`
→ app runs; UI indistinguishable from the source HTML; a real provisioning request runs
Router→CloudOps→discovery→`terraform plan`→approval→`terraform apply`→verify with full
context-graph + ServiceNow + Langfuse/OTel/Prometheus; every checklist box passes; all tests
green; no TODO/mock/placeholder in app code.
