# 01 — AegisOps Requirements (Production PRD)

**Functional source of truth = this doc + the AC/HLD content folded in here.
Visual source of truth = `design-reference/AegisOps_Workspace_v3.SOURCE_OF_TRUTH.html`.**
On any look-and-feel disagreement, the HTML wins. On any behavior gap, this doc wins.

Build posture (operator-confirmed):
- **Infra execution:** Real apply **after mandatory human-in-the-loop approval.** Support all
  modes — Dry-Run (validate only), Terraform Plan, Terraform Apply (approval required),
  Terraform Destroy (approval required). No infrastructure change occurs without explicit
  approval; only read-only operations run without it.
- **Backing services:** full production stack via Docker Compose — PostgreSQL+pgvector, Redis,
  Neo4j, Keycloak, Langfuse, OpenTelemetry Collector, Prometheus, Grafana (Loki/Tempo/MinIO/
  Kafka are optional, wire interfaces so they can be enabled later **without stubs in the
  hot path** — i.e. only add them when actually implemented).
- **Agent brain:** real LangGraph multi-agent. Gemini `gemini-3.5-flash` is the LLM *inside*
  the agents. No single-agent shortcut.
- **Mandatory mindset:** enterprise-grade from day one — scalability, maintainability,
  observability, security, resumability, commercial readiness. No dev-only shortcuts.

---

## 1. Product

AegisOps is a chat-driven, multi-agent AIOps platform unifying **CloudOps, DevOps, and SRE**
work behind one conversational interface. An engineer states intent in natural language; a
LangGraph agent graph classifies it, plans a workflow, performs read-only discovery against
real cloud/K8s, produces a real Terraform plan (or DevOps/SRE workflow), shows reasoning +
plan + policy checks + confidentiality in an artifact panel, and **pauses for human
approval** before any real change. On approval it executes for real (Terraform/Ansible/
kubectl/GitHub/ServiceNow), updates state, writes a context graph, emits full observability,
verifies, and streams the outcome back — all rendered exactly as the design depicts.

## 2. Personas & RBAC (enforced in UI **and** at every API + tool boundary)

