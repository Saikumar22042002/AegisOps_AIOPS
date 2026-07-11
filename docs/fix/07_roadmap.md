# §6 — Phased execution roadmap

[← back to FIX index](../../FIX.md)

> **Re-emitted at Stage A (2026-07-11)** per the owner's production directive and [`AEGISOPS_TARGET_ARCHITECTURE.md`](../../AEGISOPS_TARGET_ARCHITECTURE.md). The former Phase 0 is folded into Phase 1. Three phases, each **independently shippable**, **gated by tests and an exit gate**, ordered by dependency. Every item lands with its acceptance test; a red suite blocks the next item. Sizes **S/M/L**; blast low/med/high.
>
> The bar for every item: **live and working end-to-end. No mocks, no dummies, no placeholders.** A capability either works for real against real datastores/clouds/UI, or the UI explicitly labels it "proposed, not executed" / "not evaluated" until it does.

---

## Phase 1 — Trustworthy (blocking; nothing ships until this is green)

**Goal:** make the security/governance claims true and stop the correctness bleeding. **S0 goes first** (S1/S2/A4/A5 depend on it).

| Item | Ref | Size | Blast | Files | Acceptance test |
|------|-----|------|-------|-------|-----------------|
| Real multi-tenancy: principal→(org_id,user_id), kill every `get_default_org()` call site, populate `Session.user_id`, org predicates on every query; **seed two orgs with users** | S0/P2 | L | high | `security/deps.py`, `schemas/auth.py`, `db/repositories.py`, all `api/*`, `seed/seed.py` | `test_tenancy.py`: two orgs → isolation on sessions/runs/inventory/knowledge; cross-org UUID read → 404; isolation demonstrable in the UI |
| Credential reveal: initiator-or-approver + org-scope + **mandatory step-up re-auth** (fresh Keycloak proof ≤120s) + **always-on audit row on every attempt**; frontend re-auth modal on the Reveal flow | S1/P1 | M | medium | `api/artifacts.py`, `security/deps.py` (`require_fresh_auth`), `frontend/…CredentialReveal` | `test_rbac_endpoints.py`: non-owner/non-approver → 404; owner w/o fresh proof → 401; with fresh proof → value once, 2nd → 410; **every** attempt (success and denial) writes an `audit_log` row |
| `authorize_run`/`authorize_session` on every read/stream endpoint; 404 on mismatch | S2/P3 | M | medium | `api/artifacts.py:_load`, `api/chat.py:get_run`/`chat_stream`, `api/sessions.py` | `test_tenancy.py`: cross-user/cross-org read of every tab + the stream → 404 |
| `require_initiator` on `POST /chat` | S3 | S | low | `api/chat.py` | read-only role POST /chat → 403 with a clear message; can still view |
| Redaction backstop on persisted `answer`/`outcome` | S4/P20 | S | low | `api/chat.py:_persist_result` | `test_redaction.py`: planted secret in `answer` masked in persisted `messages.content` |
| Capability assertion at the execute node | S5 | S | low | `agents/execute.py` | execute node refuses if the run's initiator lacks execute capability |
| Idempotency wait-or-abort (never fall through to apply) + endpoint guard against a second `/approvals` while one is live + lifecycle primitive | A1+B7/P5 | S | low | `agents/cloudops.py:cloudops_execute`, `security/idempotency.py`, `api/chat.py:resolve_approval` | `test_idempotency.py`: two concurrent executes → exactly one apply, the other waits or aborts 409-style; claim→in_progress→done transitions; concurrent double-approve → exactly one apply |
| plan_guard re-asserted at the approval choke-point | A2 | S | low | `agents/approval.py` | `test_safety_invariants.py`: approval node refuses a mismatched plan even if a plan node skipped the guard |
| Org-scoped duplicate-name check | A4 | S | low | `agents/cloudops.py:314` | dup-name test asserts the check carries the real org |
| `runs.initiated_by` + policy-configurable 4-eyes for Production | A5/P15 | S | low | migration, `api/chat.py`, `agents/approval.py` | prod run: initiator self-approve → 403; non-prod → allowed |
| Terminal-state guarantee (finally-block) | B5 | S | low | `api/chat.py:_drive` | fault-injection: raise inside `_persist_result` → run ends `failed`, never stuck `running` |
| Zero blocking I/O on the event loop: fix `inventory.reconcile`; grep-audit all agents for sync SDK calls; fix the Gemini sync-constructor resolve | B6/P6/P18 | S | low | `agents/inventory.py:reconcile`, `integrations/gemini.py:_resolve` | concurrent reconcile + fast endpoint stay responsive; grep for `boto3.client(`/sync SDK outside `tools/` is clean |
| "Auto (ask me)" cloud option as the store default; selector-as-hint | U4/P11 | S | low | `frontend/lib/store.ts`, `api/chat.py:40`, `agents/cloudops.py:resolve_cloud` | with Auto, "provision a virtual machine" → the clarifying question actually appears in the UI; with AWS pinned → AWS |
| **Honesty labels** (cheap now, real in P2/P3): policy card shows "not evaluated" for any non-real check; SRE outcomes say "proposed, not executed"; Traces tab shows "trace view coming — open in Langfuse" deep-link | P7/P8/P9 labels | S | low | `agents/templates.py`, `agents/sre.py`, `api/artifacts.py:traces`, frontend tabs | no surface anywhere claims something it didn't verify; Langfuse deep-link opens the run's real trace |
| Langfuse project-key startup assertion | O2 | S | low | `main.py`/`integrations/langfuse_client.py` | startup log asserts the key/project; tracing tests stay green |
| Per-cloud plan-assertion tests for ingress/CIDR rules | C1 | S | low | tests only | collected `allowed_cidr`/`ingress_ports` produce the expected SG/NSG/firewall rules in `terraform show -json`, per cloud |
| Hot-path indexes | D1 | S | low | migration, `db/models.py` | `EXPLAIN` shows index scans on transcript/run queries |
| Repo/state hygiene: purge tracked `*.tfplan`, gitignore, move dev TF state off the OneDrive bind-mount | D4 | S | low | `.gitignore`, compose | `git status` clean of plan/state files; warm plan measurably faster off OneDrive |

