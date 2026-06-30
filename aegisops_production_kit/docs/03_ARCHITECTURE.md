# 03 — Architecture

Enterprise, containerized, horizontally scalable. Stateless API workers; all state in
Postgres / Redis / Neo4j. Everything below is **real** and built in full.

---

## 1. Service topology (all via `docker-compose`, see `07_SETUP_AND_RUN.md`)

```
┌────────────┐     OIDC      ┌────────────┐
│  Frontend  │◀────────────▶│  Keycloak  │  (auth, realms, roles)
│ Next.js 14 │               └────────────┘
└─────┬──────┘
      │ REST + SSE (JWT)
┌─────▼───────────────────────────────────────────────────────────┐
│                       FastAPI API (stateless)                     │
│  routers: auth, chat(SSE), sessions, feedback, approvals,         │
│           artifacts, modules, integrations, knowledge, console    │
│  ┌───────────────── LangGraph runtime ─────────────────┐          │
│  │ Router → CloudOps / DevOps / SRE / Knowledge / Gen   │          │
│  │ + Approval, ServiceNow, Notification sub-graphs      │          │
│  │ checkpointer: Postgres (+ Redis for ephemeral)       │          │
│  └──────────────────────────────────────────────────────┘         │
│  integration clients: Gemini, ServiceNow, GitHub, boto3/azure/gcp, │
│   pyVmomi, kubernetes, TerraformRunner, AnsibleRunner, Prometheus  │
│  observability: Langfuse SDK + OpenTelemetry SDK                   │
└───┬────────┬─────────┬──────────┬──────────┬───────────┬──────────┘
    │        │         │          │          │           │
┌───▼──┐ ┌───▼──┐ ┌────▼───┐ ┌────▼────┐ ┌───▼────┐ ┌────▼─────┐
│Postgres│ Redis │  Neo4j  │ Langfuse │  OTel   │ Prometheus│
│+pgvector│cache │ context │ tracing  │Collector│  +Grafana │
│ app+RAG │queues│  graph  │          │         │ dashboards│
└────────┘└──────┘└─────────┘└─────────┘└────────┘└──────────┘
```

External (operator-provided creds in `.env`): Google Gemini API, AWS/Azure/GCP, vCenter,
Kubernetes clusters, ServiceNow instance, GitHub.

## 2. Repo layout (monorepo)

```
aegisops/
├─ docker-compose.yml            # full local prod stack
├─ Makefile                      # up/down/migrate/seed/dev/test/lint
├─ .env.example  .gitignore  README.md
├─ infra/
│  ├─ keycloak/realm-export.json # realm, clients, roles imported on boot
│  ├─ grafana/dashboards/*.json  provisioning/*
│  ├─ prometheus/prometheus.yml
│  ├─ otel/otel-collector.yaml
│  └─ terraform-workspaces/      # real TF modules used by CloudOps (eks, vpc, rds, etc.)
├─ frontend/                     # Next.js 14 App Router + TS + Tailwind + Zustand
│  ├─ app/ (login, (app)/workspace, modules/*, layout, providers)
│  ├─ components/ (per 02_DESIGN_SPEC §6)
│  ├─ lib/ (api client, sse client, auth/oidc, store, rbac, theme)
│  ├─ styles/globals.css         # tokens copied verbatim
│  └─ tests/ (vitest, playwright)
├─ backend/
│  ├─ app/
│  │  ├─ main.py  settings.py  logging_conf.py  otel.py
│  │  ├─ api/ (auth, chat, sessions, feedback, approvals, artifacts,
│  │  │        modules, integrations, knowledge, console, health)
│  │  ├─ agents/ (graph.py, state.py, router.py, cloudops.py, devops.py,
│  │  │           sre.py, knowledge.py, general.py, approval.py,
│  │  │           servicenow_agent.py, notify.py, checkpointer.py)
│  │  ├─ tools/ (terraform.py, ansible.py, kubernetes.py, aws.py, azure.py,
│  │  │          gcp.py, vmware.py, github.py, prometheus.py, console.py)
│  │  ├─ integrations/ (gemini.py, servicenow.py, keycloak.py, langfuse.py)
│  │  ├─ rag/ (embeddings.py, store.py, ingest.py, retriever.py)
│  │  ├─ graph_db/ (neo4j.py, context_graph.py)
│  │  ├─ db/ (models.py, session.py, repositories/*)
│  │  ├─ security/ (confidentiality.py, redaction.py, rbac.py, idempotency.py)
│  │  └─ schemas/ (pydantic models incl. per-workflow input schemas)
│  ├─ alembic/ (versions/*)
│  ├─ seed/ (seed.py + data)
│  ├─ tests/ (unit, integration[testcontainers], conftest)
│  └─ pyproject.toml / requirements.txt
└─ docs/  (this kit's docs travel with the repo)
```

## 3. LangGraph agent graph (summary; full detail in `05_AGENTS_SPEC.md`)

Shared **typed state** (`AgentState`, a Pydantic/TypedDict) carries: session/context ids,
thread messages, intent + confidence, selected workflow + version, parsed+validated inputs,
plan JSON, discovered dependencies, tool results, approval status, execution mode, errors/
retries, SNOW ids, trace/span ids, confidentiality, references, outcome.

Nodes: `router → {cloudops|devops|sre|knowledge|general}`; action agents call tools and hit
`approval` (interrupt) before any side-effecting tool; `servicenow` and `notify` run as
sub-steps; `verify` and `finalize` close out. Edges chosen by router classification +
guardrails + decision matrix. **Checkpoint after every critical node**; **interrupt** at
approval; **resume** from checkpoint on approve or after restart.

## 4. Data flow (CloudOps "create EKS", real)
user msg → router(intent=provisioning, conf) → create SNOW SR → CloudOps: request structured
inputs → parse+Pydantic-validate → cloud SDK discovery (find prod VPC) + availability checks →
select TF workspace/module → `terraform init/validate/plan` (real) → compute confidentiality +
build plan/diff/policy artifacts → **interrupt(approval)** → (approve) resume → `terraform
apply` (real, streamed to console) → parse outputs → update monitoring → cloud SDK verify →
update+close SNOW → write/close context graph → emit Langfuse+OTel+Prometheus → stream final
status. Every arrow emits SSE step/token/analysis/reference/confidentiality events to the UI.

## 5. Transport / SSE
`POST /chat` (or `GET /chat/stream`) returns an SSE stream with events: `step`, `token`,
`analysis`, `reference`, `confidentiality`, `console`, `interrupt` (approval needed), `done`,
`error`. Each carries the run/trace/context ids. Frontend renders into the exact design UI.
SSE supports `Last-Event-ID` resume. Inputs/approvals/console-input go over REST (SSE is
one-way), per HLD.

## 6. Scaling & resilience
Stateless API → run N replicas behind a load balancer; sticky not required (state in PG/Redis/
Neo4j; SSE reconnect via Last-Event-ID). Idempotency keys on every tool execution prevent
duplicates on retry/resume. Graceful shutdown drains streams. Health (`/healthz`) + readiness
(`/readyz`) endpoints check all dependencies.

## 7. Security
Keycloak OIDC; JWT validated per request; RBAC at endpoint + per-tool; least-privilege cloud
creds; secrets only from env/secret store (Vault-ready interface, env by default); redaction
filter on all logs/streams; immutable audit log table; CORS locked; API rate limiting;
Terraform state stored in a real backend (local volume by default, S3 backend configurable);
sensitive context-graph fields masked/tokenized.
