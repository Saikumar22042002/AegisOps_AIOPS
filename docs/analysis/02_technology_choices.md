# 02 — Technology choices, critically assessed

[← back to index](../../ANALYSIS.md)

For each major technology: **why it's here**, **its real role in the code**, and a **verdict** (keep / reconsider / replace) tied to this app's actual needs — not generic advice.

## Frontend — Next.js 14 (App Router) + React 18 + TypeScript + Zustand (no Tailwind styling, no shadcn)

**Why / role.** The app is a single authenticated dashboard behind a login wall. There is exactly one route (`app/page.tsx` → `AppRoot`); everything else is client-side view switching in a Zustand store (`lib/store.ts`). The design is a **pixel-exact port** of a source HTML file, rendered with verbatim inline styles and CSS-variable tokens copied into `globals.css`; **Tailwind preflight is explicitly disabled** (`tailwind.config.ts:corePlugins.preflight=false`) and Tailwind is essentially unused for styling. shadcn is not present. The only real streaming need is the SSE chat, done with a hand-written `fetch`-based POST-SSE client (`lib/sse.ts`) because `EventSource` is GET-only.

**Assessment.** For this app, Next.js's headline features (SSR, RSC, file-based routing, image optimization) are almost entirely unused — it's a client-rendered SPA that happens to be built with Next. That's fine, but it means Next is carrying weight it isn't paying back. Zustand is a good fit: the state model is a flat store with derived selectors, and the streaming reducer in `sendText` maps cleanly onto it. The hand-rolled SSE client is justified (POST + cookies rules out `EventSource`).

- **SSR vs SPA:** the app has no SEO/SSR need (auth-walled), so SPA is correct; Next is over-provisioned for it.
- **Streaming UI:** the token/step/console streaming is handled well in the store; no framework help was needed or used.
- **Enterprise dashboard:** the inline-style + design-token approach achieves pixel fidelity but sacrifices maintainability — every component is a wall of inline `style={{…}}` objects (see `components/Workspace.tsx`, `TopNav.tsx`). This will not scale to many screens without a real styling system.

