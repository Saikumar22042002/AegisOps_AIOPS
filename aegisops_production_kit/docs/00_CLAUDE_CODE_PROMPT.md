# 🚀 PASTE THIS INTO CLAUDE CODE (first message)

> Open Claude Code in an **empty folder** in VS Code. Copy this entire kit into that folder
> first (so `design-reference/`, `docs/`, `CLAUDE.md`, `.env.example`, `docker-compose.yml`
> are all present). Then paste the message below.

---

You are building **AegisOps**, a production-grade, commercially-deployable agentic AIOps
platform. Build it as if it ships to enterprise customers on day one. **No mocks. No stubs.
No placeholders. No TODOs. No "future implementation" comments.** Every function you write
must be fully implemented with real logic, real SDKs, and real integrations. The only things
that come from outside the code are **runtime credentials** the operator places in `.env`
(API keys, cloud secrets) and the **backing services** started by `docker-compose`.

## Absolute rule #1 — the UI is a pixel-exact replica

`design-reference/AegisOps_Workspace_v3.SOURCE_OF_TRUTH.html` is the **visual source of
truth**. Open it in a browser. The app you build must look and behave **pixel-for-pixel
identical** — same layout, same dark/light/system themes, same colors, same IBM Plex
Sans/Mono fonts, same spacing, same animations, same hover states, same component structure,
same responsive behavior. Do **not** redesign or "improve" anything.

I have extracted the design for you so there is no guesswork:
- `design-reference/DESIGN_REFERENCE.tokens.css` — exact CSS tokens (dark+light), responsive
  breakpoints, keyframes, scrollbars. **Copy verbatim.** Never use an off-palette color.
- `design-reference/DESIGN_REFERENCE.template.html` — exact DOM + inline styles of every
  screen/region. Port each region to a component, preserving every inline style.
- `design-reference/DESIGN_REFERENCE.logic.js` — exact state model, all handlers, all the
  data shapes the UI expects. The UI's data **shapes** here are the contract your real
  backend must satisfy. The seed *values* (resource names, incident numbers, etc.) become
  real rows seeded into the database (see docs), not hard-coded constants.

Read all three completely before writing any UI code.

## Absolute rule #2 — everything is real

- **LLM:** Google **Gemini `gemini-3.5-flash`** (real, current GA model; alias
  `gemini-flash-latest`) via the `google-genai` SDK, used as the reasoning engine inside the
  agents. Model id from `GEMINI_MODEL` env.
- **Agents:** a real **LangGraph multi-agent** system (Router → CloudOps / DevOps / SRE /
  Knowledge-RAG / General, plus Approval + ServiceNow + Notification sub-graphs). Real graph,
  real checkpointing, real interrupts for human-in-the-loop, real tool-calling. Add more
  agents/tools if the features need them — you decide, but build them for real.
- **Infra execution:** real **Terraform** for create/modify/destroy (a real `TerraformRunner`
  that shells out to the `terraform` CLI: `init/validate/plan/apply/destroy`, parses real
  plan JSON, manages real state). Real **Ansible** for configuration. **No infra change
  happens without passing the human-approval interrupt** (see execution modes in docs). Real
  cloud **SDK reads** (boto3 / azure-sdk / google-cloud / pyVmomi / kubernetes) for
  discovery, availability pre-checks, drift detection, and post-apply verification — never
  for provisioning.
- **Integrations (all real):** ServiceNow (real REST client: SR/CR/INC create+update+close),
  GitHub (real API: repos, PRs, Actions), Keycloak (real OIDC auth + RBAC), Langfuse (real
  tracing SDK), OpenTelemetry (real traces/metrics to the Collector), Prometheus + Grafana
  (real metrics + dashboards).
- **Data:** real **PostgreSQL + pgvector** (app data + RAG embeddings), real **Redis**
  (cache, queues, agent shared state), real **Neo4j** (context graph). All started by
  `docker-compose`. Real Alembic migrations. Real seed script.
- **Transport:** real **SSE** for token/step/analysis/reference/confidentiality/VM-output
  streaming; REST for everything else.

## What to read next, in this order
1. `docs/01_REQUIREMENTS.md` — full functional + non-functional spec, phased build plan.
2. `docs/02_DESIGN_SPEC.md` — exact tokens, fonts, animations, responsive rules, component map.
3. `docs/03_ARCHITECTURE.md` — system architecture, the LangGraph agent graph, data flow,
   service topology, repo layout.
4. `docs/04_BACKEND_SPEC.md` — FastAPI app, every endpoint, the SSE contract, all integration
   clients, DB schema, RAG, observability, security.
5. `docs/05_AGENTS_SPEC.md` — each agent's responsibility, tools, state, interrupts, the
   execution-mode state machine, context-graph writes, Langfuse spans.
6. `docs/06_FEATURE_CHECKLIST.md` — the acceptance checklist = definition of done.
7. `docs/07_SETUP_AND_RUN.md` — docker-compose, migrations, seeding, running, testing.
8. `.env.example` — every runtime variable the operator fills in.

## How to work
- Build in the phases in `docs/01_REQUIREMENTS.md §Build plan`. After each phase, **run it**
  and verify against the source HTML / acceptance checklist before continuing.
- Maintain `PROGRESS.md` mirroring `docs/06_FEATURE_CHECKLIST.md`; check items off as you go.
- Write **real tests** as you build: pytest (unit + integration with testcontainers for
  PG/Redis/Neo4j), Vitest + RTL (frontend units), Playwright (E2E for the flows in the
  checklist). No skipped tests.
- Every external call has real error handling, retries with backoff, timeouts, and
  structured logging with correlation ids — no bare excepts, no swallowed errors.
- Secrets only via env. Never commit `.env`. Never hard-code a credential. `.gitignore` must
  exclude `.env`, state files, and `terraform.tfstate*`.

## Definition of done
1. `git clone` → `cp .env.example .env` (operator fills values) → `docker compose up -d` →
   `make migrate && make seed` → `make dev` starts the app.
2. The running UI is indistinguishable from the source HTML in dark, light, and mobile.
3. Every item in `docs/06_FEATURE_CHECKLIST.md` passes.
4. A real chat (“provision an EKS cluster, reuse the prod VPC”) runs end-to-end: Router →
   CloudOps agent → real cloud discovery → real `terraform plan` → confidentiality + plan
   shown in the artifact panel → **human approval interrupt** → on approve, real
   `terraform apply` → state update → context-graph write → ServiceNow updated → Langfuse
   trace + OTel spans recorded → verification → result streamed back. All visible in the UI
   exactly as the design depicts.
5. `pytest`, `npm test`, and `npx playwright test` are all green.
6. No grep hits for `TODO`, `FIXME`, `mock`, `placeholder`, `NotImplemented`, or `pass  #`
   in application code.

Start by reading the docs and the three design-reference files, then give me:
(a) a short build plan, (b) the exact repo/folder structure you'll create, and (c) the list
of services from `docker-compose.yml` you'll rely on — before you write code.
