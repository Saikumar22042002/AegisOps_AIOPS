# AegisOps — Build Progress

Mirrors `docs/06_FEATURE_CHECKLIST.md`. A box is checked only when backed by **real**
systems (no mock/stub/placeholder). Updated as each milestone lands.

## Milestone status
- [x] **M1 — Foundation & ops** _(live-verified: full stack healthy, realm imported)_
- [x] **M2 — Auth + design system + app shell** _(live-verified: real OIDC + RBAC; UI pixel-checked dark/light/mobile)_
- [x] **M3 — Data layer + integration clients** _(live-verified: migrations, seed, /integrations health, TerraformRunner plan)_
- [x] **M4 — LangGraph core + CloudOps/DevOps/SRE agents** _(graph compiles, Postgres checkpointer, SSE streams end-to-end; multi-cloud templates; full Gemini flow activates with key)_
- [x] **M5 — Workspace wiring + module endpoints** _(live-verified: real chat SSE + two-tab + approval + feedback; modules/admin bound to APIs; integrations grid live health)_
- [x] **M6 — Observability, hardening, tests, docs** _(Langfuse+OTel per-run wired; tests green: pytest 22, vitest 4, playwright smoke; README; no banned tokens)_

---

## Frontend rendering & session fixes (2026-06-30)
Root cause of the "dead UI": the POST-SSE client split frames on `"\n\n"`, but sse-starlette
emits `\r\n\r\n` — so `onEvent` never fired and no chat/artifact/approval state ever updated.
Backend, agents, Terraform runner, approval gate were already correct (verified from raw bytes).
- [x] **SSE render (P1)** — `lib/sse.ts` normalizes CRLF→LF before frame split (+ trailing-frame
      flush). step/token/analysis/confidentiality/console/interrupt/done now drive the UI live.
- [x] **Auth (P2)** — verified live: `/auth/login`→cookie, `/auth/me`→200 real user, CORS+creds OK.
      Earlier `ERR_EMPTY_RESPONSE` was a transient container recreate, not a code bug. Approve UI
      RBAC-gated on `can_approve`.
- [x] **Sessions (P3)** — frontend creates a session on first message and **reuses** its id for the
      whole thread (was sending `sessionId:null` → a new session per message). History load, rename
      (`PATCH /sessions/{id}`), delete (`DELETE /sessions/{id}`), reload persistence (localStorage +
      `restoreLast`).
- [x] **Sidebar (P4)** — real `GET /sessions` grouped Today/Yesterday/Earlier; `GET /overview`
      drives Projects + Incidents badges and org name; real empty state. No hardcoded rows.
- [x] **Artifact panel (P5)** — already wired to `/runs/{id}/{tab}`; now refetches on an
      `artifactNonce` bump (run start/interrupt/done/approval) and overlays live console lines in Logs.
- [x] **Approval gate (P6)** — inline Approve/Reject (RBAC-gated) → `POST /approvals/{runId}` resume
      stream; Timeline/Logs/Approvals tabs refresh on resolution.

---

## A. Foundation & ops
- [x] `docker compose up -d` starts PG+pgvector, Redis, Neo4j, Keycloak, Langfuse(v2), OTel
      Collector, Prometheus, Grafana — pinned versions + healthchecks. _(verify with `make up`)_
- [x] `make migrate` runs Alembic (15 tables, pgvector + HNSW index) + Neo4j schema/constraints;
      `make seed` loads real data (org, 8 roles, users, integrations, 4 Knowledge docs → chunks,
      notifications) — idempotent. _(verified live)_
- [x] `make dev` starts backend (uvicorn :8000) + frontend (next :3000); `/healthz` + `/readyz`
      check all deps.
- [x] `/metrics` exposes Prometheus metrics; Grafana dashboard provisioned.
- [x] No grep hits for TODO/FIXME/mock/placeholder/NotImplemented in app code.