Roles (from the design's role selector):
`Platform Admin, Org Admin, Cloud Architect, DevOps Engineer, SRE, Developer, Auditor, Read Only`.

- **Read Only / Auditor:** view + chat (read-only intents) only. Cannot approve/reject or
  trigger any side-effecting action. Server rejects such calls (403); UI disables controls
  with tooltip.
- **Developer / DevOps Engineer / SRE:** chat + *initiate* workflows; **cannot approve**
  production changes. Approval requests route to an admin.
- **Cloud Architect / Org Admin / Platform Admin:** full control incl. approve/reject/apply.

RBAC is backed by **Keycloak** (OIDC): real login, real roles/claims, real token validation
on the API. The UI's role selector reflects/uses the authenticated user's roles.

## 3. Functional requirements (folds in HLD + AC + Context-Graph + Langfuse + ServiceNow docs)

### 3.1 Auth & session
- Real Keycloak OIDC login (Authorization Code + PKCE). JWT validated on every API call.
- The login screen renders exactly as the source `LOGIN` region; it initiates the real OIDC
  flow. Sign-out ends the session.
- Sessions persist (Postgres). A session closes when its Service/Change Request is closed or
  its Incident/Problem is resolved; on close the composer is disabled and further writes are
  blocked (terminal state), per AC.
- Follow-up questions stay in the same context (full thread + memory passed to the graph).

### 3.2 Chat workspace (the heart)
Reproduce the design exactly, but powered for real:
- Composer (textarea, Enter=send, Shift+Enter=newline, three suggestion chips, send-button
  enable/disable, model + agent-mode footer).
- On send: render the live **thinking timeline** as the graph emits real step events
  (intent → memory/context → cloud query → knowledge search → policy eval → compose), then
  stream the agent's **real Gemini tokens** word-by-word with the blinking caret.
- **Two-tab message view** per HLD/AC: *Conversation* and *Analysis / References*. The
  Analysis tab shows the privacy-safe reasoning summary (NOT raw chain-of-thought) +
  retrieved references/citations.
- **Confidentiality badge** (Low/Medium/High + score + tooltip) on every agent message,
  computed by a real classifier over the response/content.
- **Feedback**: 👍/👎 + optional comment + sensitivity flag; persisted to Postgres and linked
  to the context graph node (for future training), reflected optimistically.
- **Interpreted intent**, **selected workflow**, and **step-by-step execution plan with input
  JSON** are displayed (AC requirement) — these come from real graph state.
- New chat clears the thread.

### 3.3 Artifact panel (8 tabs) — all real data
Timeline, Reasoning, Terraform, Logs, Metrics, Traces, References, Approvals. Each renders
real run data: the live workflow node states; the agent's reasoning summary cards; the real
Terraform PR-style diff + real `terraform plan` resource counts + real OPA/policy check
results; real execution logs (secret-masked); real Prometheus metrics; **real Langfuse trace
spans** for the run; real RAG references with sources; the real approval record(s). Shapes
match `DESIGN_REFERENCE.logic.js`.

### 3.4 Human-in-the-loop approval & execution modes
- The graph **interrupts** at the approval gate (LangGraph interrupt/checkpoint). UI shows
  Approve/Reject (RBAC-gated). 
- Execution-mode state machine (see `05_AGENTS_SPEC.md`):
  `Dry-Run → Plan → [Approval] → Apply | Destroy`. 
- Approve → resume the checkpointed graph → real apply/destroy → live status in Timeline
  ("Applying N resources…") → state update → verification.
- Reject → halt; workflow terminal; nodes show cancelled; nothing executes.
- Approvals are resumable after process restart (durable checkpoints in Postgres/Redis).
- Every approval records who/when/what (immutable audit).

### 3.5 VM / command console (per HLD/AC)
A real command-execution surface: commands run in a sandboxed runner (Docker exec / K8s Job
with a PTY bridge); stdout/stderr stream over SSE; interactive prompts (password/approval/
input) pause execution and await user action via REST; secrets masked in the UI. Wire this
to the same approval gate. (If the engineer prefers, the Terraform/Ansible execution surfaces
its real CLI output through this same console stream.)

### 3.6 Intent classification & routing (real)
- Classify into **Cloud Operations, DevOps, SRE (Incident Management)**, Knowledge/RAG, or
  General. Explainable: reason + confidence logged (and shown as interpreted intent).
- Every new actionable request creates a **ServiceNow SR/CR or Incident** with full context
  and artifact links (real ServiceNow REST). Misroute target <1% (measure + log).
- Ambiguous/low-confidence intent → ask the user to clarify; **no destructive action on
  unclear intent.**

### 3.7 CloudOps workflows (real)
Router→CloudOps. Select workflow template by intent; pick tool (Terraform/Ansible); request
required inputs in a structured format; parse free-form (comma-separated/multiline); validate
with **Pydantic**; on schema error return actionable clarification. Run pre-validations and
**real resource-availability checks** (cloud SDK reads). Generate + display the execution
plan JSON and input JSON for human validation. **No Terraform/Ansible execution without
approval.** Execute multi-step; update monitoring/observability post-provision; update+close
ServiceNow; capture step-level results; store full context in the **context graph**; return
final status. (Mirror the "create vm" example flow in the AC doc, for real.)

### 3.8 DevOps workflows (real)
Stages: `INIT → ENSURE_REPO_EXISTS → ENSURE_WORKING_COPY → ENSURE_CHANGES_PUSHED →
ENSURE_CI_RUN → ENSURE_IMAGE_EXISTS → ENSURE_K8S_DEPLOYED`. Real GitHub API: create repo if
absent; ensure Dockerfile + Actions workflows + secrets (GitHub Secrets); clone; commit;
push; trigger + track CI; build + verify image; deploy to K8s. Approvals before PR merge,
before CI execution, before K8s deploy. Track env (dev/stg/prod) + feature branch; share repo
link in chat.

### 3.9 SRE / incident workflows (real)
Triage true/false positive with rationale; collect real logs/metrics (cloud SDK / Prometheus
reads); RAG-retrieve runbooks; apply a decision matrix to choose next actions; produce a
human-readable analysis; propose remediation; execute only after approval; remediation
visible in the console stream; correlate with deploys; update + close ServiceNow.

### 3.10 Modules (real, DB-backed)
Projects, Infrastructure, Incidents, Knowledge, Analytics, Administration, Settings. Each
renders eyebrow/title/icon/description, a 4-up stat grid, and list rows — populated from real
data (live cloud inventory for Infrastructure, real incidents/SNOW for Incidents, real
documents+embeddings for Knowledge, real metrics for Analytics, real org/RBAC/MCP/audit for
Administration, real user prefs for Settings). Administration shows the integrations grid with
**live health** of each connected service.

### 3.11 Knowledge / RAG (real)
Real document store + **pgvector** embeddings. Real semantic search with citations surfaced in
the Analysis/References tab. Runbooks/RCAs/design-docs/conversation-summaries ingested via a
real pipeline. Embeddings via a real embedding model (Gemini embeddings or a configured
embedder).

### 3.12 Context graph (real, Neo4j) — per the Context-Graph AC
One graph per SR/Incident; nodes for Context/Trigger/Agent/Intent/Workflow/Step/Action/
Approval/Human/Tool/Reasoning/Evidence/Outcome/Feedback with the documented relationships.
Records ordered steps, agent type, workflow+version, intent+routing, template, sanitized
inputs, Pydantic + pre-validation results, approvals (who/when), tool used, step status,
errors+retries, partial failures, human-vs-automated, outcomes, final resolution, rollback
info, SNOW ids, linked metrics/logs/traces, reasoning, feedback. **Resumable execution from
last successful step.** Closed contexts immutable, searchable, reusable; RBAC-gated; sensitive
data masked/tokenized; all updates audit-logged.

### 3.13 Observability (real) — per the Langfuse AC
Langfuse enabled for all agents; one trace per request linked to the context-graph id; spans
for intent/routing/planning/each step/tool calls/reasoning/RAG/approvals/outcomes; token usage
+ latency per call; error tagging; sanitized I/O; no secrets. Plus OpenTelemetry traces/metrics
to the Collector; Prometheus metrics (per-agent request/success/fail counts, durations, retry
counts, approval wait time, RAG latency, LLM latency, tagged by agent/workflow/env); Grafana
dashboards provisioned. Structured JSON logs with correlation ids (trace id, context id,
session id), agent name, workflow step. Logs/metrics/traces correlated and queryable per SR/
Incident.

### 3.14 Command palette, notifications, theming, responsive
⌘K palette (real actions/nav), notifications (real events), dark/light/system theme (live OS
follow), full responsive behavior — all exactly as the design.

## 4. Non-functional requirements
- **Performance:** first token <1.5s P50; stream cadence ≤300ms; dependency discovery <30s
  P50 typical.
- **Reliability:** graph checkpoints at every critical node; resume on crash; SSE auto-
  reconnect with Last-Event-ID; idempotency keys so no duplicate tool execution.
- **Security/Privacy:** secrets never logged; outputs redacted; RBAC at endpoints and per
  tool; immutable audit; least-privilege cloud creds; Keycloak-issued tokens; CORS locked to
  configured origins; rate limiting on the API.
- **Observability:** as §3.13. **Resumability:** as §3.12.
- **Commercial readiness:** multi-tenant-aware (org scoping on every query), config via env,
  health/readiness endpoints, graceful shutdown, containerized, horizontally scalable
  (stateless API workers; state in PG/Redis/Neo4j).

## 5. Build plan (phased — verify against source HTML + checklist after each)
1. **Foundation:** monorepo, `docker-compose` (PG+pgvector, Redis, Neo4j, Keycloak, Langfuse,
   OTel, Prometheus, Grafana), `.env.example`, Makefile, CI config, lint/format, base FastAPI
   + Next.js apps, health endpoints, structured logging, OTel wiring.
2. **Auth + RBAC:** Keycloak realm/clients/roles (import file), OIDC login on FE, JWT
   validation + role guards on BE; login screen matches source.
3. **Design system:** port tokens/fonts/animations/responsive; theming + system follow; build
   every shared component shell to match the template.
4. **App shell:** sidebar, top-nav selectors (all menus), command palette, notifications,
   overview cards, mobile drawers — matching source exactly; selectors wired to real state.
5. **Data layer:** SQLAlchemy models + Alembic migrations for all entities; Neo4j schema;
   pgvector; Redis; seed script that loads real initial data.
6. **Integration clients (all real):** Gemini, ServiceNow, GitHub, cloud SDK readers,
   TerraformRunner, AnsibleRunner, kubernetes client, Langfuse, Prometheus query client.
7. **LangGraph core:** shared typed state, checkpointer (Postgres/Redis), Router agent,
   interrupts, SSE event emission; General + Knowledge/RAG agents end-to-end.
8. **CloudOps agent:** full real flow incl. Terraform plan/apply/destroy + approval + context
   graph + SNOW + verification.
9. **DevOps agent:** full real GitHub→CI→image→K8s flow with the staged state machine +
   approvals.
10. **SRE agent:** triage → telemetry → RAG → decision matrix → remediation (approval-gated).
11. **Workspace wiring:** connect composer/streaming/two-tab/confidentiality/feedback to the
    real graph SSE; artifact panel tabs to real run data; approval gate to real interrupts.
12. **Modules:** all 7 modules + integrations health from real sources.
13. **Observability + context graph polish:** Langfuse spans, OTel, Prometheus, Grafana
    dashboards, context-graph completeness, audit log.
14. **Hardening + tests:** unit/integration/E2E green; security pass; idempotency; reconnect;
    resumability; rate limiting; visual diff vs source HTML in all themes/mobile.
15. **Docs:** generated `README.md`, runbook, architecture diagram, `.env` reference.

## 6. Definition of done
See `00_CLAUDE_CODE_PROMPT.md §Definition of done` and `06_FEATURE_CHECKLIST.md` (every box).
