# AegisOps — Remediation & Re-Architecture Plan (FIX.md)

> **Status: BLUEPRINT. No code changes until this is approved.** This plan is the execution contract for later staged passes. Every item is grounded in [`ANALYSIS.md`](ANALYSIS.md) and the real code, cited as `path/file.py:symbol` + the `ANALYSIS §` / `Pnn` finding it addresses.
>
> Companion to [`ANALYSIS.md`](ANALYSIS.md) (the ground-truth read of the codebase). Where the two disagree, the code wins.
>
> Plan date: 2026-07-07. Branch: `feature/cloudops-v1`.
>
> **Amended 2026-07-11 (Stage A):** folded in [`AEGISOPS_TARGET_ARCHITECTURE.md`](AEGISOPS_TARGET_ARCHITECTURE.md) (the owner's consolidated target architecture). **Authority order: where that document and this plan disagree, `AEGISOPS_TARGET_ARCHITECTURE.md` wins.** Decisions 7–13 below are owner-signed and final. The [execution checklist](#8--execution-plan-checklist-stage-a-single-progress-tracker) at the end of this file is the single progress tracker for implementation.

## Decisions locked (from the steering checkpoint)

| # | Decision | Locked answer |
|---|----------|---------------|
| 1 | Harness: fix vs replace | **Fix LangGraph** — keep it as the durable checkpoint/state-machine core; re-architect the transport, drive, and add a bounded planner. Temporal deferred to a Phase‑3 decision. |
| 2 | Credential-reveal posture (P1) | **Initiator-or-approver + org-scope + always-on audit log + mandatory step-up re-auth.** Re-auth and audit are **not optional**. |
| 3 | Multi-tenancy (P2) | **Real per-user→org scoping in Phase 1** (foundation for P1/P3). |
| 4 | Multi-step planner | **Competitive phase**, bounded + approval-gated. |
| 5 | LLM provider abstraction (P10) | **`LLMProvider` interface now**, UI trimmed to what's real, extra providers later. |
| 6 | Neo4j | **Keep for now**, flag as a Phase‑3 invest-or-fold decision. *(Superseded by decision 10: resolved = INVEST.)* |
| 7 | Architecture philosophy | **Split-Trust**: deterministic Governed Core for all mutation (zero LLM trust) + Intelligent Shell (LLM planning/investigation, full autonomy read-only). Boundary = one mutating tool `execute_governed_step(cloud, resource, action, params)` whose interior is the existing pipeline: approved module → validate → plan → plan_guard → durable approval → apply (isolated remote-locked state) → verify → record. |
| 8 | U6 upgraded | The bounded planner becomes the **Governed Executive Loop**: LLM loop at the *planning* level on LangGraph/`create_agent` primitives. It drafts a **goal DAG** (each node = approved module + params, or read-only verification), gets **ONE approval for the whole DAG**, deterministic code executes steps via `execute_governed_step`, structured observations feed back, replans **deviating from the approved DAG trigger a fresh approval interrupt**. Hard bounds: max steps, max replans per step, budget ceiling. |
| 9 | Memory = Context Engine | M1–M4 implemented as five layers: **retrieval** (pgvector message embeddings + deterministic `get_turn` positional recall), **compression** (rolling LLM summary + context offloading — plans/logs stored as references, fetched on demand), **persistent memory** (per-user/org standing context under S0), **routing** (`build_context(session, budget, purpose)` into EVERY LLM call — router, cloudops, devops, sre, loop), **verification** (store-grounded answers; reconciliation verifies memory against actual cloud state). |
| 10 | D3 resolved = **INVEST** | Neo4j becomes the **live World Model + Reconciliation Engine**: cloud inventory, TF state refs, dependency edges, run/session provenance; continuous drift detection (all clouds, extend beyond AWS EC2), orphan sweep (closes P14 spend leak), impact analysis gating destroys ("what depends on this?"). Honest exit gate: if graph queries stay 1–2 hops after a quarter of real use, fold to Postgres and drop Neo4j. |
| 11 | Module Promotion Pipeline (new, Phase 3) | When no approved module exists, the agent may DRAFT one: generate → `terraform fmt`/`validate` → Checkov/tfsec scan → open as a PR-style **proposal for platform-engineer review**. Only after human promotion does it join the approved library. **Generation and execution NEVER happen in the same turn.** |
| 12 | Rejected on record | Ephemeral agent-per-tool swarms; trust-the-LLM mutation loops; runtime LLM-generated HCL execution; SDK/imperative mutation tiers ("emergency" paths); replacing LangGraph with Temporal now. Do not implement these even if a later prompt ambiguously suggests them — flag the conflict instead. |
| 13 | Decision gates | **Temporal**: trigger = long-running (hours+) / high-fan-out DAG workflows; revisit at Phase-3 exit with real data. **deepagents package**: read-only investigation agents only; re-evaluate at 1.0/LTS. Sub-agent spawning allowed for read-only work only; mutation is never delegated to a spawned agent. |

## The harness verdict (one line)

**Keep LangGraph; fix the three layers around it that are actually broken** — replace the in-process SSE bus with a Redis Streams event log, replace the fire-and-forget drive with a supervised runner + a stranded-run reconciler, and add an approval-gated multi-step planner sub-graph. Full reasoning + candidate comparison in [`docs/fix/01_harness.md`](docs/fix/01_harness.md).

## Document set

| File | Section | Contents |
|------|---------|----------|
| *(this file)* | §1 | Overview, locked decisions, **consolidated ranked problem inventory** |
| [`docs/fix/01_harness.md`](docs/fix/01_harness.md) | §2 | The harness — current assessment, proposed design, candidate comparison, end-to-end, competitive edge *(the heart)* |
| [`docs/fix/02_fixes_safety_reliability.md`](docs/fix/02_fixes_safety_reliability.md) | §3a | Safety & state isolation · Reliability · Connectivity & usability |
| [`docs/fix/03_fixes_memory_security_obs.md`](docs/fix/03_fixes_memory_security_obs.md) | §3b | Memory & continuity · Security & RBAC · Observability |
| [`docs/fix/04_fixes_ux_data.md`](docs/fix/04_fixes_ux_data.md) | §3c | UX / rendering / seamless feel · Data layer |
| [`docs/fix/05_target_architecture.md`](docs/fix/05_target_architecture.md) | §4 | The to-be architecture + a representative "create an EC2" walkthrough |
| [`docs/fix/06_seamless_parity.md`](docs/fix/06_seamless_parity.md) | §5 | Seamless-workflow parity plan + verdict |
| [`docs/fix/07_roadmap.md`](docs/fix/07_roadmap.md) | §6 | Phased execution roadmap with acceptance tests + decision gates |
| [`docs/fix/08_risks_open_questions.md`](docs/fix/08_risks_open_questions.md) | §7 | Risks, trade-offs, open questions |

Sizing legend: **S** = <1 day, 1 file · **M** = 2–4 days, several files · **L** = ≥1 week, structural. Blast radius: **low / medium / high** = how many subsystems the change disturbs.

---

## §1 — Consolidated problem inventory (ranked)

One ranked list of every serious problem from `ANALYSIS.md` (P-numbers are stable across both documents). "Current → Required" states the behavior gap the fix must close.

### Tier 0 — Must-fix to be trustworthy (safety / security / correctness)

| ID | Severity | Problem | Root cause | Files | Current → Required |
|----|----------|---------|-----------|-------|--------------------|
| **P2** | Critical | Multi-tenancy is fictional | Every endpoint uses `repo.get_default_org()`; `user.org` ignored; `Session.user_id` never set | `db/repositories.py:get_default_org`, `api/chat.py:117`, `api/sessions.py`, `api/modules.py`, `api/knowledge.py` | One shared workspace for all users → each request scoped to the authenticated user's org; sessions owned by their user |
| **P3** | Critical | No authz on session/run read + stream | Endpoints check auth only, not ownership | `api/sessions.py:59`, `api/chat.py:228`, `api/artifacts.py:_load`, `api/chat.py:218` | Any user reads any run/session by UUID → owner/org predicate; 404 on mismatch |
| **P1** | Critical | Credential-reveal not RBAC'd / no re-auth / no audit | `Depends(get_current_user)` only; no ownership, re-auth, or audit | `api/artifacts.py:209 reveal_credential` | Any authed user exfiltrates any run's private key → initiator-or-approver + org-scope + **mandatory step-up re-auth** + **always-on audit log** |
| **P5** | High | Idempotency guard double-applies on in-flight claim | Falls through to `apply()` when claim in-progress but no stored result | `agents/cloudops.py:935`, `security/idempotency.py:35` | Concurrent `/approvals` → double apply → wait-or-abort; never fall through |
| **P6** | High | Blocking `boto3` on the event loop | Sync client + `describe_instances` in an async fn | `agents/inventory.py:229 reconcile` | Every EC2 read stalls the worker → thread-offload all SDK calls |
| **P14** | Medium | Cross-store writes not atomic; no reconciliation | Apply→inventory→graph→trace are separate best-effort writes | `agents/cloudops.py:cloudops_execute`, `inventory.record_from_apply` | Crash between apply and inventory → orphaned cloud spend → same-txn inventory write + periodic reconciler |
| **P12** | Medium | Local TF backend, no state locking | `backend "local" {}`; legacy resources share `aegisops.tfplan` | all `infra/terraform-workspaces/*/main.tf`, `tools/terraform.py:61` | Concurrent ops corrupt state → remote backend + lock; unique plan-file per resource |
| **P15** | Medium | No 4-eyes; initiator not recorded | Run has no initiator; approver≠initiator not enforced | `api/chat.py:chat`, `agents/approval.py` | Self-approval possible → record `run.initiated_by`; optional approver≠initiator gate for prod |

### Tier 0 — Reliability & continuity (trust)

| ID | Severity | Problem | Root cause | Files | Current → Required |
|----|----------|---------|-----------|-------|--------------------|
| **P4** | High | Streaming single-process; channels leak | Module-global `_channels` dict; `drop_channel` only called in a test | `agents/events.py:41`, `api/chat.py:_drive` | Memory leak + multi-worker broken → Redis Streams event bus; evict on terminal |
| **—** | High | Crash mid-run strands the run | `_drive` is fire-and-forget `asyncio.create_task`; only the approval pause is resumed, not a crash mid-apply | `api/chat.py:171`, `agents/runner.py` | A run in `running`/`awaiting_approval` after a crash never re-drives → supervised runner + stranded-run reconciler |
| **Memory** | High | Conversational recall is lossy | Char-budgeted transcript; older turns → 160-char digest; router sees only 8 turns | `agents/memory.py:build_transcript`, `classification_context` | "100th message recalls the 20th" only partially → deterministic positional + semantic per-session retrieval; token budget + rolling summary |

### Tier 1 — Needed to be competitive (governance honesty, UX, autonomy, observability depth)

| ID | Severity | Problem | Root cause | Files | Current → Required |
|----|----------|---------|-----------|-------|--------------------|
| **P7** | High | SRE remediation is a no-op reporting success | Only lists deployments; hardcoded `recent_deploy:True` | `agents/sre.py:146`, `agents/sre.py:53` | Approves a remediation that does nothing → real K8s actions + real Prometheus signals, or explicit "proposed, not executed" |
| **P8** | High | Policy checks are hardcoded `True` | `_ck(name, True)` regardless of inputs/plan | `agents/templates.py:_s3_policy…_gcp_cloudsql_policy` | Approver trusts theater → evaluate against `validated`/plan JSON; OPA/Conftest option |
| **P9** | Medium | Traces tab shows static placeholders | Returns fixed span names, ignores Langfuse | `api/artifacts.py:184 traces` | Real trace tree not shown → query Langfuse or deep-link |
| **P10** | Medium | Model selection is fake | `body.model` never read; global Gemini singleton | `api/chat.py`, `integrations/gemini.py:get_gemini`, `frontend/lib/data.ts:42` | UI lies about model choice → `LLMProvider` interface; honor `body.model`; trim UI |
| **P11** | Medium | "Ask which cloud" unreachable from UI | Selector defaults `AWS` | `agents/cloudops.py:resolve_cloud`, `api/chat.py:40`, `frontend/lib/store.ts:137` | Never asks in real UI → "Auto/ask me" default; selector as hint only |
| **P16** | Low/Med | DevOps CI verify doesn't wait | Reads `latest_run_status` immediately after dispatch | `agents/devops.py:170` | May read the previous run → capture run id + poll to completion |
| **P13** | Medium | Dead code: `runinput:`, `drop_channel`, `route_decision`, `prior_user_questions` | Wired but unconsumed | `api/chat.py:248`, `agents/events.py:54`, `agents/router.py:195`, `agents/memory.py:53` | Interactive input is a no-op → wire mid-run input to `console.send_input`; remove/wire the rest |

### Tier 1 — Connectivity & usability (already mostly-fixed; verify + harden)

| ID | Severity | Problem | Root cause | Files | Current → Required |
|----|----------|---------|-----------|-------|--------------------|
| N-02 | (verify) | VM reachability / credential delivery | `allowed_cidr` + one-time reveal exist | `infra/terraform-workspaces/*-vm|gce/main.tf`, `agents/cards.py`, `api/artifacts.py` | Real, but reveal is under-secured (P1) → keep + secure |

### Tier 2 — Hardening / differentiators

| ID | Severity | Problem | Files | Current → Required |
|----|----------|---------|-------|--------------------|
| **P17** | Low | Notify emails sender, not stakeholders | `agents/notify.py:46` | Self-email → address initiator/approver/team |
| **P18** | Low | Gemini sync network call in constructor | `integrations/gemini.py:_resolve` | Blocks first call → lazy/async resolve |
| **P19** | Low | Rate-limit on SSE; unused metrics | `main.py`, `metrics.py` | Empty dashboard series → exempt SSE; wire/remove metrics |
| **P20** | Low | `answer`/`outcome` not redaction-scanned pre-persist | `api/chat.py:_persist_result` | Future secret echo persists → `redact()` backstop |
| Data | Med | Missing hot-path indexes | `db/models.py` | Seq scans → add `messages(session_id,created_at)`, `messages(run_id)`, `runs(session_id)`, `runs(org_id,created_at)` |
| Neo4j | — | Best-effort mirror earns little | `graph_db/*` | Invest or fold — Phase‑3 decision |

**Coverage note.** Every P-item and every gap in `ANALYSIS.md` §09/§10 is carried into a domain fix ([02](docs/fix/02_fixes_safety_reliability.md)–[04](docs/fix/04_fixes_ux_data.md)) and placed in a phase ([07](docs/fix/07_roadmap.md)). Nothing from the analysis is dropped.

---

## §8 — Execution plan checklist (Stage A, single progress tracker)

> Appended at Stage A (2026-07-11). **This checklist is the single progress tracker** for Stage B; status is updated per item as implemented and mirrored into `aegisops_production_kit/PROGRESS.md`. Full item specs + acceptance tests: [07 roadmap](docs/fix/07_roadmap.md) and the domain fix docs. Work strictly in phase order; a phase's exit gate must pass before the next phase starts.
>
> **UI surface legend (§5 of the production directive):** ① tenancy visible · ② roles honest · ③ reveal flow · ④ cloud selector · ⑤ model menu · ⑥ approval card · ⑦ traces tab · ⑧ SRE honesty · ⑨ memory in the UI · ⑩ executive-loop UI · ⑪ drift & orphans · ⑫ streaming. Status: `pending` → `in progress` → `done (evidence)`.

### Phase 1 — Trustworthy

| ID | Item | Files touched | Acceptance test | UI | Status |
|----|------|--------------|-----------------|----|--------|
| S0 | Real multi-tenancy: principal→(org_id,user_id); kill `get_default_org()` everywhere; `Session.user_id`; org predicates; seed **two orgs with users** | `security/tenancy.py` (new), `security/deps.py`, `schemas/auth.py`, `db/repositories.py` (`org_for`, `get_default_org` deleted), `api/{auth,chat,sessions,modules,knowledge}.py`, `seed/seed.py`, `infra/keycloak/realm-export.json` (org groups + membership mapper + org-B users), `rag/embeddings.py` (keyword degrade) | `test_tenancy.py`: two-org isolation; cross-org UUID → 404; visible in UI | ① | **done** — `tests/test_tenancy.py` 8/8 (resolver matrix: claim wins/mirror update/seeded fallback; sessions+chat+approvals+overview+knowledge isolation, cross-org 404; strict 403 for unscoped principal); full suite 402 passed/2 skipped; vitest 25 passed. Orgs seeded: northwind-financial + acme-industrial. Flag `AEGISOPS_TENANCY=strict\|legacy` (default strict). Keycloak org claim via group-membership mapper; existing dev realms need a keycloak container recreate to pick it up (username/email fallback covers until then) |
| S1 | Credential reveal: initiator-or-approver + org-scope + step-up re-auth (≤120s proof) + always-on audit; frontend re-auth modal | `api/artifacts.py:reveal_credential`, `security/deps.py:verify_stepup_auth`, `settings.py` (`REVEAL_STEPUP_MAX_AGE_SECONDS=120`), `frontend/…CredentialReveal` (password modal) | non-owner → 404; no fresh proof → 401; value once, 2nd → 410; every attempt audited | ②③ | **done** — `test_tenancy.py::TestCredentialRevealS1` (cross-org→404, non-initiator/approver→404, initiator w/o proof→401, bad password→401, approver+fresh proof→value once, 2nd→410; audit-row count == attempts, value never logged); reveal modal collects password re-entry, surfaces 401 in place; full suite 407 passed/2 skipped; vitest 25; tsc clean |
| S2 | `authorize_run`/`authorize_session` on every read/stream; 404 on mismatch | `security/deps.py` (shared predicates), `api/artifacts.py:_load` (all 8 tabs + credentials + metrics), `api/chat.py:get_run`/`chat_stream`, `api/sessions.py:session_messages` | cross-org read of every tab + stream → 404 | ① | **done** — `test_tenancy.py::test_cross_org_read_of_every_tab_is_404` (9 run reads + stream + messages + credentials cross-org → 404; same-org → 200; invalid UUID → 404 not 500); full suite 403 passed/2 skipped; vitest 25 passed |
| S3 | `require_initiator` on `POST /chat` | `api/chat.py`, `frontend/components/Workspace.tsx` (read-only composer notice) | read-only POST /chat → 403; composer honest for read-only | ② | **done** — `test_rbac_endpoints.py::test_chat_requires_initiator` (read-only/auditor → 403 at the gate; all 5 initiator roles pass it); composer replaced with a clear read-only notice for `can_initiate=false`; full suite 404 passed/2 skipped; vitest 25 passed; tsc clean |
| S4 | Redaction backstop on persisted `answer`/`outcome` | `api/chat.py:_persist_result` | planted secret masked in persisted content | — | **done** — `test_redaction.py::TestPersistBackstop` (planted private key + password masked in `messages.content` and `runs.outcome`; non-secret fields preserved); full suite 405 passed/2 skipped |
| S5 | Capability assertion at the execute node | `agents/execute.py` (fail-closed), `agents/approval.py` + `api/chat.py` (carry approver `can_execute` through the resume) | execute refuses when the approver lacks execute capability | — | **done** — `test_safety_invariants.py::TestExecuteCapabilityGuard` (3). Deviation from fix-doc "initiator": asserts the **approver's** capability — an initiator (developer) legitimately lacks execute, so asserting the initiator would wrongly block every developer-initiated run; the approver authorizes mutation. Full suite 424 passed/2 skipped |
| A1+B7 | Idempotency wait-or-abort + `/approvals` endpoint guard + lifecycle primitive | `security/idempotency.py` (`is_in_progress`, `wait_for_result`), `agents/cloudops.py:cloudops_execute` (wait-or-abort, never fall through), `api/chat.py:resolve_approval` (NX in-flight lock) | concurrent double-approve → exactly one apply; in-progress never falls through | — | **done** — `test_idempotency.py` (+4: in-progress helper, wait returns stored/None/abort-at-deadline, **node aborts on in-flight claim with apply() asserted unreachable**), `test_tenancy.py::test_double_approval_endpoint_guard` (2nd /approvals → 409); full suite 412 passed/2 skipped |
| A2 | plan_guard re-asserted at the approval node | `agents/approval.py` (choke-point re-assertion before `interrupt()`) | approval refuses mismatched plan even if plan node skipped the guard | ⑥ | **done** — `test_safety_invariants.py::TestApprovalChokePointGuard` (apply-with-replace blocked before interrupt; destroy-that-creates blocked; explicit read-action honored; blocked routes to finalize not execute); full suite 416 passed/2 skipped |
| A4 | Org-scoped duplicate-name check | `agents/cloudops.py:314` (no logic change — `list_active(state["org_id"])` now carries the real S0 org) | dup check carries the real org | — | **done** — `test_inventory.py::test_duplicate_name_check_is_org_scoped` (a name active in org A is invisible to org B; the same-name-create dup predicate fires only within the owning org; no cross-org leak). Verified, not rebuilt: S0 flows the real org into `state["org_id"]` |
| A5 | `runs.initiated_by` + policy-configurable 4-eyes (Production) | migration `0004_run_initiated_by` (+`runs.env`), `db/models.py`, `api/chat.py`, `settings.py` (`AEGISOPS_FOUR_EYES_FOR_PRODUCTION`, default on) | prod self-approve → 403 | ② | **done** — `test_tenancy.py::test_four_eyes_blocks_prod_self_approval` (prod self-approve → 403 four-eyes; different approver passes the gate; non-prod exempt); full suite 406 passed/2 skipped |
| B5 | Terminal-state guarantee (finally + fault-injection test) | `api/chat.py` (`_force_terminal` backstop + `except` in both `_drive` closures) | fault in `_persist_result` → run `failed`, never stuck | — | **done** — `test_tenancy.py::TestTerminalStateB5` (force-terminal marks a running run failed; leaves a completed run untouched; fault-injection through the real /chat endpoint → run ends `failed`, not `running`) |
| B6 | Zero blocking I/O: `inventory.reconcile`, sync-SDK grep-audit, Gemini sync-resolve (P18) | `agents/inventory.py:reconcile` (boto3 describe → `anyio.to_thread`), `integrations/gemini.py` (lazy off-thread `_ensure_model`, no network in constructor) | responsiveness test; clean grep audit | — | **done** — `test_inventory.py::test_reconcile_offloads_blocking_sdk_call` (concurrent ticker runs ≥10× during a 0.4s blocking describe → loop never stalled); grep-audit: all remaining agent SDK calls go through the offloaded `tools/aws.py`; Gemini model resolve moved out of `__init__` to a lazy `anyio.to_thread` call (P18); full suite 418 passed/2 skipped |
| U4 | "Auto (ask me)" cloud default; selector-as-hint | `frontend/lib/data.ts` (Auto option + `cloudToWire`), `frontend/lib/store.ts` (default + wire map), `api/chat.py` (`ChatContext.cloud=None`) | ambiguous VM request → clarifying question appears in UI | ④ | **done** — `cloud_selector.test.ts` (Auto is first option, maps to null; real clouds sent verbatim) + existing `test_routing_scenarios.py::test_ambiguous_cloud_asks_never_defaults_to_aws` (null selector + generic VM → resolve_cloud None → clarifying question). `resolve_cloud` already handled null; only the AWS default needed removing. Full suite 424 passed/2 skipped; vitest 28 |
| HON | Honesty labels: policy "not evaluated"; SRE "proposed, not executed"; Traces "coming — open in Langfuse" deep-link | `agents/templates.py` (`_todo`/`evaluated` flag), `agents/sre.py` (proposed-not-executed), `api/artifacts.py` (traces deep-link + timeline count), `frontend/components/ArtifactPanel.tsx` (not-evaluated render + Langfuse link) | no surface claims what it didn't verify | ⑥⑦⑧ | **done** — P8: `test_templates.py::test_policy_checks_never_fake_a_pass` + updated `test_s3_policy_checks` (literal-True checks → `passed=None` "not evaluated", real predicates keep bool; a false predicate shows a real fail). P7: `test_honesty_labels.py::TestSreHonesty` (never `applied:True`; outcome `proposed_not_executed`; user told "proposed, not executed"). P9: `TestTracesHonesty` (no fabricated spans; `coming_soon` + Langfuse deep-link, trace_id==run_id). Full suite 427 passed/2 skipped; vitest 28 |
| O2 | Langfuse project-key startup assertion | `settings.py` (`LANGFUSE_EXPECTED_PROJECT`), `integrations/langfuse_client.py:assert_project`, `main.py` lifespan | startup asserts key/project | — | **done** — `test_langfuse_tracing.py` (+3: keys match→ok; wrong project→"wrong_project" loud warning; no keys→"not_configured"). Queries `/api/public/projects` with the keys, warns loudly if the expected project isn't among them (catches the exact "0 traces / wrong project" regression). Best-effort; never blocks startup |
| C1 | Per-cloud plan-assertion tests: ingress/CIDR | `tests/test_module_ingress.py` | SG/NSG/firewall rules asserted per cloud | — | **done** — `test_module_ingress.py` (6): admin port bound to `allowed_cidr` only (never 0.0.0.0/0) on AWS SG / Azure NSG; GCP firewalls attach via network tags (the exact regressed defect); app ports from `ingress_ports`. Static source assertion → runs anywhere; the live `terraform show -json` superset is creds-gated |
| D1 | Hot-path indexes | migration `0005_hot_path_indexes` | `EXPLAIN` shows index scans | — | **done** — `test_indexes.py` (2): the 4 indexes exist (`messages(session_id,created_at)`, `messages(run_id)`, `runs(session_id)`, `runs(org_id,created_at)`); the transcript query plans onto `ix_messages_session_created` |
| D4 | Repo/state hygiene: purge tracked `*.tfplan`, gitignore, dev TF state off OneDrive | `.gitignore` (+`*.tfplan`, `terraform.tfstate.d/`), purged 11 tracked `*.tfplan` from the index | `git status` clean; warm plan faster | — | **done** — 11 `aegisops.tfplan` files removed from git tracking (they embed variable values); `*.tfplan` + `terraform.tfstate.d/` gitignored; 0 tracked plan/state files remain. Compose already mounts the `tfstate` named volume off the bind-mount; the durable dev-state-off-OneDrive fix is A3 (remote backend, Phase 2) — noted, not silently skipped |

**Phase 1 exit gate:** two orgs isolated in API+UI · read-only can't initiate · no prod self-approve · exactly one apply under concurrent approve · reveal gated+audited · no dishonest surface · regression suite green. — **status: pending**

### Phase 2 — Production harness + Context Engine

| ID | Item | Files touched | Acceptance test | UI | Status |
|----|------|--------------|-----------------|----|--------|
| E2E | **(owner-ordered, first)** Playwright e2e for §5 flows 1–3 + re-run existing browser suite | `frontend/e2e/tenancy_roles_reveal.spec.ts` (new) | flows 1 (tenancy visible), 2 (read-only composer), 3 (reveal step-up modal) | ①②③ | **done** — `tenancy_roles_reveal.spec.ts` (6): org-A sees Northwind; org-B sees Acme only, never Northwind; read-only shows the notice + no composer; initiator sees composer; reveal → step-up modal → wrong password stays with re-auth message → correct shows the value once. **Full browser suite green: 22 passed / 2 skipped (by-design mobile) / exit 0**; 1 live-LLM streamed-run test is flaky-but-passes here (invalid Gemini key → slow backend retries; clean with a real key). Frontend served by host `next dev` (live code; the compose image was stale) |
| B1 | Redis Streams event bus; `Emitter` unchanged; `AEGISOPS_EVENT_BUS=memory\|redis` | `agents/events.py` (`RedisChannel` + XREAD pump, memory path byte-identical), `settings.py` (flag), `api/chat.py:chat_stream` (redis existence check + cursor) | SSE contract on Redis; multi-worker publish/consume; TTL on terminal | ⑫ | **done** — `test_event_bus_redis.py` (5): frame shapes identical; exactly-once + EOS stops (marker never leaks); replay-after-id; **multi-worker publish-A/consume-B**; terminal stream sets TTL. Memory path unchanged — `test_sse_contract.py` (7) still green. Default `memory` (rollback); `redis` opt-in via flag. Full suite green |
| B2 | RunSupervisor: tracked tasks + heartbeat + graceful drain | new `agents/supervisor.py`, `api/chat.py` (both drive sites → `supervisor.run`), `main.py` (drain on shutdown) | heartbeat expiry observable; honest shutdown persistence | — | **done** — `test_supervisor.py` (2): a supervised run is `is_live` with a `run:<id>:hb` key, deregisters (key cleared) on normal completion; **drain cancels an in-flight run and persists it `failed`** (never dropped). Replaces the fire-and-forget `create_task`; heartbeat TTL 45s / refresh 15s feeds B3's stale-run detection |
| B3 | Stranded-run reconciler | new `agents/reconciler.py`, `main.py` (gated start/stop in lifespan), `settings.py` (`AEGISOPS_RECONCILER=on\|off`), `docker-compose.override.yml` (off under pytest) | kill-mid-apply → terminal exactly once | — | **done** — `test_reconciler.py` (5): stranded non-resumable → failed honestly; resumable → re-driven via supervisor (not failed); live/fresh-heartbeat skipped; awaiting_approval left; **kill-mid-apply → terminal once, no re-apply**. **Defect fixed**: reconciler no longer auto-starts a background loop in TestClient lifespans (gated off under pytest) + `start()` idempotent. Full suite green **twice in a row** (450/2/exit 0 each; direct exit code, no pipe-masking) |
| B4 | Verify cross-cloud (Azure/GCP), timeout-bounded, offloaded + honest cards (C2) | `agents/finalize.py`, `agents/cards.py` | per-cloud mock-slow → warn card, never hangs | — | pending |
| A3 | Remote TF backend (S3+DynamoDB, `-backend-config`); unique plan-file per run; `AEGISOPS_TF_BACKEND` | `tools/terraform.py`, module init | safety-live green on remote; no plan-file collision | — | pending |
| LAT | Latency pass: init-skip + `TF_PLUGIN_CACHE_DIR` | `tools/terraform.py`, compose | warm turn → approval card ≤15s; measured in PROGRESS.md | ⑥ | pending |
| M1 | `build_context(session, budget, purpose)` into EVERY LLM call; purpose slices | `agents/memory.py`, all agents | 40-turn context within budget; CloudOps sees prior turns | ⑨ | pending |
| M2 | Embeddings on write + semantic retrieval + deterministic `get_turn`; embedding flag | message-write path, migration, `agents/memory.py` | **turn 20 of 100 verbatim, in the UI** | ⑨ | pending |
| M3 | Router purpose-slice replaces last-8-turns | `agents/router.py`, `agents/memory.py` | 30-turn-old reference resolves | ⑨ | pending |
| M5 | Context offloading: plans/logs/dumps as references, fetched on demand | `agents/memory.py`, prompt assembly | long session in budget; prior plan answered from artifact | — | pending |
| U1 | Real policy predicates over `validated` + plan JSON | `agents/templates.py` | encryption-off plan → **failed** check on the card | ⑥ | pending |
| DEF | Defaults honesty: defaulted VPC/subnet/network/RG stated on the approval card | `agents/cloudops.py`, card payload, frontend | "placing in default VPC vpc-0abc" visible | ⑥ | pending |
| U2 | SRE real: Prometheus deploy-annotation signal; real K8s actions or "proposed, not executed" | `agents/sre.py`, `tools/kubernetes.py` | fake-K8s patch real; without K8s honest label; never fake success | ⑧ | pending |
| U3 | LLMProvider + GeminiProvider + `get_provider(body.model)`; menu trimmed | new `integrations/llm/`, `agents/llm.py`, `frontend/lib/data.ts` | model selection real; unknown model → clear error | ⑤ | pending |
| O1 | Real Traces tab: run_steps-derived tree + Langfuse deep-link | `api/artifacts.py:traces` | real durations, no `—` | ⑦ | pending |
| O3 | Metrics hygiene; SSE exempt from rate limiter | `main.py`, `metrics.py`, `agents/timing.py` | non-empty series or removed; SSE unthrottled | — | pending |
| D2 | Same-txn inventory write + orphan sweeper | `agents/cloudops.py`, reconciler | crash-inject → no invisible orphan | — | pending |
| U5 | Mid-run input: wire via supervisor or remove endpoint+key (document choice) | `api/chat.py`, supervisor | wired e2e or zero references | — | pending |
| U8 | SSE contract regression green on Redis bus (reducer unchanged) | frontend tests | vitest + Playwright streaming green | ⑫ | pending |
| P16 | DevOps CI poll-to-completion *(here or Phase 3)* | `agents/devops.py` | polls the dispatched run id to completion | — | pending |

**Phase 2 exit gate:** worker-kill mid-apply recovers exactly once · multi-worker streaming + reconnect · turn-20 recall in UI · real failed policy check · real Traces tab · honest model menu. — **status: pending**

### Phase 3 — Intelligence layer

| ID | Item | Files touched | Acceptance test | UI | Status |
|----|------|--------------|-----------------|----|--------|
| D3 | World Model + Reconciliation Engine: schema, ingestion, drift job, orphans, `impact_of` in destroy path | `graph_db/*`, `agents/inventory.py`, reconciler, frontend bell/panel | manual SG drift → notification; depended-on destroy warns; orphan found | ⑥⑪ | pending |
| DEP | Dependency closure resolution (strict order: named → world model → stated default → create-first DAG) | `agents/cloudops.py`, loop, world model | tests (a) VPC→EC2 DAG; (b) RG→storage DAG; (c) EKS offered existing VPCs; (d) two VPCs → asks | ⑥⑩ | pending |
| U6 | Governed Executive Loop: `execute_governed_step`; goal-DAG approval card; per-step timeline; deviation re-approval; bounds; `AEGISOPS_EXEC_LOOP` | new loop graph, `agents/graph.py`, frontend DAG card | VPC+EC2 one approval both applied in order; EFS replan → re-approval; bounds tested | ⑩ | pending |
| INV | Read-only investigation agents (SRE triage, discovery); sub-agents allowed here only; deepagents here only | `agents/sre.py`, discovery module | agents hold read-only tools (asserted); no mutation delegation | ⑧ | pending |
| MPP | Module Promotion Pipeline: draft → fmt/validate → Checkov/tfsec → proposal + review UI → promote | new pipeline, `agents/templates.py`, frontend | drafted module unselectable until promoted; never same-turn execution | ⑥ | pending |
| M4 | Per-user/org persistent memory, user-editable, in `build_context` | new `user_memory`, `agents/memory.py`, frontend | "my usual region" honored in a new session | ⑨ | pending |
| U7 | Retry-with-fix + undo last apply (gated destroy) | `agents/cloudops.py`, `provider_errors.py` | bad-region retry one-click; "undo that" gated destroy | ⑥ | pending |
| MOD | Modify beyond ports: S3 lifecycle/versioning, RDS scaling, tags | `_modify_resource`, schemas, modules | in-place guard + gate per new type | ⑥ | pending |
| COST | Cost estimation → real policy check + approval card (verify tooling at impl) | new cost module, `agents/templates.py`, card | real $/mo on card; guardrail breach → failed check | ⑥ | pending |
| P17 | Notify real recipients | `agents/notify.py` | initiator/approver addressed, not sender | — | pending |

**Phase 3 exit gate:** VPC→EC2 DAG demo e2e in UI · drift notification from manual console change · world-model destroy warning · module proposal→review→promotion flow. — **status: pending**
