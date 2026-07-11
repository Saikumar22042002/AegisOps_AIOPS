# 13 — Files read (coverage checklist)

[← back to index](../../ANALYSIS.md)

This analysis was built by reading the files below directly (not from `PROGRESS.md`). Directory-by-directory checklist so nothing load-bearing was skipped.

## Root / orientation
- `PROGRESS.md`, `README.md`, `CLAUDE.md`, `.env.example`, `Makefile`, `make.ps1` (listed), `docker-compose.yml`, `docker-compose.override.yml`, `.github/workflows/ci.yml`
- Not code-read (docs/spec, used only as reference): `docs/00…08_*.md`, `design-reference/*` (HTML/JS/CSS source of truth — its tokens are mirrored in `globals.css`), `Screenshots/*` (evidence PNGs).

## Backend — `backend/app` (all Python read)
- **Core:** `settings.py`, `main.py`, `logging_conf.py`, `metrics.py`, `otel.py`, `__init__.py`
- **API:** `api/chat.py`, `auth.py`, `artifacts.py`, `sessions.py`, `health.py`, `integrations.py`, `knowledge.py`, `modules.py`
- **Agents (all):** `state.py`, `events.py`, `graph.py`, `runner.py`, `runtime.py`, `router.py`, `intent_guard.py`, `memory.py`, `plan_guard.py`, `cloudops.py`, `params.py`, `templates.py`, `inventory.py`, `approval.py`, `execute.py`, `finalize.py`, `general.py`, `knowledge.py`, `llm.py`, `notify.py`, `servicenow_agent.py`, `devops.py`, `sre.py`, `timing.py`, `cards.py`, `checkpointer.py`, `provider_errors.py`
- **Schemas:** `schemas/workflows.py`, `schemas/auth.py`
- **Security:** `security/deps.py`, `rbac.py`, `redaction.py`, `sessions.py`, `idempotency.py`, `confidentiality.py`
- **DB:** `db/models.py`, `repositories.py`, `session.py`; migrations `alembic/versions/0001_initial.py`, `0002_resources.py`, `0003_state_workspace.py`, `alembic/env.py`
- **Graph DB:** `graph_db/context_graph.py`, `neo4j.py`, `schema.py`
- **Integrations:** `integrations/gemini.py`, `keycloak.py`, `langfuse_client.py`, `servicenow.py`
- **Tools (all):** `tools/terraform.py`, `aws.py`, `azure.py`, `gcp.py`, `console.py`, `github.py`, `kubernetes.py`, `prometheus.py`, `ansible.py`, `vmware.py`
- **RAG:** `rag/embeddings.py`, `ingest.py`, `retriever.py`, `store.py`
- **Cache:** `cache/redis.py`
- **Seed / build:** `seed/seed.py`, `Dockerfile`, `pyproject.toml`, `alembic.ini` (listed)
- **Tests:** enumerated all ~27 test files + `conftest.py` (function names catalogued in the review); read `conftest.py` in full.

## Frontend — `frontend` (all lib/components/app read)
- **lib:** `store.ts`, `sse.ts`, `api.ts`, `auth.tsx`, `theme.tsx`, `types.ts`, `data.ts`, `colors.ts`, `styles.ts`, `icons.tsx`
- **components:** `AppRoot.tsx`, `AppShell.tsx`, `Workspace.tsx`, `ArtifactPanel.tsx`, `Sidebar.tsx`, `TopNav.tsx`, `LoginScreen.tsx`, `CommandPalette.tsx`, `ModuleView.tsx`, `Markdown.tsx`
- **app:** `layout.tsx`, `page.tsx`, `providers.tsx`, `globals.css` (tokens/responsive read)
- **config:** `package.json`, `next.config.mjs`, `tailwind.config.ts`
- **tests:** enumerated vitest (`tests/*.test.ts[x]`) + Playwright (`e2e/*.spec.ts`) function names.

## Infra — `infra`
- **Terraform modules (all 14 + demo):** `aws-ec2`, `aws-s3`, `aws-rds`, `aws-vpc`, `eks-provision` (main/variables/outputs), `azure-vm`, `azure-storage`, `azure-resource-group`, `azure-postgres`, `azure-aks`, `gcp-gce`, `gcp-gcs`, `gcp-gke`, `gcp-cloudsql`, `demo-null` — each `main.tf` read in full.
- **Configs:** `otel/otel-collector.yaml`, `prometheus/prometheus.yml`, `postgres/init/01-init.sql`, `grafana/provisioning/datasources/datasource.yml`, `grafana/dashboards/aegisops-overview.json` (panels), `keycloak/realm-export.json` (summarized: realm/roles/clients/users), `kube/empty-kubeconfig` (listed).

## Notes on repo state observed
- Working tree has many pending changes (the CloudOps V1 branch) — analysis reflects the **current on-disk source**, which is what runs.
- **Committed artifacts that shouldn't be:** dozens of `infra/terraform-workspaces/**/*.tfplan` files are tracked/untracked in git (plan files embed variable values) — flagged in [12 §23](12_roadmap.md).
- `__pycache__` and `.terraform` caches were ignored.

## Discrepancies between code and `PROGRESS.md` (code wins)
1. PROGRESS: "stateless API … horizontally scalable." Code: in-process SSE channels, no eviction ([09 P4](09_problems.md)).
2. PROGRESS: "Multi-tenant: org-scope every query." Code: single default org, `user.org` ignored ([09 P2](09_problems.md)).
3. PROGRESS/CLAUDE: "RBAC enforced … per tool." Code: no per-tool RBAC; reveal + reads under-guarded ([08](08_observability_security_rbac.md)).
4. PROGRESS: ambiguous cloud "asks, never defaults to AWS." Code: UI selector defaults AWS, so it rarely asks in the real UI ([09 P11](09_problems.md)).
5. CLAUDE: "Everything is real. No mocks/stubs." Code: SRE remediation no-op, policy checks hardcoded, Traces tab static, `runinput` dead ([09 P7/P8/P9/P13](09_problems.md)).
6. Model choice advertised in UI; backend ignores `body.model` ([09 P10](09_problems.md)).

Everything else in PROGRESS that was spot-checked (SSE CRLF fix, per-message run binding, per-resource state isolation, durable approval interrupt, Langfuse span tree, redaction fix, provider-error classification, per-cloud shape validators) **is** backed by the code as described.