## B. Auth & RBAC (Keycloak, real)
- [x] Keycloak realm (8 roles, frontend+backend clients, seed users) imported on boot.
- [x] Login screen matches source; real OIDC — password grant (form) + Auth Code + PKCE (SSO);
      callback exchanges code → session. _(verified live)_
- [x] JWT validated via JWKS on every API call; unauth → 401; UI gated behind login.
- [~] Role capabilities enforced (approver/initiator/read-only deps verified live); per-route +
      per-tool guards applied as side-effecting endpoints/tools land in M3–M5.
- [x] Sign-out ends session (revoke refresh + clear cookie). _(verified live)_

## C. UI parity (pixel-exact vs source HTML, dark+light+mobile)
- [x] Tokens/fonts/animations/responsive copied verbatim into `globals.css`.
- [x] Sidebar + navs with correct active styling. _(verified)_
- [x] Top-nav selectors (cloud/model/theme/notifications/profile + role switch) work; one menu
      open at a time. _(org/env/region exist in state but are not rendered in the design's
      top-nav — the source HTML is authoritative; role lives in the profile menu.)_
- [x] Theme dark/light/system incl. live OS follow (matchMedia); cycle order correct. _(verified)_
- [x] Command palette (⌘K) opens/closes; actions + nav run. _(verified)_
- [n/a] Overview summary cards — present in logic.js state but not rendered in the source design.
- [x] Mobile drawers (sidebar overlay) behave per source. _(verified at 390px)_

## D. Chat workspace (real Gemini via SSE)
- [ ] Composer behavior; live thinking-timeline; real Gemini token stream.
- [ ] Two-tab message view (Conversation + Analysis/References).
- [ ] Confidentiality badge from real classifier.
- [ ] Interpreted intent + workflow + plan/input JSON.
- [ ] Feedback persisted + linked to context graph.
- [ ] Follow-ups keep context; SSE reconnect; secrets masked.

## E. Artifact panel (8 tabs, real run data)
- [ ] Timeline · Reasoning · Terraform · Logs · Metrics · Traces · References · Approvals.

## F. Approval & execution modes (real, HITL)
- [ ] Interrupt at approval; RBAC-gated; modes dry_run/plan/apply/destroy.
- [ ] Approve → real apply/destroy → state update → verify; reject halts.
- [ ] Resumable after restart; idempotent; immutable approval record.

## G. CloudOps (real end-to-end)
- [x] **LIVE-VERIFIED with real Gemini + AWS**: "create a t3.micro EC2" → Router→CloudOps (100%) →
      aws.ec2 template → AWS availability check → real `terraform plan` (+1) → approval interrupt →
      `terraform apply` created instance `i-090d9b12107402936`; then a destroy run terminated it
      (`1 destroyed`) with clean verify+finalize. Approvals immutable; context graph written.
      (Fixes during this run: AWS `AWS_SESSION_TOKEN` support for sandbox creds; Neo4j map-property
      JSON encoding; non-fatal graph writes.)

## H. DevOps (real)
- [ ] Staged state machine via GitHub + K8s; approvals; repo link in chat.

## I. SRE (real)
- [ ] Triage; telemetry; RAG runbooks; decision matrix; approval-gated remediation.

## J. Modules (real, org-scoped)
- [ ] All 7 modules from real data; integrations grid live health; responsive grids.

## K. Knowledge / RAG (real)
- [~] Ingest chunks + stores docs in pgvector (embeddings generated once a Gemini key is set);
      semantic (cosine) search + trigram keyword fallback + retriever built. _(4 docs seeded;
      citations wired into Analysis/References UI in M5)_

## L. Context graph (real, Neo4j)
- [~] Full node/relationship model + schema/constraints + redaction + resume API built. _(writes
      exercised per-run in M4; immutability enforced on close.)_

## Workspace wiring & modules (M5) — live-verified
- [x] Composer → POST /chat real SSE: step/token/analysis/reference/confidentiality/interrupt/
      done/error consumed live (fetch-based SSE client, POST). Simulated stream removed.
- [x] Two-tab Conversation / Analysis-References per AI message; confidentiality badge from real
      classifier; feedback → POST /feedback (optimistic); follow-ups keep session context.