**Verdict: KEEP Next.js + React + Zustand** (migration cost isn't worth it and the SSE integration is solid), **RECONSIDER the styling approach.** The verbatim-inline-style mandate is a design-parity decision that has hardened into tech debt; adopting the CSS variables with real CSS modules / a token-driven component library would cut the inline-style noise without changing pixels. Real alternatives (Remix, Vite SPA, TanStack) would be lateral moves — not worth it.

## Backend — FastAPI + Pydantic v2 + SSE + SQLAlchemy 2 / Alembic

**Why / role.** FastAPI gives async request handling (needed for SSE + concurrent SDK/LLM calls), Pydantic v2 does the settings + per-workflow input validation that is central to the safety story (`schemas/workflows.py`), and `sse-starlette` provides `EventSourceResponse`. SQLAlchemy 2 async + Alembic own the relational state.

**Assessment.** Strong fit. Pydantic is doing real safety work here (per-cloud machine-shape validators, CIDR validation, OS allowlists) — it's not decoration. FastAPI's dependency-injection is used for the auth/RBAC guards (`security/deps.py`). The one architectural weakness is not FastAPI's fault but the app's: run orchestration is a fire-and-forget `asyncio.create_task` with in-process SSE channels, which is why horizontal scaling breaks (see 09).

**Verdict: KEEP.** No credible reason to switch.

## Agent orchestration — LangGraph

**Why / role.** LangGraph's **durable checkpointing + dynamic interrupt** is the load-bearing reason it's here: the human-approval gate is a real `interrupt()` over a Postgres checkpointer (`agents/approval.py`, `agents/checkpointer.py`), resumable across restarts. The graph is a small, explicit state machine (`agents/graph.py`) — not an autonomous tool-calling loop.

**Assessment.** This is the right tool for *this* problem shape. The app needs: (a) a pause point that survives process death, (b) deterministic routing, (c) per-node observability. LangGraph delivers all three. Notably, the app does **not** use LangGraph's tool-calling/agentic autonomy — every node is hand-written Python that calls Gemini for classification/extraction/generation only. So most of "agent framework" value is unused; what's used is the checkpointed state machine.

**Alternatives:**
- **Plain async + a state column + Redis/DB checkpoint:** would work and remove a dependency, but you'd reimplement interrupt/resume and lose the span-tree ergonomics. Not worth it now.
- **Temporal:** a better fit *if* runs became long, multi-step, and needed retries/timeouts/versioned workflows at scale — Temporal's durable execution is industrial-grade compared to a Postgres checkpointer. Overkill today; worth revisiting if DevOps/SRE pipelines grow.
- **Other agent frameworks (CrewAI, AutoGen, LlamaIndex agents):** worse fit — they optimize for autonomous multi-agent chatter, which is exactly what this app deliberately avoids for safety.

**Verdict: KEEP LangGraph.** It's used for the one thing it's best at (durable HITL state machine). Revisit Temporal only if workflows lengthen.

## LLM — Google Gemini (`gemini-3.5-flash`) via `google-genai`

**Why / role.** Gemini is the reasoning engine inside the agents: JSON classification (`llm.classify_json`), NL parameter extraction, streamed answers (`llm.stream_answer`), and RAG embeddings (`aembed`, `gemini-embedding-001`). Real streaming with truncation-resilience is implemented.

**Assessment & swappability.** The integration itself is clean and defensive (retry, model-fallback resolution, usage/cost capture). **But model-swappability is not real** (see [ANALYSIS finding #9](../../ANALYSIS.md)): `POST /chat` ignores `body.model`; the client is a global singleton keyed only to `GEMINI_MODEL`; the frontend's provider menu (Claude/GPT/Gemini/Llama) is decorative. There is no `LLMProvider` interface — swapping providers means editing `integrations/gemini.py` and restarting. For an "AI-native" platform that advertises model choice in its UI, this is a gap.

Two smaller issues: `GeminiLLM.__init__` performs a **synchronous** `client.models.list()` network call inside `_resolve` (blocks the event loop on first `get_gemini`), and the singleton ignores later settings changes (annoying in tests).

**Verdict: KEEP Gemini as the default, but BUILD a provider abstraction.** Introduce an `LLMProvider` protocol (`classify_json`/`generate`/`astream`/`aembed`) with a Gemini implementation, wire `body.model` → provider selection, and make the model menu honest (or remove the non-Gemini entries). Also move `_resolve` off the constructor / make it lazy-async.

## Datastores — PostgreSQL + pgvector, Redis, Neo4j (three stores)

**Why each is here (from the code):**
- **Postgres** owns the durable relational truth: orgs/users/sessions/messages/runs/run_steps/approvals/feedback/documents+chunks(+embeddings)/audit_log/integrations/**resources** (inventory)/notifications — *and* the LangGraph checkpoints *and* the Langfuse DB. pgvector holds RAG embeddings with an HNSW cosine index; pg_trgm powers the keyword-search fallback.
- **Redis** holds ephemeral/coordination state: server-side sessions + OAuth PKCE state (`security/sessions.py`), multi-turn param-collection pending records (`agents/params.py`), idempotency keys (`security/idempotency.py`), one-time credential-reveal claims (`api/artifacts.py:_claim_reveal`), and a `runinput:` list (dead — see 09).
- **Neo4j** holds the per-run **context graph**: Context/Trigger/Intent/Agent/Workflow/Step/Tool/Reasoning/Evidence/Approval/Human/Outcome/Feedback + Resource/Run/Session provenance relationships (`graph_db/context_graph.py`).

**Is three stores justified?** Postgres and Redis are clearly justified — the roles don't overlap and Redis is doing real coordination work (idempotency NX, session TTL, one-shot reveal). **Neo4j is the questionable one.** What the graph actually delivers today: (a) a per-run audit trail that largely duplicates `run_steps` + `approvals` already in Postgres, and (b) resource↔run↔session provenance used by exactly one read path (`inventory.provenance` enriching a day-2 read). Every graph write is best-effort and wrapped in "never fails the run," and the app functions without it. The relationship queries in use are shallow (1–2 hops) and expressible in Postgres. The graph's *intended* value — cross-run correlation, incident-to-deploy graphs, "explore infrastructure as a live graph" — is **not implemented** (the Infrastructure module reads live AWS VPCs, not the graph).

**Verdict:** **KEEP Postgres + Redis.** **RECONSIDER Neo4j.** It is not over-engineered in principle (a context graph is a legitimate design), but *as currently used* it's a third operational dependency earning little that Postgres couldn't. Two honest options: (1) invest in it — build the cross-run correlation/incident-graph features that justify a graph DB; or (2) fold provenance into Postgres (a `resource_provenance` join) and drop Neo4j until the graph features exist. Don't leave it as a best-effort mirror.

## Auth — Keycloak OIDC + RBAC

**Why / role.** Real OIDC: password grant (the form) + Auth-Code/PKCE (SSO), JWKS validation, dual-issuer acceptance for the container-vs-browser host split, server-side sessions in Redis, refresh, logout. 8 realm roles → capability tiers (`security/rbac.py`).

**Assessment.** Solid and real. The dual-issuer fix (`integrations/keycloak.py:validate`) and browser-facing auth-URL rewrite (`build_auth_url`) are correct handling of a genuinely tricky dockerized-OIDC problem. The gap is **enforcement coverage, not the auth tech** — RBAC is enforced at `/approvals` but missing on credential-reveal and read endpoints (see 08/09), and the "RBAC re-checked at every side-effecting tool" claim in `CLAUDE.md` is not implemented (tools don't see the user).

**Verdict: KEEP Keycloak.** Fix the enforcement coverage in the API layer.

## Observability — Langfuse (v2) + OpenTelemetry + Prometheus + Grafana

**Why / role.** Langfuse: one trace per run (`trace_id == run_id`) with a nested span tree driven by `agents/timing.py`, LLM generations with token usage + computed USD cost, tool spans, redacted payloads. OTel: FastAPI auto-instrumentation + an `agent.run` span, exported OTLP→collector→Prometheus. Prometheus: custom `aegisops_*` metrics registry; Grafana dashboard provisioned.

**Assessment.** The Langfuse integration is genuinely good — the span-tree design (deterministic span ids so the approval span closes across the interrupt) is thoughtful, and cost math is real. This is the most polished non-core subsystem. Two caveats: it's Langfuse **v2** (v3 is the current line; a migration will come), and the artifact **Traces tab in the UI does not read Langfuse** — it returns static placeholder spans (`api/artifacts.py:184`), so the product surface doesn't reflect the real traces.

**Verdict: KEEP** all four. Wire the UI Traces tab to the real Langfuse trace (or link out to the Langfuse UI). Plan the v2→v3 migration.

## Infra execution — Terraform (+ Ansible, unused in graph)

**Why / role.** Terraform is the only mutation path; 14 curated, pinned, secure-by-default modules; cloud SDKs are read-only. The `TerraformRunner` (`tools/terraform.py`) does init/validate/plan(-out)/show-json/apply/destroy/output with per-resource state workspaces and raw-JSON capture for plan parsing. Ansible has a real runner (`tools/ansible.py`) but **no graph node calls it** — it's wired but unused.

**Assessment.** The Terraform choice is exactly right for a "human approves a plan, then it's applied" product — `plan` *is* the reviewable artifact, and state isolation solves the multi-resource problem. The implementation is careful (raw capture to avoid the redaction-corrupts-JSON bug; workspace-new without `TF_WORKSPACE`). Weaknesses: state is local-backend-on-a-volume (no locking, no remote state by default — a real risk for concurrency and durability), and Ansible is dead weight.

**Verdict: KEEP Terraform.** Move to a remote backend with locking (S3+DynamoDB is already env-configurable — make it the default for anything beyond a demo). Either wire Ansible into a real flow or drop it.

### One-line verdict table

| Tech | Verdict | Reason |
|------|---------|--------|
| Next.js + React + Zustand | Keep | SSE integration solid; SPA need met |
| Inline-style/design-token styling | Reconsider | Pixel-parity hardened into maintenance debt |
| FastAPI + Pydantic v2 + SQLAlchemy | Keep | Great fit; Pydantic does real safety work |
| LangGraph | Keep | Used for durable HITL state machine (its strength) |
| Gemini (`google-genai`) | Keep + abstract | Clean client, but swappability is fake — build a provider interface |
| PostgreSQL + pgvector | Keep | Core truth + vectors + checkpoints |
| Redis | Keep | Real coordination (idempotency/session/one-shot) |
| Neo4j | Reconsider | Best-effort mirror today; either invest or fold into PG |
| Keycloak OIDC | Keep | Real, correct dockerized-OIDC handling |
| Langfuse v2 | Keep | Best non-core subsystem; wire UI + plan v3 |
| OTel/Prometheus/Grafana | Keep | Standard, correctly wired |
| Terraform | Keep | Right model; move to remote state + locking |
| Ansible | Drop or wire | Present but unused by any node |
