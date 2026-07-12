# AEGISOPS — PRODUCTION HARDENING MASTER DIRECTIVE

Read this entire directive before touching anything. This is the execution contract for turning AegisOps from an impressive demo into a purely production solution — direct competition to Claude Code itself, with the one thing Claude Code doesn't have: a governed mutation boundary.

---

## 0. Context and authority order

You previously produced two document sets in this repo. Both stand. Read them again now, every file, before any work:

1. **`ANALYSIS.md` + `docs/analysis/01…13`** — your ground-truth read of the codebase. Accepted as-is. Where code and PROGRESS.md disagree, code wins.
2. **`FIX.md` + `docs/fix/01…08`** — your remediation blueprint. Accepted, WITH the amendments in this directive.
3. **`AEGISOPS_TARGET_ARCHITECTURE.md`** (repo root, newly added by the owner) — the final consolidated architecture. **Where it and FIX.md disagree, AEGISOPS_TARGET_ARCHITECTURE.md wins.** Read it in full; it resolves every open decision.

Be honest about where we are: the AegisOps you built has **serious problems**. P1–P20 are real. Three are Critical security holes (any authed user can exfiltrate any run's private key; multi-tenancy is fictional; reads/streams are unauthorized). Several surfaces are theater — policy checks hardcoded `True`, a static Traces tab, SRE remediation that reports success after doing nothing — in a product whose entire value proposition is that approvers can trust what they see. We are now fixing **all of them**. Not some. All.

**The bar:** every fix live and working end-to-end. No mocks. No dummies. No placeholders. No `return _ck(name, True)`. No `{"applied": True}` after a list call. Every capability either works for real against real datastores/clouds/UI, or it is explicitly labeled in the UI as "proposed, not executed" until it does. Anything that fakes success is a defect, full stop.

---

## 1. STAGE A — Amend the plan first (docs only, no app code)

Before implementation, fold the target architecture into your own FIX docs so the plan is one coherent contract. Amend in place — do NOT create a new document tree.

### A-1. `FIX.md` — update the locked-decisions table
Append these decisions (owner-signed, final):

| # | Decision | Locked answer |
|---|----------|---------------|
| 7 | Architecture philosophy | **Split-Trust**: deterministic Governed Core for all mutation (zero LLM trust) + Intelligent Shell (LLM planning/investigation, full autonomy read-only). Boundary = one mutating tool `execute_governed_step(cloud, resource, action, params)` whose interior is the existing pipeline: approved module → validate → plan → plan_guard → durable approval → apply (isolated remote-locked state) → verify → record. |
| 8 | U6 upgraded | The bounded planner becomes the **Governed Executive Loop**: LLM loop at the *planning* level on LangGraph/`create_agent` primitives. It drafts a **goal DAG** (each node = approved module + params, or read-only verification), gets **ONE approval for the whole DAG**, deterministic code executes steps via `execute_governed_step`, structured observations feed back, replans **deviating from the approved DAG trigger a fresh approval interrupt**. Hard bounds: max steps, max replans per step, budget ceiling. |
| 9 | Memory = Context Engine | M1–M4 implemented as five layers: **retrieval** (pgvector message embeddings + deterministic `get_turn` positional recall), **compression** (rolling LLM summary + context offloading — plans/logs stored as references, fetched on demand), **persistent memory** (per-user/org standing context under S0), **routing** (`build_context(session, budget, purpose)` into EVERY LLM call — router, cloudops, devops, sre, loop), **verification** (store-grounded answers; reconciliation verifies memory against actual cloud state). |
| 10 | D3 resolved = **INVEST** | Neo4j becomes the **live World Model + Reconciliation Engine**: cloud inventory, TF state refs, dependency edges, run/session provenance; continuous drift detection (all clouds, extend beyond AWS EC2), orphan sweep (closes P14 spend leak), impact analysis gating destroys ("what depends on this?"). Honest exit gate: if graph queries stay 1–2 hops after a quarter of real use, fold to Postgres and drop Neo4j. |
| 11 | Module Promotion Pipeline (new, Phase 3) | When no approved module exists, the agent may DRAFT one: generate → `terraform fmt`/`validate` → Checkov/tfsec scan → open as a PR-style **proposal for platform-engineer review**. Only after human promotion does it join the approved library. **Generation and execution NEVER happen in the same turn.** |
| 12 | Rejected on record | Ephemeral agent-per-tool swarms; trust-the-LLM mutation loops; runtime LLM-generated HCL execution; SDK/imperative mutation tiers ("emergency" paths); replacing LangGraph with Temporal now. Do not implement these even if a later prompt ambiguously suggests them — flag the conflict instead. |
| 13 | Decision gates | **Temporal**: trigger = long-running (hours+) / high-fan-out DAG workflows; revisit at Phase-3 exit with real data. **deepagents package**: read-only investigation agents only; re-evaluate at 1.0/LTS. Sub-agent spawning allowed for read-only work only; mutation is never delegated to a spawned agent. |

### A-2. Amend the affected fix docs
- `docs/fix/01_harness.md` — add the Intelligent Shell / Governed Core split and the Governed Executive Loop as the evolution of the §2.3 bounded planner.
- `docs/fix/03_fixes_memory_security_obs.md` — reframe M1–M4 as the five-layer Context Engine (keep the item IDs and acceptance tests; add context offloading and purpose-routing).
- `docs/fix/04_fixes_ux_data.md` — U6 rewritten per decision 8; D3 resolved per decision 10.
- `docs/fix/05_target_architecture.md` — reconcile with `AEGISOPS_TARGET_ARCHITECTURE.md` (that file is authoritative; update the mermaid + walkthrough to show the loop and world model at Phase 3).
- `docs/fix/07_roadmap.md` — re-emit the roadmap as the three phases in §3 below, each item with its acceptance test.

### A-3. Produce the execution plan
Append to `FIX.md` a **checklist of every work item in phase order** with: item ID, files touched, its acceptance test (from the fix docs), UI surface affected (from §5 below), and a status column you will keep updated as you implement. This checklist is the single progress tracker; also mirror status into `PROGRESS.md` as you go.

**STOP after Stage A and present the amended docs + checklist for owner review before writing any application code.**

---

## 2. Non-negotiable invariants (apply to every line you write)

1. **The LLM never authors HCL that executes.** Only the 14 (or later promoted) pre-written, pinned modules run, parameterized strictly via `-var`. The Module Promotion Pipeline produces *proposals*, never same-turn execution.
2. **Nothing mutates a cloud except Terraform**, through the governed pipeline. boto3/azure-mgmt/google-cloud remain read-only forever. No kubectl/SDK "emergency" mutation paths (K8s SRE actions in U2 are the one sanctioned exception — behind the same approval gate).
3. **plan_guard on every mutation path**, re-asserted at the approval choke-point (A2), and per-step in the executive loop's DAG.
4. **The durable approval interrupt is never bypassed, weakened, or made optional.** One approval may cover a whole DAG; deviation requires re-approval.
5. **Honesty is a feature.** No surface may claim something it didn't verify. If a capability isn't implemented, the UI says so.
6. **Tenancy and authz predicates on every query and endpoint** once S0 lands — no exceptions, no "internal" endpoints.
7. **High-blast-radius changes are feature-flagged** with the old path intact for rollback: `AEGISOPS_EVENT_BUS=memory|redis`, `AEGISOPS_TF_BACKEND=local|remote`, `AEGISOPS_EXEC_LOOP=off|on`, embedding writes flagged so a no-Gemini setup degrades to keyword recall.
8. **Do not break what's verified:** GCP VM full lifecycle, AWS EC2/S3 apply paths, SSE frame contract (the store reducer must not change), pixel-exact UI design system, the Emitter interface, Langfuse trace_id==run_id span tree. Regression tests stay green after every item.
9. **Verify library versions at implementation time** (LangGraph API, `create_agent`, Langfuse v2 client, psycopg checkpointer) — read the installed versions and current docs before using an API; do not code from memory.
10. **Secrets discipline:** sensitive TF outputs never enter logs/DB/chat/context; redaction backstop (S4) on everything persisted; plan-JSON sensitivity invariant gets its own test.

---

## 3. STAGE B — Implement, phase by phase

Work strictly in phase order. Within a phase, item by item: implement → write/extend the acceptance test → run the full test suite → update the FIX.md checklist + PROGRESS.md → commit with a descriptive message. Do not start a phase until the previous phase's exit gate passes.

### PHASE 1 — Trustworthy (blocking; nothing ships until this is green)
Items (specs + acceptance tests are in the fix docs; implement exactly those):
- **S0** real multi-tenancy: principal→(org_id, user_id) in `security/deps.py`, kill every `get_default_org()` call site, populate `Session.user_id`, org predicates on every query. Seed **two orgs with users** so isolation is demonstrable in the UI.
- **S1** credential reveal: initiator-or-approver + org-scope + **mandatory step-up re-auth** (fresh Keycloak proof ≤120s) + **always-on audit row on every attempt** (success and denial). Frontend: a re-auth modal on the Reveal button flow.
- **S2** `authorize_run`/`authorize_session` on every read/stream endpoint; 404 on mismatch.
- **S3** `require_initiator` on `POST /chat`.
- **S4** redaction backstop on persisted `answer`/`outcome`.
- **S5** capability assertion at the execute node.
- **A1 + B7** idempotency wait-or-abort (never fall through to apply) + endpoint guard against a second `/approvals` while one is live.
- **A2** plan_guard re-asserted at the approval node.
- **A4** org-scoped duplicate-name check.
- **A5** `runs.initiated_by` + policy-configurable 4-eyes for Production.
- **B5** terminal-state guarantee (finally-block + fault-injection test).
- **B6** zero blocking I/O on the event loop (fix `inventory.reconcile`; grep-audit all agents for sync SDK calls; fix the Gemini sync-constructor resolve, P18).
- **U4** "Auto (ask me)" cloud option as the store default; selector-as-hint logic.
- **Honesty labels (cheap now, real in P2):** policy checks card shows "not evaluated" for any check not yet a real predicate; SRE outcomes say "proposed, not executed"; Traces tab shows "trace view coming — open in Langfuse" deep-link instead of fake spans.
- **O2** Langfuse project-key startup assertion.
- **C1** per-cloud plan-assertion tests for ingress/CIDR rules.
- **D1** hot-path indexes; **D4** repo/state hygiene (purge tracked `*.tfplan`, gitignore, move dev TF state off the OneDrive bind-mount).

**Phase 1 exit gate (all must pass):** two seeded orgs are fully isolated in API and UI (cross-org UUID → 404); read-only role cannot initiate; initiator cannot self-approve a prod run; concurrent double-approve produces exactly one apply; reveal without fresh auth → 401, every attempt audited; no surface anywhere claims something it didn't do; full regression suite green.

### PHASE 2 — Production harness + Context Engine
- **B1** Redis Streams event bus (`run:<id>:events`, XADD/XREAD BLOCK, exactly-once by stream id, TTL on terminal). Emitter interface unchanged. Multi-worker test: publish from worker A, consume from worker B. Feature-flagged.
- **B2** RunSupervisor (tracked tasks + Redis heartbeat + graceful drain).
- **B3** stranded-run reconciler (resume from checkpoint or mark failed honestly; kill-mid-apply integration test — with A1 proving no double apply).
- **B4** verification cross-cloud (Azure/GCP branches, timeout-bounded, thread-offloaded).
- **A3** remote TF backend with locking (S3+DynamoDB via `-backend-config`), unique plan-file per run, migration documented; local stays the dev default.
- **Latency pass:** skip `terraform init` when initialized, `TF_PLUGIN_CACHE_DIR` on a named volume. Target: warm provisioning turn reaches the approval card in ≤15s (measure and record before/after in PROGRESS.md).
- **M1/M2/M3** Context Engine core: `build_context(session, budget_tokens, purpose)` threaded into **every** LLM call; message embeddings on write + per-session semantic retrieval; deterministic `get_turn` positional recall; rolling summary replacing the 160-char digest; router purpose-slice replacing last-8-turns. **The headline acceptance test:** seed a 100-message session → "what was my 20th question?" returns turn 20 verbatim, in the UI.
- **Context offloading:** plan JSON / apply logs / discovery dumps stored as artifacts, referenced (not inlined) in LLM context, fetched on demand.
- **U1** real policy checks — every `_*_policy` becomes a real predicate over `validated` + `terraform show -json`. A plan with encryption disabled must render a **failed** check in the approval card.
- **Defaults honesty:** whenever a dependency reference is silently defaulted (default VPC/subnet on aws-ec2, default network on gcp-gce, auto-created RG on azure-vm), the approval card must state the defaulted value explicitly ("placing in default VPC vpc-0abc"). No invisible placement decisions.
- **U2** SRE real: Prometheus deploy-annotation signal replaces the hardcoded `recent_deploy:True`; real K8s rollback/scale/restart via `tools/kubernetes.py` when configured; "proposed, not executed" otherwise.
- **U3** LLMProvider protocol + GeminiProvider + `get_provider(body.model)`; UI model menu trimmed to what's real.
- **O1** real Traces tab: run_steps-derived span tree (durations, order) + Langfuse deep-link. No `—` placeholders.
- **O3** metrics hygiene (wire AGENT_STEP_DURATION/TOOL_RETRIES or remove; exempt SSE route from the rate limiter).
- **D2** inventory row written in the same txn as the run outcome; orphan sweeper extends B3.
- **U5** wire mid-run input to `CommandConsole.send_input` via the supervisor, or remove the endpoint + key entirely (pick based on whether any tool flow needs it; document the choice).
- **U8** SSE contract regression suite green against the Redis bus.

**Phase 2 exit gate:** kill the API worker mid-apply → run recovers to a terminal state exactly once; UI streaming survives multi-worker deployment and reconnect; turn-20-of-100 recall works in the UI; a bad plan shows a real failed policy check; Traces tab shows real spans; model menu is honest.

### PHASE 3 — Intelligence layer (the competitive edge)
- **World Model + Reconciliation Engine** (per decision 10): schema for resources/dependencies/runs in Neo4j; ingestion from apply outputs + read-only discovery; continuous reconcile job (drift events surfaced as notifications in the UI bell + a drift panel); orphan detection; `impact_of(resource)` query wired into the destroy path ("2 resources depend on this — proceed?" in the approval card).
- **Dependency closure resolution (first-class item):** every resource request resolves its dependency references in this strict order: (1) **user-named** value; (2) **World Model lookup** — live graph query for suitable existing infrastructure (org-scoped, e.g. "which VPCs exist in us-east-1?"), asking the user to pick when several qualify; (3) **module default** — allowed only when it exists AND is stated on the approval card; (4) **doesn't exist at all → the executive loop proposes a goal DAG that creates the dependency first** (VPC before EC2, resource group before storage account), one approval for the whole chain. Never silently place a resource in infrastructure the user can't see, never hard-fail on a missing dependency the platform can create.
  Acceptance tests: (a) "create an EC2" with no default VPC and no VPC in the world model → DAG proposal VPC→EC2, one approval, EC2 lands in the new VPC; (b) "create a storage account" with no existing RG → DAG proposal RG→storage (closes today's hard requirement for a pre-existing RG); (c) "provision an EKS cluster" → world model offers existing VPCs/subnets to pick from instead of demanding raw IDs; (d) two suitable VPCs exist → the agent asks which, never guesses.
- **Governed Executive Loop** (per decision 8): `execute_governed_step` tool wrapping the existing pipeline; loop on LangGraph/`create_agent`; goal-DAG approval artifact (new UI card: ordered steps, per-step plan summaries + policy checks, ONE approve/reject); live per-step progress in the timeline; deviation → re-approval card; bounds enforced and tested. Acceptance: **"create a VPC and an EC2 inside it"** → one DAG approval → both applied in order, EC2 in the new VPC; **"…and an EFS mounted on it"** exercises replan-on-failure if the first mount attempt fails. (If an EFS module doesn't exist, that's the Module Promotion Pipeline's first real proposal.)
- **Read-only investigation agents:** SRE triage and multi-cloud discovery as loop-until-done agents with read-only tools; sub-agent spawning allowed here only. deepagents package permitted here only.
- **Module Promotion Pipeline** (per decision 11): draft → fmt/validate → Checkov/tfsec → proposal artifact + UI review surface for platform-admin → promote into `infra/terraform-workspaces/` + `templates.py` registration. Never same-turn execution; test asserts a drafted module cannot be selected until promoted.
- **M4** per-user/org persistent memory (preferences, naming conventions), user-editable, surfaced into `build_context`.
- **U7** retry-with-fix on classified provider errors + "undo last apply" via the gated destroy path.
- **Modify beyond ports** (S3 lifecycle/versioning, RDS scaling, tags) using the existing modify framework.
- **Cost estimation** feeding a real policy check + the approval card (Infracost or provider pricing — verify tooling at impl time).
- **P16** DevOps CI polling; **P17** notify real recipients.

**Phase 3 exit gate:** the VPC→EC2 DAG demo passes end-to-end in the UI; a deliberate drift (manually change a SG in the console) surfaces as a drift notification; destroy of a depended-on resource warns from the world model; a drafted module goes through proposal→review→promotion and only then becomes usable.

---

## 4. Working method
- One item at a time. Never batch unrelated changes into one commit.
- Every item lands **with its acceptance test** — the test names are already specified in the fix docs; implement those exactly, plus any regression test the item implies.
- Run the full suite (backend pytest tiers + frontend vitest + Playwright where touched) before every commit. A red suite blocks the next item.
- Keep `FIX.md`'s checklist and `PROGRESS.md` current — status, evidence (test names, measured latencies), and any deviation from this directive with its reason.
- If you hit a genuine conflict between this directive, the fix docs, and the code — **stop and ask the owner**; do not silently pick.
- If a step needs credentials/infrastructure you don't have (remote state bucket, second cloud grant), implement + feature-flag + test with what exists, and record exactly what's pending in PROGRESS.md. Never fake the missing part.

## 5. UI/UX contract — the user must be able to DO and SEE everything
Every backend change surfaces in the UI, matching the existing pixel design system (inline styles + tokens in `globals.css`; do not introduce a new styling approach). 100% working means a human can click through all of this:

1. **Tenancy visible:** log in as org-A user → only org-A sessions/runs/inventory anywhere in the UI; org-B UUIDs 404.
2. **Roles honest:** read-only user sees no composer send for actionable requests (clear message), sees no Approve/Reject; initiator sees Approve/Reject only if approver.
3. **Reveal flow:** Reveal → step-up re-auth modal → value shown once → second attempt shows "already revealed"; audit visible to platform-admin.
4. **Cloud selector:** defaults to "Auto (ask me)"; ambiguous VM request → the clarifying question actually appears.
5. **Model menu:** only real options; selection actually changes the provider.
6. **Approval card:** real policy checks with real pass/**fail** states; cost estimate (Phase 3); world-model impact warnings on destroy (Phase 3).
7. **Traces tab:** real span tree with durations + "Open in Langfuse".
8. **SRE:** remediation outcomes labeled truthfully; real actions when K8s configured.
9. **Memory in the UI:** "what was my 20th question?" answered verbatim; "my usual region" honored (Phase 3).
10. **Executive loop UI (Phase 3):** goal-DAG approval card, live step-by-step timeline, deviation re-approval, honest partial-failure reporting ("steps 1–2 applied, step 3 failed: …").
11. **Drift & orphans (Phase 3):** notification bell + a panel showing drift/orphan findings from the reconciliation engine.
12. **Streaming:** token/step/console streaming stays as smooth as today on the Redis bus, including reconnect mid-run and after approval.
Manual click-through per phase + Playwright e2e for flows 1–5, 7, and 12 at minimum.

## 6. Definition of done
All P1–P20 closed with passing acceptance tests. All three phase exit gates demonstrated. The FIX.md checklist 100% green with evidence. PROGRESS.md tells the true story. A new engineer can read ANALYSIS → FIX → AEGISOPS_TARGET_ARCHITECTURE and find the code matches the documents.

Begin with Stage A now. Do not write application code until the amended plan is approved.