**Dependencies:** S0 → (S1, S2, A4, A5). **Risk mitigation:** feature flag `AEGISOPS_TENANCY=strict|legacy` with a parallel-run window.

**Phase 1 exit gate (all must pass):** two seeded orgs fully isolated in API **and UI** (cross-org UUID → 404) · read-only role cannot initiate · initiator cannot self-approve a prod run · concurrent double-approve produces exactly one apply · reveal without fresh auth → 401, every attempt audited · **no surface anywhere claims something it didn't do** · full regression suite green.

---

## Phase 2 — Production harness + Context Engine

**Goal:** scalable, crash-safe, honest, and it *remembers*. **Order:** B1 → B2 → B3; M1 → M2/M3.

| Item | Ref | Size | Blast | Files | Acceptance test |
|------|-----|------|-------|-------|-----------------|
| Redis Streams event bus (`run:<id>:events`, XADD/XREAD BLOCK, exactly-once by stream id, TTL on terminal); `Emitter` interface unchanged; flag `AEGISOPS_EVENT_BUS=memory\|redis` | B1/P4 | L | high | `agents/events.py`, `api/chat.py:_sse` | SSE contract ported (exactly-once, replay-after-id, done stops); **multi-worker test: publish from worker A, consume from worker B**; stream TTL-evicts on terminal |
| RunSupervisor (tracked tasks + Redis heartbeat + graceful drain) | B2 | M | medium | new `agents/supervisor.py`, `main.py` | kill supervisor mid-run → heartbeat expires observably; shutdown persists in-flight runs as `failed` with a real message |
| Stranded-run reconciler (resume from checkpoint or mark failed honestly) | B3 | M | medium | new `agents/reconciler.py`, `main.py` | **kill-mid-apply integration test**: run recovers to a terminal state **exactly once** (A1 proves no double apply) |
| Verification cross-cloud (Azure/GCP branches, timeout-bounded, thread-offloaded) + honest cards | B4/C2 | S | low | `agents/finalize.py`, `agents/cards.py` | per-cloud: mock slow SDK → warn card, never hangs; card `host`/`connection` reflect reconciled values |
| Remote TF backend with locking (S3+DynamoDB via `-backend-config`), unique plan-file per run, migration documented; local stays dev default; flag `AEGISOPS_TF_BACKEND=local\|remote` | A3/P12 | M | medium | `tools/terraform.py`, module init path | `test_safety_live.py` green against the remote backend; two concurrent plans never collide on a plan file |
| Latency pass: skip `terraform init` when initialized; `TF_PLUGIN_CACHE_DIR` on a named volume | A3-lat/D4 | M | medium | `tools/terraform.py`, compose | **warm provisioning turn reaches the approval card in ≤15s** — measured, before/after recorded in PROGRESS.md |
| Context Engine core: `build_context(session, budget_tokens, purpose)` into **every** LLM call; message embeddings on write + per-session semantic retrieval; deterministic `get_turn`; rolling summary replacing the 160-char digest; router purpose-slice replacing last-8-turns; embedding writes feature-flagged (no-Gemini degrades to keyword recall) | M1/M2/M3 | M×3 | medium | `agents/memory.py`, message-write path, migration, all agents' prompt assembly, `agents/router.py` | **Headline:** seed a 100-message session → "what was my 20th question?" returns turn 20 **verbatim, in the UI**; semantic recall finds a turn by content; router resolves a 30-turn-old reference; 40-turn context stays within budget |
| Context offloading: plan JSON / apply logs / discovery dumps stored as artifacts, referenced (not inlined), fetched on demand | M5 | M | medium | `agents/memory.py`, agent prompt assembly | long session with multiple plans stays in budget; a question about a prior plan answers from the stored artifact |
| Real policy checks: every `_*_policy` becomes a real predicate over `validated` + `terraform show -json` | U1/P8 | M | medium | `agents/templates.py` | a plan with encryption disabled renders a **failed** check in the approval card; `test_templates.py` asserts checks reflect the plan |
| **Defaults honesty:** silently-defaulted dependency references (default VPC/subnet on aws-ec2, default network on gcp-gce, auto-created RG on azure-vm) stated explicitly on the approval card | new | S | low | `agents/cloudops.py`, approval card payload, frontend | approval card shows e.g. "placing in default VPC vpc-0abc" — no invisible placement decisions |
| SRE real: Prometheus deploy-annotation signal replaces hardcoded `recent_deploy:True`; real K8s rollback/scale/restart via `tools/kubernetes.py` when configured; "proposed, not executed" otherwise | U2/P7 | M | medium | `agents/sre.py`, `tools/kubernetes.py` | fake K8s: `restart` issues the real patch and reports it; no K8s: "proposed, not executed" — never `applied:True` |
| LLMProvider protocol + GeminiProvider + `get_provider(body.model)`; UI menu trimmed to what's real | U3/P10 | M | medium | new `integrations/llm/`, `agents/llm.py`, `frontend/lib/data.ts` | `body.model` selects the provider; unknown model → clear error; menu shows only real options and selection changes the provider |
| Real Traces tab: run_steps-derived span tree (durations, order) + Langfuse deep-link | O1/P9 | M | low | `api/artifacts.py:traces` | traces endpoint returns the run's real step tree; no `—` placeholders for a timed run |
| Metrics hygiene: wire AGENT_STEP_DURATION/TOOL_RETRIES or remove; exempt SSE route from the rate limiter | O3/P19 | S | low | `main.py`, `metrics.py`, `agents/timing.py` | `/metrics` shows non-empty series after a run (or the metrics are gone); a long SSE stream isn't rate-limited |
| Inventory row written in the same txn as the run outcome; orphan sweeper extends B3 | D2/P14 | M | medium | `agents/cloudops.py`, reconciler | fault-inject crash between apply and inventory → same-txn write prevents the orphan or the sweeper flags it |
| Mid-run input: wire to `CommandConsole.send_input` via the supervisor, **or** remove the endpoint + `runinput:` key entirely; document the choice | U5/P13 | S | low | `api/chat.py`, supervisor | if wired: an interactive prompt answered end-to-end; if removed: zero `runinput:` references remain |
| SSE contract regression suite green against the Redis bus (store reducer unchanged) | U8 | S | low | frontend tests | `sse.test.ts` + `markdown.test.tsx` + Playwright streaming specs green on `AEGISOPS_EVENT_BUS=redis` |
| DevOps CI poll-to-completion | P16 | S | low | `agents/devops.py` | dispatched run id captured and polled to completion with a timeout — never reads the previous run *(may slip to Phase 3 per the master directive; tracked there)* |

