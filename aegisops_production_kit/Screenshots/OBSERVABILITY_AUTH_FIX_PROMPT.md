# OBSERVABILITY_AUTH_FIX_PROMPT.md — paste into Claude Code

> Setup: put the 2 reference screenshots in `Screenshots/` as `langfuse-empty.png` (the empty dashboard) and `langfuse-target-trace.png` (the nested trace reference). Keep `PROGRESS.md` at repo root. Then paste from "You are working on AegisOps…".

---

You are working on **AegisOps** (Next.js + FastAPI + LangGraph + Gemini 2.5 Pro + Terraform + Postgres/Redis/Neo4j + Langfuse + Keycloak), the codebase documented in `PROGRESS.md`. Two specific, independent defects need fixing. Do NOT rebuild anything else; keep everything in PROGRESS green.

Read `PROGRESS.md` first (it claims Langfuse tracing and Keycloak SSO were built — so this is a "wired but broken" fix, not a from-scratch build). Reference screenshots are in `Screenshots/`.

---

## ISSUE 1 — Langfuse tracing produces NOTHING (must trace every request end-to-end)

### Evidence
- `Screenshots/langfuse-empty.png`: the Langfuse dashboard (v2.95.11 OSS, project `aegisops`, logged in as admin) shows **0 traces, $0.00 cost, 0 tokens tracked** over the last 24h. So the app is not sending traces, even though PROGRESS.md M6 claims "Langfuse trace + OTel span per run" is done.
- `Screenshots/langfuse-target-trace.png`: THIS is the target quality — a single top-level trace with a full nested span tree (parent → child → grandchild), one span per function/step, LLM generation spans with token counts + cost + latency, and tool-call spans (including errors surfaced). Reproduce this shape for AegisOps.

### What "fixed" means — end-to-end tracing for EVERY request
Every user request produces **one Langfuse trace** whose span tree mirrors the real call graph, so I can audit exactly what happened:
- **One root trace per request/run**, named meaningfully (e.g. `chat-request` / `cloudops-run`), tagged with session id, run id, context-graph id, user, agent, cloud, environment, and intent.
- **A nested span for every meaningful step and function call**, reflecting who-calls-whom: router/classification → agent (cloudops/devops/sre/knowledge/general) → planner → param collection → policy evaluation → terraform plan → approval gate (including the human-wait) → terraform apply/destroy → verification → finalize → servicenow update → notify. If function A calls function B calls C, the spans nest A→B→C.
- **LLM generation spans** for every Gemini call: model name, prompt/response (redacted), input+output **token counts**, **cost**, and latency — like the `ai.streamText` / `doStream` spans in the reference.
- **Tool/integration spans** for every external call (Terraform runner, cloud SDK reads, ServiceNow, GitHub, Prometheus, RAG retrieval) with inputs (redacted), outputs/status, latency; **errors captured on the span** (like the red `searchLangfuseDocs ERROR` in the reference), not swallowed.
- **Approval/human-in-the-loop** represented as a span across the interrupt, so the wall-clock wait is visible.
- The trace is **linked to the context-graph id** (put it in trace metadata) and vice-versa.
- Secrets are **never** sent to Langfuse — reuse the existing redaction on all inputs/outputs/metadata.

### Diagnose first (report before fixing)
Determine WHY nothing is arriving. Check, in order, and tell me which it is:
1. **Config/keys/host** — `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_HOST` in `.env` actually set and matching THIS instance + the `aegisops` project? (The dashboard is at `localhost:3001`; confirm the app points there, not a default cloud host.) Are the keys for the right project?
2. **Client init / flush** — is the Langfuse client actually instantiated on the request path, and are traces **flushed** before the process/async task ends? Short-lived async handlers often drop traces if `flush()`/shutdown isn't awaited. This is the most common cause of "0 traces."
3. **Instrumentation present but not firing** — are the trace/span calls on the REAL code path (the live `/chat` SSE handler + LangGraph nodes), or only in a helper that isn't invoked? PROGRESS claims per-node records exist — verify they emit to Langfuse, not only to the context graph.
4. **Network/health** — can the API container reach the Langfuse container (service name, port, healthcheck)? Any auth/ingestion errors in the API logs?

### Verify
- Send a real chat request, then open the Langfuse dashboard → the trace count increments and the trace shows the full nested tree with LLM token/cost spans and tool spans, matching the reference screenshot's depth.
- A request that calls an external tool which errors shows the error **on that span**.
- Token cost + count are populated (dashboard no longer $0.00 / 0 tokens).
- Add an automated check that a traced run produces a root trace with the expected child spans (assert against the Langfuse API or the SDK's local trace object).
- Confirm no secret values appear anywhere in the trace payloads.

---

## ISSUE 2 — "Sign in with Keycloak" fails on the login screen

### Evidence
Clicking **Continue with Keycloak SSO** on the login page fails. PROGRESS.md says OIDC via password-grant works and Auth Code + PKCE was implemented for SSO — so the interactive SSO redirect/callback flow is the broken part.

### Diagnose first (report before fixing)
Walk the full OIDC Authorization-Code + PKCE flow and find where it breaks; tell me which:
1. **Redirect URI mismatch** — the redirect/callback URL the app sends vs. the **Valid Redirect URIs** configured on the Keycloak client (exact scheme/host/port/path; `localhost:3000` vs `3001` vs container hostname are common mismatches).
2. **Client config** — client id/secret, "Standard flow" enabled, PKCE method (S256) set, web origins/CORS on the Keycloak client, public vs confidential client type.
3. **Discovery/endpoints** — is the app using the right issuer/authorization/token/JWKS URLs (browser-reachable host vs. in-container host — the browser must hit a host it can resolve, not a Docker service name)?
4. **Callback handler** — does `/callback` (or equivalent) exchange the code → tokens correctly, set the session cookie, and redirect back? Any error in the API logs or the URL (`error=...`) on the failed redirect?
5. **Realm import** — is the realm/client actually present in this Keycloak instance (PROGRESS says imported on boot — verify it loaded)?

### Verify
- Click "Continue with Keycloak SSO" → redirected to Keycloak → log in → redirected back to AegisOps authenticated, with a valid session (`/auth/me` → 200 real user).
- Sign-out works and a fresh SSO login works again.
- The existing password-grant login still works (don't regress it).
- RBAC still gates approver-only actions after SSO login.
- Add/adjust an E2E test for the SSO round-trip if feasible (or document the manual steps if the login rate-limiter makes it flaky, as PROGRESS notes).

---

## How to work
- Fix the two issues independently; they're unrelated. For each: diagnose → tell me the root cause → fix → verify against the evidence above → add a test/regression guard.
- No secrets in code/logs/traces. Keep existing suites green. Update `PROGRESS.md` (dated note) for both fixes.

Start by reading `PROGRESS.md`, then reply with your **diagnosis for both issues** (which of the numbered causes applies to each, based on inspecting the code + `.env` + container config) BEFORE changing code.

---

## Note to you (the human)
- Have `.env` open — both fixes likely hinge on values there (Langfuse keys/host; Keycloak client id/secret/redirect URIs). Confirm the Langfuse keys are copied from THIS instance's project settings (`localhost:3001` → Settings → API keys), not placeholders.
- For Keycloak, the classic gotcha is the redirect URI + the browser-vs-container hostname: the browser must be sent to a Keycloak URL it can actually open (e.g. `localhost:8080`), while the backend validates tokens against an issuer that may differ inside Docker. Tell Claude Code your exact ports if it asks.
```
