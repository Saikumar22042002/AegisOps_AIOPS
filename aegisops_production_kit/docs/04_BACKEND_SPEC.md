# 04 — Backend Spec (FastAPI · real integrations · no mocks)

Every client below is **fully implemented** against the real service. The only thing missing
at build time is runtime credentials, which the operator supplies in `.env`. Where a service
is unreachable at runtime, fail loudly with a clear error and structured log — **never**
silently fall back to fake data.

---

## 1. Stack
Python 3.11+, FastAPI, Uvicorn/Gunicorn, Pydantic v2, `sse-starlette`, SQLAlchemy 2 + Alembic,
`asyncpg`/`psycopg`, `pgvector`, `redis`, `neo4j`, **LangGraph** + **LangChain**,
`google-genai` (Gemini), `python-keycloak` / OIDC validation (`authlib`/`python-jose`),
`langfuse`, `opentelemetry-sdk` + exporters, `prometheus-client`, `boto3`, `azure-identity` +
`azure-mgmt-*`, `google-cloud-*`, `pyVmomi`, `kubernetes`, `PyGithub`/`githubkit`, `httpx`,
`tenacity` (retries), `structlog`. Terraform & Ansible CLIs available in the API image.

## 2. Settings (`settings.py`, pydantic-settings; values from `.env`)
All keys in `.env.example`. Validate on startup; `/readyz` reports each dependency's health.

## 3. Gemini (real) — `integrations/gemini.py`
```python
from google import genai
from google.genai import types
import os, structlog
log = structlog.get_logger()

class GeminiLLM:
    def __init__(self, settings):
        self.client = genai.Client(api_key=settings.GEMINI_API_KEY)
        self.model = self._resolve(settings.GEMINI_MODEL)        # default "gemini-3.5-flash"

    def _resolve(self, wanted: str) -> str:
        # gemini-3.5-flash is the current GA flash model (alias: gemini-flash-latest).
        try:
            ids = {m.name.split("/")[-1] for m in self.client.models.list()}
            if wanted in ids: return wanted
            for cand in ("gemini-3.5-flash", "gemini-flash-latest"):
                if cand in ids:
                    log.warning("gemini.model_fallback", wanted=wanted, using=cand); return cand
            return wanted
        except Exception as e:
            log.warning("gemini.model_list_failed", error=str(e)); return wanted

    async def stream(self, system: str, contents: list, tools=None):
        cfg = types.GenerateContentConfig(system_instruction=system, tools=tools or None)
        for chunk in self.client.models.generate_content_stream(
            model=self.model, contents=contents, config=cfg):
            if chunk.text: yield chunk.text   # also surface tool-call parts for agent tool-calling
```
Used as the LLM **inside** the LangGraph agents (with native tool-calling). Embeddings via
`gemini-embedding` (or a configured embedder) for RAG.

## 4. SSE contract — `POST /chat` (auth required)
Body: `{ sessionId, message, model, context:{org,env,cloud,region,role} }`.
Events:
| event           | data                                                                 | UI effect |
|-----------------|----------------------------------------------------------------------|-----------|
| `step`          | `{index,label}`                                                       | advance thinking timeline |
| `token`         | `{text}`                                                              | append streamed reply |
| `analysis`      | `{summary, reasoningCards[]}`                                         | Analysis tab + Reasoning artifact |
| `reference`     | `{title,source,url,relevance}`                                        | References tab/citations |
| `confidentiality`| `{level,score}`                                                     | badge on message |
| `console`       | `{stream:'stdout'|'stderr', line}`                                    | VM/command console |
| `interrupt`     | `{kind:'approval'|'password'|'input', runId, plan, diff, policyChecks}`| approval gate / prompt |
| `done`          | `{messageId, runId, traceId, contextId, snowId, outcome}`            | finalize message |
| `error`         | `{message, code, retriable}`                                          | error UI + retry |
Supports `Last-Event-ID`. The step labels reflect the live graph and the selected cloud/region
(e.g. "Queried AWS · us-east-1").

## 5. REST endpoints (all auth + RBAC)
```
# auth (Keycloak OIDC)
GET  /auth/login            -> redirect to Keycloak (Auth Code + PKCE)
GET  /auth/callback         -> exchange code, set session, return user+roles
GET  /auth/me               -> { user, roles }
POST /auth/logout

# chat / runs
POST /chat                  -> SSE (see §4)
GET  /chat/stream/{runId}   -> SSE resume (Last-Event-ID)
POST /runs/{runId}/input    -> answer an interactive prompt (password/input)  [masked, not logged]
POST /approvals/{runId}     -> { decision:'approved'|'rejected' }  [RBAC: approver roles] resumes graph
GET  /runs/{runId}          -> full run state (intent, plan json, inputs json, steps, outcome)

# sessions / messages / feedback
GET  /sessions ; POST /sessions ; GET /sessions/{id} ; POST /sessions/{id}/close
GET  /sessions/{id}/messages
POST /feedback              -> { messageId, value, comment?, sensitive? }  (persist + link to context graph)

# artifacts (real run data)
GET  /runs/{runId}/timeline | /reasoning | /terraform | /logs | /metrics | /traces | /references | /approvals

# modules (real, org-scoped)
GET  /modules/projects | /infrastructure | /incidents | /knowledge | /analytics | /admin | /settings
GET  /integrations          -> live health of each connected service
GET  /notifications

# knowledge / rag
POST /knowledge/ingest      -> ingest doc(s) -> embeddings (pgvector)
GET  /knowledge/search?q=   -> semantic search w/ citations

# console (command execution surface)
GET  /console/{runId}/stream-> SSE stdout/stderr
POST /console/{runId}/input -> stdin / approval / password

# ops
GET  /healthz ; GET /readyz ; GET /metrics  (Prometheus)
```