**Phase 2 exit gate:** kill the API worker mid-apply → run recovers to a terminal state exactly once · UI streaming survives multi-worker deployment and reconnect · turn-20-of-100 recall works in the UI · a bad plan shows a real failed policy check · Traces tab shows real spans · model menu is honest.

---

## Phase 3 — Intelligence layer (the competitive edge)

**Goal:** the Split-Trust differentiators — world model, executive loop, promotion pipeline. All per decisions 7–13.

| Item | Ref | Size | Blast | Files | Acceptance test |
|------|-----|------|-------|-------|-----------------|
| **World Model + Reconciliation Engine** (decision 10): Neo4j schema for resources/dependencies/runs; ingestion from apply outputs + read-only discovery; continuous reconcile job (drift events → UI bell + drift panel); orphan detection; `impact_of(resource)` wired into the destroy path | D3 | L | high | `graph_db/*`, `agents/inventory.py`, reconciler, frontend bell/panel | deliberate manual drift (change an SG in the console) surfaces as a drift notification; destroy of a depended-on resource warns "N resources depend on this" in the approval card; orphan sweep finds an unrecorded resource |
| **Dependency closure resolution** (first-class): strict order — (1) user-named; (2) World Model lookup (org-scoped, ask when several qualify); (3) module default only if stated on the card; (4) missing → executive loop proposes a DAG creating the dependency first. Never silently place; never hard-fail on a creatable dependency | new | M | medium | `agents/cloudops.py`, loop, world model | (a) EC2 with no default VPC and none in the model → DAG proposal VPC→EC2, one approval, EC2 lands in the new VPC; (b) storage account with no RG → DAG RG→storage; (c) EKS → model offers existing VPCs/subnets to pick; (d) two suitable VPCs → the agent **asks**, never guesses |
| **Governed Executive Loop** (decision 8): `execute_governed_step` tool wrapping the existing pipeline; loop on LangGraph/`create_agent`; goal-DAG approval artifact (new UI card: ordered steps, per-step plan summaries + policy checks, ONE approve/reject); live per-step timeline; deviation → re-approval card; bounds enforced; flag `AEGISOPS_EXEC_LOOP=off\|on` | U6 | L | high | new loop graph in `agents/`, `agents/graph.py`, frontend DAG card | "create a VPC and an EC2 inside it" → one DAG approval → both applied in order, EC2 in the new VPC; "…and an EFS mounted on it" exercises replan-on-failure → deviation re-approval; bounds (max steps/replans/budget) enforced and tested; honest partial-failure reporting |
| **Read-only investigation agents:** SRE triage + multi-cloud discovery as loop-until-done agents with read-only tools; sub-agent spawning allowed here only; deepagents package permitted here only | new | M | medium | `agents/sre.py`, discovery, new investigation module | investigation agents hold only read-only tools (asserted in a test); mutation is never delegated to a spawned agent |
| **Module Promotion Pipeline** (decision 11): draft → `fmt`/`validate` → Checkov/tfsec → proposal artifact + UI review surface for platform-admin → promote into `infra/terraform-workspaces/` + `templates.py` registration | new | L | high | new pipeline module, `agents/templates.py`, frontend review surface | a drafted module goes proposal→review→promotion and **only then** becomes selectable; test asserts a drafted module cannot be selected until promoted; generation and execution never in the same turn |
| Per-user/org persistent memory (preferences, naming conventions), user-editable, surfaced into `build_context` | M4 | M | medium | new `user_memory`, `agents/memory.py`, frontend editor | a new session honors "my usual region"/"my usual VPC" from user memory; the user can view/edit it in the UI |
| Retry-with-fix on classified provider errors + "undo last apply" via the gated destroy path | U7 | M | medium | `agents/cloudops.py`, `provider_errors.py`, graph edges | a bad-region failure surfaces "retry in <region>" one-click; "undo that" destroys the last resource via the gated flow |
| Modify beyond ports: S3 lifecycle/versioning, RDS scaling, tags via the existing modify framework | new | M | medium | `agents/cloudops.py:_modify_resource`, schemas, modules | each new modify type plans in the resource's own state, passes the in-place guard, and applies behind the gate |
| Cost estimation feeding a real policy check + the approval card (Infracost or provider pricing — **verify tooling at impl time**) | new | M | medium | new cost module, `agents/templates.py`, approval card | approval card shows a real $/mo estimate; a guardrail breach renders a failed policy check |
| DevOps CI polling (if not closed in Phase 2) | P16 | S | low | `agents/devops.py` | see Phase 2 row |
| Notify real recipients (initiator/approver/team, configurable) | P17 | S | low | `agents/notify.py` | notification email addresses the initiator/approver, not the sender |

