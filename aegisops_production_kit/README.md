# AegisOps

**AI-native, multi-cloud agentic AIOps platform — CloudOps · DevOps · SRE behind one chat.**

An engineer states intent in natural language; a real **LangGraph** multi-agent system
(Router → CloudOps / DevOps / SRE / Knowledge / General + Approval / ServiceNow / Notify)
classifies it, plans a workflow, performs **read-only** discovery against real cloud/K8s,
produces a real **Terraform** plan (or DevOps/SRE workflow), shows reasoning + plan + policy +
confidentiality in an artifact panel, and **pauses for human approval** before any real change.
On approval it executes for real, writes a Neo4j context graph, updates ServiceNow, and emits
full observability — all streamed to a pixel-exact UI.

- **LLM:** Google **Gemini** (`gemini-3.5-flash`) inside the agents, via `google-genai`.
- **Multi-cloud, any resource:** curated Terraform template registry across **AWS / Azure / GCP**
  (S3, VPC, EKS, RDS, EC2, Azure Storage/Resource Group, GCS, …) **+ a generic module** escape
  hatch. Cloud SDKs are read-only (discovery/availability/verify); **Terraform performs every
  mutation, only after the human-approval interrupt.**
- **Stack:** Next.js 14 + React 18 + TypeScript (frontend) · FastAPI + Pydantic v2 + SSE +
  SQLAlchemy 2/Alembic (backend) · PostgreSQL+pgvector, Redis, Neo4j · Keycloak OIDC + RBAC ·
  Langfuse + OpenTelemetry + Prometheus + Grafana.

---

## Prerequisites
- Docker + Docker Compose
- Node.js 20+ and npm
- Python 3.11–3.13 (for host-side `make dev`; the API container uses 3.11)
- Terraform CLI, Ansible, kubectl — **baked into the API image**; install locally only for host `make dev`
- On **Windows without GNU make**, use `./make.ps1 <target>` (same targets as the Makefile)

## Quickstart
```bash
cp .env.example .env          # fill GEMINI_API_KEY (+ cloud/GitHub/ServiceNow creds as needed)
docker compose up -d          # PG+pgvector, Redis, Neo4j, Keycloak, Langfuse, OTel, Prometheus, Grafana
make migrate                  # Alembic migrations + Neo4j schema   (Windows: ./make.ps1 migrate)
make seed                     # real seed data (org, roles, users, integrations, knowledge docs)
make dev                      # backend :8000 + frontend :3000      (Windows: ./make.ps1 dev)
```
Open **http://localhost:3000** and sign in (see users below). API docs: **http://localhost:8000/docs**.

To run everything containerized instead of `make dev`: `docker compose --profile full up -d --build`.

## Service endpoints (host ports)
| Service | URL | Service | URL |
|---|---|---|---|
| Frontend | http://localhost:3000 | Langfuse | http://localhost:3001 |
| API | http://localhost:8000 | Grafana | http://localhost:3002 |
| Keycloak | http://localhost:8080 | Prometheus | http://localhost:9090 |
| Neo4j browser | http://localhost:7474 | Postgres | localhost:`${POSTGRES_PORT}` (default 5433) · Redis localhost:6379 |

> Langfuse and Grafana both listen on container port 3000 but are mapped to **3001/3002** —
> only the frontend uses host **3000**. (Don't run `make dev` and `--profile full` together.)

## Seed users (Keycloak realm `aegisops`, password `aegisops`)
| User | Role | Can approve | Can initiate |
|---|---|---|---|
| `maya.okafor@northwind.com` | Platform Admin | ✅ | ✅ |
| `dev.engineer@northwind.com` | DevOps Engineer | ❌ | ✅ |
| `audit.viewer@northwind.com` | Read Only | ❌ | ❌ |

## Enabling full functionality (credentials in `.env`)
| Capability | Requires |
|---|---|
| Live chat, reasoning, RAG embeddings | `GEMINI_API_KEY` |
| AWS / Azure / GCP provisioning + discovery | cloud creds (`AWS_*`, `AZURE_*`, `GOOGLE_*`) |
| DevOps pipeline (repos/CI/secrets) | `GITHUB_TOKEN`, `GITHUB_ORG` |
| ITSM tickets | `SERVICENOW_*` (**rotate the leaked password from the source doc**) |
| Kubernetes deploys | a mounted `KUBECONFIG` |

Without these, the platform runs and **fails loudly** on the unconfigured path (clear error +
structured log) — it never falls back to fake data. `GET /readyz` and the Administration →
Integrations grid report live health.

## How a CloudOps request flows (with creds set)
`chat → Router (intent + cloud/resource/action, ServiceNow ticket, context graph) → CloudOps
(template select → Pydantic validate → SDK availability → terraform init/validate/plan →
policy + confidentiality) → approval interrupt → on approve: terraform apply/destroy (streamed)
→ verify (SDK reads) → ServiceNow close → context graph close → done` — every arrow emits SSE
(`step/token/analysis/reference/confidentiality/console/interrupt/done/error`).

## Tests
```bash
make test                                   # backend pytest + frontend vitest
cd frontend && npx playwright test          # E2E (app must be running)
```

## Security
- Secrets only via `.env` (gitignored). Never commit `.env`, `terraform.tfstate*`.
- RBAC enforced at every endpoint **and** per tool; approvals are immutable (DB + context graph).
- Secrets redacted in logs/streams/console; confidentiality badge on every agent message.
- Use least-privilege cloud credentials; Terraform state is a local volume by default
  (S3+DynamoDB backend configurable via `TF_STATE_*`).

## Repo layout
`backend/` FastAPI + LangGraph agents + tools + RAG + graph_db + security · `frontend/` Next.js
app + components + lib · `infra/` keycloak realm, prometheus, otel, grafana, **terraform-workspaces**
(multi-cloud templates) · `docs/` spec · `PROGRESS.md` build checklist.

See `docs/` for the full requirements, architecture, backend, and agents specs.