## 6. Database (PostgreSQL + pgvector) — SQLAlchemy + Alembic
Entities (real migrations): `organizations, users, roles, sessions, messages
(+confidentiality_level/score, trace_id, context_id, snow_id), feedback (+comment, sensitive),
runs (intent, confidence, workflow, version, mode, status, plan_json, input_json, outcome),
run_steps (name, status, started/ended, error, retries, tool, human_vs_auto),
approvals (run_id, decision, actor_user, actor_role, ts — immutable),
documents + document_chunks(embedding vector), audit_log (immutable),
integrations (name, kind, config_ref, status), notifications`. Org-scope every query
(multi-tenant). pgvector index (ivfflat/hnsw) on `document_chunks.embedding`.

## 7. Context graph (Neo4j) — `graph_db/context_graph.py`
Implement the full node/relationship model from `01_REQUIREMENTS.md §3.12` / the Context-Graph
AC. One graph per SR/Incident. Real writes at each step. Resumable: load last successful step
and continue without re-asking inputs. Closed contexts immutable + searchable. Sensitive
fields tokenized. Every write audit-logged. Store trace/span ids on nodes.

## 8. Integration clients (all real)
- **ServiceNow** `integrations/servicenow.py`: REST (Basic/OAuth) — create/update/close
  `incident`, `sc_request`/`sc_req_item`/`sc_task`, `change_request`; attach artifact links;
  read approval state. Retries + timeouts. (Credentials from env only — the leaked password
  from the source doc must be rotated; never hard-code it.)
- **GitHub** `tools/github.py`: repos, branches, commits, PRs, Actions runs, GitHub Secrets.
- **Cloud readers** `tools/{aws,azure,gcp,vmware}.py`: read-only discovery, availability/quota
  checks, drift detection, post-apply verification. **No provisioning here.**
- **Terraform** `tools/terraform.py`: `TerraformRunner` shelling to the CLI —
  init/validate/plan(`-json`)/apply/destroy; parse plan to the diff + resource counts the
  artifact panel shows; manage workspaces + state backend; stream output to console SSE.
- **Ansible** `tools/ansible.py`: run playbooks, stream output, parse results.
- **Kubernetes** `tools/kubernetes.py`: official client for deploy/status/logs (reads +
  approved applies).
- **Prometheus** `tools/prometheus.py`: PromQL queries for metrics/analytics/SRE.
- **Keycloak** `integrations/keycloak.py`: OIDC discovery, token validation, role extraction.
- **Langfuse** `integrations/langfuse.py`: real traces/spans per `01_REQUIREMENTS §3.13`.

## 9. RAG (`rag/`)
Real ingest → chunk → embed (Gemini embeddings) → store in pgvector; retriever with optional
reranker; citations returned to the Analysis/References UI. Seed with the runbooks/RCAs/design
docs implied by the design (Knowledge module rows) as real documents.

## 10. Security & confidentiality
- `security/confidentiality.py`: real classifier (pattern + heuristic + optional LLM check)
  producing Low/Medium/High + score per message — drives the badge. Required on every agent
  response.
- `security/redaction.py`: mask secrets/tokens/passwords in logs, streams, console, and
  context graph (best-effort client-side mirror in the UI too).
- `security/rbac.py`: role guards (FastAPI dependencies) on every side-effecting route.
- `security/idempotency.py`: idempotency keys for tool executions (Redis-backed) to prevent
  duplicate apply on retry/resume.

## 11. Observability
- `otel.py`: OpenTelemetry traces + metrics to the Collector; one span per graph node/tool.
- `prometheus-client`: per-agent counters/histograms (requests, success/fail, duration, retry,
  approval wait, RAG latency, LLM latency) tagged by agent/workflow/env; exposed at `/metrics`.
- `structlog`: JSON logs with correlation ids (trace, context, session), agent, step. Never
  log secrets. Grafana dashboards provisioned from `infra/grafana`.

## 12. Error handling (everywhere)
Typed exceptions; `tenacity` retries w/ backoff on transient external failures; timeouts on
every network call; SSE emits `error` with `retriable`; graph marks step failed with rationale
and supports retry/rollback; partial failures preserved in the context graph; no bare
`except: pass`.