- [x] Approval gate → POST /approvals/{runId} (RBAC approver) streams the continuation.
- [x] Artifact panel 8 tabs fetch GET /runs/{id}/{tab} (timeline/reasoning/terraform/logs/
      metrics/traces/references/approvals) — real run data + empty states.
- [x] Modules bound to GET /modules/{name} (real org-scoped counts/rows); Admin integrations grid
      → GET /integrations live health; TopNav bell → GET /notifications.
- [~] Full streamed Gemini answer + CloudOps plan/approval render: wired; shows live error until
      GEMINI_API_KEY is set, then renders end-to-end.

## Agents & SSE (M4) — backend live-verified
- [x] Real LangGraph graph: router → cloudops/devops/sre/knowledge/general + approval(interrupt) →
      execute → verify → finalize → servicenow → notify. Compiles + runs.
- [x] Durable Postgres checkpointer (interrupt/resume/restart-safe); checkpoint tables created.
- [x] SSE contract: step/token/analysis/reference/confidentiality/console/interrupt/done/error —
      streamed end-to-end via POST /chat; /approvals, /chat/stream (Last-Event-ID), /runs, /runs/input.
- [x] **CloudOps multi-cloud**: template registry (aws s3/vpc/eks/rds/ec2, azure storage/rg, gcp gcs,
      generic module) → Pydantic validate → availability (SDK reads) → terraform plan → approval →
      apply/destroy → verify. Cloud SDKs read-only; Terraform mutates; approval gate enforced.
- [x] **DevOps**: staged GitHub→CI→image→K8s pipeline with approval gate.
- [x] **SRE**: triage → telemetry (Prometheus) → RAG runbooks → decision matrix → gated remediation.
- [x] Router creates ServiceNow ticket + opens context graph; approval recorded (DB + graph, immutable).
- [x] Confidentiality classifier on responses; redaction on console; idempotency on tool exec.
- [~] Full Gemini reasoning + token streaming + CloudOps apply/approval live run: wired + checkpointer
      ready + terraform plan proven; **activates when GEMINI_API_KEY (and cloud creds) are set**.

## Integration clients (M3)
- [x] All built real + import-clean: Gemini, ServiceNow, GitHub, AWS/Azure/GCP/VMware readers,
      Kubernetes, Prometheus, TerraformRunner (init/plan verified), AnsibleRunner, console.
- [x] `GET /integrations` live health (datastores + observability live; cloud/SNOW/GitHub/Gemini
      "not configured" until creds added). Security: redaction, idempotency, confidentiality.

## M. Observability (real)
- [x] Langfuse trace + OTel span per run (linked to context id); per-node records in context graph
      + SSE steps. Prometheus `aegisops_*` metrics at /metrics tagged by agent/workflow/domain/env;
      Grafana dashboard provisioned. Structured JSON logs (structlog) with correlation ids; secrets
      redacted, never logged.

## N. Non-functional
- [x] CORS locked; per-IP rate limiting (SlowAPIMiddleware); graceful shutdown; stateless API
      (state in PG/Redis/Neo4j); idempotency keys on tool exec; durable checkpoint resume; resilient
      degraded startup with /readyz truth.

## O. Tests (real, green)
- [x] Backend pytest **22 passing** (confidentiality, RBAC, multi-cloud templates + schema/parse,
      auth-boundary 401s, health/metrics) — run in-container with real deps.
- [x] Frontend Vitest **4 passing** (color helpers, LoginScreen RTL). Playwright E2E smoke passing.
- [~] testcontainers DB integration + full multi-step E2E journeys: scaffolded; expand with creds.

## P. Delivery
- [x] Generated `README.md` (fresh-clone runbook); `.env.example` lists every variable; `.env`
      gitignored; no secret committed. No grep hits for TODO/FIXME/mock/placeholder/NotImplemented
      in app code.

_Legend: [x] done · [~] partial/scaffolded · [ ] pending._