**Phase 3 exit gate:** the VPC→EC2 DAG demo passes end-to-end in the UI · a deliberate drift (manually change an SG in the console) surfaces as a drift notification · destroy of a depended-on resource warns from the world model · a drafted module goes through proposal→review→promotion and only then becomes usable.

---

## Cross-phase discipline

- **One item at a time**; every item lands with its acceptance test; run the full suite (backend pytest tiers + frontend vitest + Playwright where touched) before every commit; a red suite blocks the next item.
- **Regression protection (invariant 8):** GCP VM full lifecycle, AWS EC2/S3 apply paths, SSE frame contract (store reducer unchanged), pixel-exact UI design system, the `Emitter` interface, Langfuse trace_id==run_id span tree — green after every item.
- **Feature flags on every high-blast change:** `AEGISOPS_TENANCY`, `AEGISOPS_EVENT_BUS=memory|redis`, `AEGISOPS_TF_BACKEND=local|remote`, `AEGISOPS_EXEC_LOOP=off|on`, embedding writes flagged (no-Gemini degrades to keyword recall).
- **Verify library versions at implementation time** (LangGraph API, `create_agent`, Langfuse v2 client, psycopg checkpointer) — read installed versions + current docs before using an API.
- **Two honestly-noted test gaps close in Phase 2:** kill-mid-interrupt restart-resume; full apply→day-2 browser E2E.
- Status tracked in the [FIX.md execution checklist](../../FIX.md#8--execution-plan-checklist-stage-a-single-progress-tracker), mirrored into `PROGRESS.md`.

## Decisions — resolved and remaining

**Resolved (owner-signed, final — do not re-open):**
1. **Split-Trust + `execute_governed_step`** boundary (decision 7). 2. **Governed Executive Loop** replaces the bounded planner (decision 8). 3. **Context Engine** five layers (decision 9). 4. **Neo4j = INVEST** as World Model, with the honest fold-to-Postgres exit gate (decision 10). 5. **Module Promotion Pipeline** in Phase 3 (decision 11). 6. **Rejected on record:** agent-per-tool swarms, LLM mutation loops, runtime HCL execution, SDK mutation tiers, Temporal-now (decision 12). 7. **Remote TF backend = S3+DynamoDB** via `-backend-config` (A3). 8. **Cost estimation = build it** (Phase 3). 9. **SRE remediation is always approval-gated** — no ungated auto-remediation; K8s SRE actions are the one sanctioned non-Terraform mutation, behind the same gate. 10. **Model menu trimmed to Gemini now**, `LLMProvider` interface for later breadth (U3). 11. **Step-up re-auth:** fresh Keycloak proof ≤120s with a frontend re-auth modal (password re-entry first cut).

**Remaining (confirm at the flagged moment):**
1. **Tenancy source of truth** (at S0 impl): Keycloak group/claim vs seeded `users` table. *Recommendation stands: Keycloak claim → mirrored into `users`.*
2. **Temporal** (Phase-3 exit gate, decision 13): adopt only if DAG workflows become long-running (hours+) / high-fan-out.
3. **deepagents package** (decision 13): read-only investigation agents only; re-evaluate at 1.0/LTS.
4. **OPA/Conftest vs native predicates** for policy tier 2 (Phase 3): decide when tier-1 predicates are live.
5. **Langfuse v2→v3** (Phase 3): verify the v3 SDK shape at that time.
