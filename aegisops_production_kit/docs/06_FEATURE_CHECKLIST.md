# 06 — Feature Checklist (Definition of Done)

Every box must pass with **real** systems. Mirror this as `PROGRESS.md`. No item is "done" via
a mock, stub, or placeholder.

## A. Foundation & ops
- [ ] `docker compose up -d` starts PG+pgvector, Redis, Neo4j, Keycloak, Langfuse, OTel
      Collector, Prometheus, Grafana — all healthy.
- [ ] `make migrate` runs real Alembic migrations; `make seed` loads real initial data.
- [ ] `make dev` starts backend + frontend; `/healthz` + `/readyz` green (all deps checked).
- [ ] `/metrics` exposes Prometheus metrics; Grafana shows provisioned dashboards.
- [ ] No grep hits for TODO/FIXME/mock/placeholder/NotImplemented in app code.

## B. Auth & RBAC (Keycloak, real)
- [ ] Login screen matches source; initiates real OIDC (Auth Code + PKCE); callback works.
- [ ] JWT validated on every API call; unauth → 401; UI gated behind login.
- [ ] All 8 roles enforced at endpoint + per tool; Read Only/Auditor cannot approve/execute
      (403 + disabled UI); Developer/DevOps/SRE cannot approve prod changes; admins can.
- [ ] Sign-out ends session.

## C. UI parity (pixel-exact vs source HTML, dark+light+mobile)
- [ ] Tokens/fonts/animations/responsive copied verbatim; no off-palette colors.
- [ ] Sidebar + 8 navs with correct active styling.
- [ ] All top-nav selectors (org/env/cloud/region/model/role/notifications/profile) work; one
      menu open at a time; correct dots/sublabels/checkmarks.
- [ ] Theme dark/light/system incl. live OS follow; cycle order correct.
- [ ] Command palette (⌘K) opens when authed, Esc closes, actions + nav run.
- [ ] Overview summary cards render + collapse.
- [ ] Mobile drawers (sidebar overlay, artifact drawer) behave per source.

## D. Chat workspace (real Gemini via SSE)
- [ ] Composer: Enter/Shift+Enter, suggestion chips, send enable/disable, model+mode footer.
- [ ] On send: live thinking-timeline from real graph steps, then real Gemini tokens stream
      word-by-word with blinking caret (cadence ≤300ms; first token <1.5s P50).
- [ ] Two-tab message view: Conversation + Analysis/References (reasoning summary + citations).
- [ ] Confidentiality badge (level+score+tooltip) on every agent message, from real classifier.
- [ ] Interpreted intent, selected workflow, and execution plan + input JSON displayed.
- [ ] 👍/👎 + comment + sensitivity flag; persisted + linked to context graph; optimistic UI.
- [ ] Follow-ups keep context (thread + memory passed to graph).
- [ ] SSE auto-reconnect with Last-Event-ID; "Reconnecting…" affordance.
- [ ] Secrets masked in any streamed/log/console output.

## E. Artifact panel (8 tabs, real run data)
- [ ] Timeline reflects real graph node states + execution mode.
- [ ] Reasoning shows real reasoning-summary cards.
- [ ] Terraform shows real `terraform plan` PR-style diff + resource counts + real policy checks.
- [ ] Logs show real execution logs (masked).
- [ ] Metrics show real Prometheus data.
- [ ] Traces show the real Langfuse trace for the run (spans, durations, tokens).
- [ ] References show real RAG citations.
- [ ] Approvals show the real approval record(s).

## F. Approval & execution modes (real, HITL)
- [ ] Graph interrupts at approval; Approve/Reject RBAC-gated.
- [ ] Modes: Dry-Run (validate), Plan, Apply (approval), Destroy (approval) all work.
- [ ] Approve → real terraform apply/destroy streamed to console → state update → verify.
- [ ] Reject → halt, terminal, nothing executes.
- [ ] Resumable after restart (durable checkpoints); no duplicate execution (idempotency).
- [ ] Every approval recorded who/when/what (immutable).

