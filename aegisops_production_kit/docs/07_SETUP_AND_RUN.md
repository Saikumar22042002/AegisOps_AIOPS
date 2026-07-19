# 07 — Setup & Run

This is what the generated `README.md` must let a fresh clone do. Claude Code: create the
`docker-compose.yml`, `Makefile`, migrations, seed, Keycloak realm import, and observability
provisioning so the steps below actually work.

## Prerequisites
- Docker + Docker Compose
- Node.js 20+ and npm
- Python 3.11+
- **Terraform CLI** and **Ansible** installed (and baked into the backend image)
- `kubectl` + a kubeconfig for any target cluster
- Credentials (placed in `.env`): Gemini API key; AWS/Azure/GCP; vCenter; ServiceNow; GitHub

## 1. Configure
```bash
cp .env.example .env
# Fill in every value you have. Minimum to boot the app + chat: GEMINI_API_KEY + the
# auto-provisioned local service URLs (already set to the docker-compose defaults).
# Cloud/ServiceNow/GitHub creds are needed only for those real workflows.
```

## 2. Bring up the stack
```bash
docker compose up -d        # PG+pgvector, Redis, Neo4j, Keycloak, Langfuse, OTel, Prometheus, Grafana
docker compose ps           # all healthy
```
Default local endpoints (override in .env):
- Postgres `localhost:${POSTGRES_PORT}` (default `5433`; container-internal port stays `5432`) · Redis `localhost:6379` · Neo4j `localhost:7687` (UI `:7474`)
- Keycloak `localhost:8080` (realm `aegisops` auto-imported) · Langfuse `localhost:3001`
- Prometheus `localhost:9090` · Grafana `localhost:3002` · OTel Collector `:4317/:4318`

## 3. Migrate + seed
```bash
make migrate     # alembic upgrade head + neo4j schema/constraints
make seed        # real initial data: org, roles, sample documents->embeddings, integrations
```

## 4. Run the app
```bash
make dev         # backend (uvicorn :8000) + frontend (next :3000) together
# API docs: http://localhost:8000/docs   App: http://localhost:3000
```

## 5. Verify (smoke)
1. http://localhost:3000 → login screen matches source → sign in via Keycloak.
2. Toggle theme dark/light/system → matches source.
3. Ask: "List all VPCs in AWS us-east-1" (read-only) → real discovery streams back (needs AWS creds).
4. Ask: "Provision an EKS cluster reusing the prod VPC" → intent→plan→**approval gate**; approve
   (as an admin role) → real `terraform apply` streams in the console; timeline → verify → done.
5. 👍 a reply → reload → feedback persists. Open all 8 artifact tabs → real run data.
6. Open Langfuse (`:3001`) → the run's trace is there. Open Grafana (`:3002`) → metrics flow.
7. Neo4j browser (`:7474`) → the run's context graph exists with ordered steps.

## 6. Tests
```bash
make test                 # backend pytest (unit + integration via testcontainers)
cd frontend && npm test   # vitest
cd frontend && npx playwright install && npx playwright test   # E2E
```

## 7. Makefile targets (provide these)
`up, down, logs, migrate, seed, dev, dev-api, dev-web, test, lint, fmt, e2e, reset`.

## 8. Production notes (build for these, even if run locally)
- Terraform state: local volume by default; S3+DynamoDB backend configurable via env.
- Run multiple API replicas behind a load balancer; SSE resumes via Last-Event-ID; state lives
  in PG/Redis/Neo4j (API stays stateless).
- Secrets: env by default; a `SecretProvider` interface allows Vault/KMS later (implement env
  provider fully now; do not stub the Vault one — add it only when wired).
- Graceful shutdown drains active SSE streams; `/healthz` + `/readyz` for orchestration.

## 9. Security reminders
- The ServiceNow doc shipped with this project leaked a real instance password. **Rotate it**
  and place credentials only in `.env`. Never commit `.env`. Never hard-code any secret.
- Use least-privilege cloud credentials. Keep `terraform.tfstate*` and `.env` in `.gitignore`.

## 10. Troubleshooting
- **Gemini model:** default `gemini-3.5-flash` (GA; alias `gemini-flash-latest`). If your key
  lacks access, set `GEMINI_MODEL` to a flash model your account lists (the client logs the
  available models on startup).
- **CORS:** set `CORS_ORIGINS=http://localhost:3000`.
- **Keycloak first boot:** wait for realm import to finish (check `docker compose logs keycloak`).
- **SSE buffering:** disable proxy buffering; ensure the frontend reads the stream incrementally.
- **Terraform/Ansible not found:** confirm they're installed in the backend image/PATH.