## G. CloudOps (real end-to-end)
- [ ] Router→CloudOps; correct template + tool selected.
- [ ] Structured input request; free-form parse; Pydantic validation; actionable errors.
- [ ] Real availability checks (SDK reads) before provisioning.
- [ ] Plan JSON + input JSON shown for human validation; no TF/Ansible run without approval.
- [ ] Real provision/modify/destroy via Terraform; monitoring updated; SDK verification.
- [ ] SNOW SR/CR created/updated/closed with artifact links; context graph stored; status returned.

## H. DevOps (real)
- [ ] Full staged state machine via real GitHub + K8s; secrets in GitHub Secrets.
- [ ] Repo create-if-absent / ensure Dockerfile+Actions+secrets; clone/commit/push.
- [ ] CI triggered + tracked; image built + verified; K8s deploy executed.
- [ ] Approvals before PR merge / CI / K8s deploy; env + branch tracked; repo link in chat.

## I. SRE (real)
- [ ] Triage TP/FP with rationale; real logs/metrics collected; RAG runbooks retrieved.
- [ ] Decision matrix selects next actions; human-readable analysis produced.
- [ ] Remediation proposed; executes only after approval; visible in console stream; SNOW updated.

## J. Modules (real, org-scoped)
- [ ] All 7 modules render from real data (Infrastructure = live inventory; Incidents = real/
      SNOW; Knowledge = real docs; Analytics = real metrics; Admin = real org/RBAC/MCP/audit).
- [ ] Integrations grid shows live health of each service.
- [ ] Stat grids responsive (2-up ≤860px, 1-up ≤460px).

## K. Knowledge / RAG (real)
- [ ] Ingest pipeline embeds docs into pgvector; semantic search returns cited results.
- [ ] Citations surface in Analysis/References.

## L. Context graph (real, Neo4j)
- [ ] One graph per SR/Incident with full node/relationship model; ordered steps; all required
      fields (intent, routing, template, sanitized inputs, validations, approvals, tool, status,
      errors/retries, partial failures, human-vs-auto, outcome, resolution, rollback, SNOW ids,
      linked metrics/logs/traces, reasoning, feedback).
- [ ] Resumable from last successful step without re-asking inputs.
- [ ] Closed contexts immutable + searchable; sensitive data masked; updates audit-logged; RBAC.

## M. Observability (real)
- [ ] Langfuse trace per request linked to context id; spans for all stages w/ tokens+latency+errors.
- [ ] OTel traces/metrics to Collector; Prometheus metrics tagged by agent/workflow/env.
- [ ] Structured JSON logs with correlation ids; no secrets; correlated + queryable per SR/Incident.

## N. Non-functional
- [ ] Perf targets met (first token <1.5s P50; cadence ≤300ms).
- [ ] Reliability: checkpoint/resume; SSE reconnect; idempotency.
- [ ] Security: RBAC everywhere; redaction; immutable audit; CORS locked; rate limiting; least-priv.
- [ ] Multi-tenant org scoping on every query; graceful shutdown; stateless API scalable.

## O. Tests (real, green)
- [ ] pytest unit + integration (testcontainers for PG/Redis/Neo4j) pass.
- [ ] Vitest + RTL frontend units pass.
- [ ] Playwright E2E: login → chat (streamed) → two-tab → feedback → approval → real plan/apply
      (against a test workspace) → modules → palette → theme → mobile. All pass.
- [ ] Accessibility: keyboard nav, ARIA, focus-visible.

## P. Delivery
- [ ] `README.md` lets a fresh clone bring everything up with the documented commands.
- [ ] `.env.example` lists every variable; `.env` gitignored; no secret committed.
- [ ] Final side-by-side visual diff vs source HTML passes in dark, light, mobile.
