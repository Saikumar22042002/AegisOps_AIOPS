# 09 — Serious problems observed (prioritized)

[← back to index](../../ANALYSIS.md)

Ranked by severity × likelihood. Each has: severity, evidence (`file:line`), impact, and a concrete fix. "Sev" = Critical / High / Medium / Low.

---

### P1 — Credential-reveal endpoint is not RBAC'd or ownership-checked · **Critical (security)**
**Evidence:** `api/artifacts.py:209` — `reveal_credential(... user = Depends(get_current_user) ...)`; no `require_approver`, no check that `run.org_id == user.org` or that the user initiated the run. It reads the run's **sensitive** Terraform output (private key / admin password) via `terraform output -raw`.
**Failure scenario:** an authenticated read-only auditor (or any user in a future second org) enumerates/guesses a `run_id`, calls `POST /runs/{id}/credentials {output:"private_key_pem"}`, and receives the VM's private key on the first (only) reveal. The one-shot NX claim doesn't help — the *first* caller wins, legitimate or not.
**Fix:** require an approver/owner role; verify the run belongs to the caller's org and (ideally) that the caller initiated or approved it; log a redacted audit event on every reveal. Consider requiring re-auth for reveals.

### P2 — Multi-tenancy is not implemented (single default org + no per-user scoping) · **Critical (correctness/security)**
**Evidence:** every endpoint uses `repo.get_default_org(s)` (oldest org) and ignores `user.org` (`db/repositories.py:14`, used in `api/chat.py`, `sessions.py`, `modules.py`, `knowledge.py`). `Session.user_id` is never set on chat (`api/chat.py:117`). `CLAUDE.md`: "Multi-tenant: org-scope every query."
**Failure scenario:** add a second organization and every user of either org shares one workspace — all sessions, runs, inventory, and knowledge are visible to everyone. The product is single-tenant in practice.
**Fix:** derive org from the authenticated user (map `keycloak_sub`/claim → `users.org_id`), scope every query by it, populate `Session.user_id`, and add an org+owner filter to session/run reads.

### P3 — No authorization on session/run read + stream endpoints · **Critical (security)**
**Evidence:** `api/sessions.py:59` (`session_messages`), `api/chat.py:228` (`get_run`), all `api/artifacts.py` GET tabs, `api/chat.py:218` (`chat_stream`) — authenticated but not owner/org-scoped.
**Failure scenario:** any authenticated user reads any other user's conversation, plan, logs, approvals, and live stream by UUID. Latent today (one org), a breach the moment tenancy is added.
**Fix:** add an ownership/org predicate to `_load` and the session/run getters; return 404 (not 403) on mismatch to avoid enumeration.

### P4 — Streaming is single-process; channels leak and can't scale · **High (reliability/scalability)**
**Evidence:** `agents/events.py:41` `_channels: dict` module global; `create_channel` inserts, but `drop_channel` is **only called in a test** (`grep`: `drop_channel` appears in `events.py` + `test_sse_contract.py` only). `README`/`CLAUDE.md`: "stateless API … horizontally scalable."
**Failure scenario:** (a) memory grows unbounded — every run leaves a `RunChannel` (queue + 1000-item deque) alive forever; (b) with >1 uvicorn worker or >1 replica, `POST /chat` and its reconnect/`POST /approvals` may hit different processes → `get_channel` returns None → 404 "no active stream", broken resume.
**Fix:** evict channels on `done`/close (call `drop_channel` in `_drive`'s finally with a grace window for reconnect); for scale-out, move the event bus to Redis pub/sub or streams keyed by run_id so any worker can serve the stream and the resume.

### P5 — Idempotency guard double-applies on an in-flight claim · **High (correctness/safety)**
**Evidence:** `agents/cloudops.py:935` — `if not await idempotency.claim(key): done = await idempotency.get_result(key); if done: return …`. `get_result` returns None while state is `in_progress` (`security/idempotency.py:35`), so control **falls through to `runner.apply()`**.
**Failure scenario:** two concurrent `POST /approvals/{id}` for the same run (double-click, retry) → first claims and is applying; second fails the claim, gets `None` (not yet done), and **applies again** → duplicate/racing Terraform apply on the same state.
**Fix:** on a failed claim with no stored result, **wait-and-poll** for the result or **abort** with "already in progress" — never fall through to execute. Also guard at the endpoint (reject a second `/approvals` while a prior one is running).

### P6 — Blocking `boto3` call on the event loop · **High (reliability/latency)**
**Evidence:** `agents/inventory.py:229` — `reconcile` constructs a `boto3.client("ec2")` and calls `describe_instances` **synchronously** inside an async function (no `anyio.to_thread`). Contrast `tools/aws.py`, which correctly offloads via `_run`.
**Failure scenario:** every specific EC2 read blocks the entire event loop for the duration of the AWS call (hundreds of ms to seconds on a cold/regional call), stalling all concurrent requests/streams in that worker.
**Fix:** route reconciliation through `aws_tool.get_aws().list_instances`/a thread-offloaded describe; never call blocking SDK clients directly in coroutines.

### P7 — SRE remediation is a no-op that reports success · **High (correctness / "no mocks" violation)**
**Evidence:** `agents/sre.py:146` — `sre_execute` for `rollback`/`scale_out`/`restart` only calls `k8s.list_deployments("default")` then returns `{"status":"remediated","applied":True}`. It never rolls back, scales, or restarts anything. Also `agents/sre.py:53` — `signals = {"recent_deploy": True}` is **hardcoded**, so `decision_matrix` with any error_rate>0.05 always proposes `rollback`.
**Impact:** the SRE approval gate approves a "remediation" that does nothing; the timeline/outcome claim success. Directly violates `CLAUDE.md`'s "Everything is real. No mocks/stubs."
**Fix:** implement the real K8s actions (rollback = `kubectl rollout undo`/patch, scale = patch replicas, restart = rollout restart) or mark SRE remediation explicitly "proposed, not executed" until real. Collect real signals (query Prometheus for deploy annotations) instead of a constant.

### P8 — Policy checks are hardcoded `True` (decorative governance) · **High (trust/governance)**
**Evidence:** `agents/templates.py` — `_s3_policy`, `_ec2_policy`, `_rds_policy`, `_azure_vm_policy`, etc. return `_ck(name, True)` for most checks regardless of the inputs/plan. The approver sees "6/6 passed" that were never evaluated.
**Impact:** the "policy evaluation" that gates production changes is theater. An approver trusts a green checklist that asserts, e.g., "Root volume encrypted" as a literal `True` — even though the module happens to enforce it, the *check* isn't verifying anything.
**Fix:** evaluate checks against the actual plan/inputs (or the plan JSON), or integrate OPA/Conftest against the `terraform show -json`. At minimum, derive each check from a real predicate on `validated`/`plan.diff`.

### P9 — Artifact "Traces" tab shows static placeholders, not real traces · **Medium (UX/trust)**
**Evidence:** `api/artifacts.py:184` — `traces()` returns fixed span names (`intent.classify`, `agent.route`, …) with `dur:"—"`, ignoring the real Langfuse trace that exists for the run.
**Fix:** query Langfuse's API for the run's trace tree (or deep-link to the Langfuse UI). The real data is already there.

### P10 — Model selection is fake; no provider abstraction · **Medium (product/architecture)**
**Evidence:** `frontend/lib/store.ts:139` default model "Gemini 2.5 Pro"; `lib/data.ts:42` menu lists Claude/GPT/Llama; `api/chat.py` **never reads `body.model`** (grep confirms no `body.model` usage); `integrations/gemini.get_gemini` is a global singleton keyed to `GEMINI_MODEL`.
**Impact:** the UI advertises a model choice that does nothing; there is no way to actually switch providers per request.
**Fix:** add an `LLMProvider` interface, honor `body.model`, and either implement the alternate providers or trim the menu to what's real.

### P11 — "Ask which cloud" ambiguity guard is unreachable from the real UI · **Medium (safety-UX gap)**
**Evidence:** `resolve_cloud` (`cloudops.py:98`) falls back to the UI selector; `ChatContext.cloud` defaults `"AWS"` (`api/chat.py:40`) and the store defaults `"AWS"` (`store.ts:137`). So a no-cloud VM request resolves to AWS via the selector, never asking.
**Impact:** the documented "never silently default to AWS" behavior only holds when `cloud=null` is sent (tests) — the product always sends AWS.
**Fix:** add an explicit "Auto / ask me" cloud option (null) and make it the default, or only use the selector as a hint when the message truly names no cloud and the user hasn't pinned one.

### P12 — Local Terraform backend, no state locking · **Medium (reliability)**
**Evidence:** every module `backend "local" {}`; per-resource `TF_WORKSPACE` isolates resources but legacy resources share `aegisops.tfplan` (`tools/terraform.py:61`), and there's no lock. `TF_STATE_*` remote backend is env-configurable but not defaulted/used.
**Impact:** concurrent operations on the same resource/module can corrupt state or plan files; state lives on a bind-mounted volume (durability + the documented OneDrive I/O amplification).
**Fix:** default to an S3+DynamoDB (or equivalent) backend with locking for anything beyond a single-user demo; give each resource its own plan-file path unconditionally.

### P13 — Dead code paths: `runinput:` queue, `drop_channel`, `route_decision`, `prior_user_questions` · **Medium (correctness/maintenance)**
**Evidence:** `POST /runs/{id}/input` rpushes to `runinput:<run_id>` (`api/chat.py:248`) but **no consumer reads it** (grep) — interactive console input is a no-op. `drop_channel`, `router.route_decision`, and `memory.prior_user_questions` are defined but only referenced by tests.
**Impact:** an advertised "interactive prompt/input" feature does nothing; dead code misleads readers and hides the channel-leak (P4).
**Fix:** wire `run_input` to `CommandConsole.send_input` for the running process (or remove the endpoint); call `drop_channel`; delete or wire the unused helpers.

### P14 — Cross-store writes aren't atomic; no reconciliation · **Medium (correctness)**
**Evidence:** apply result (PG) → inventory (PG, separate txn) → graph (Neo4j) → trace (Langfuse) are independent best-effort writes (`cloudops_execute`, `inventory.record_from_apply`).
**Failure scenario:** crash after `apply` but before the inventory write → a real cloud resource with no inventory row → invisible to day-2/destroy → orphaned spend.
**Fix:** write the inventory row in the same transaction as the run outcome; add a periodic reconciliation (Terraform state list vs inventory) to catch orphans.

### P15 — No 4-eyes / initiator ≠ approver · **Medium (governance)**
**Evidence:** `api/chat.py` doesn't record the initiator on the run; `resolve_approval` only checks `require_approver`, not that the approver differs from the initiator.
**Fix:** record `run.initiated_by`; optionally enforce approver ≠ initiator for production changes (policy-configurable).

### P16 — DevOps CI "verify" doesn't wait for the run · **Low/Medium (correctness)**
**Evidence:** `agents/devops.py:170` — after `dispatch_workflow`, it immediately reads `latest_run_status` (may return the *previous* run; dispatch is async) with no poll loop.
**Fix:** capture the dispatched run id and poll to completion with a timeout.

### P17 — `notify` emails the sender, not stakeholders · **Low**
**Evidence:** `agents/notify.py:46` — `msg["To"] = settings.notify_from` (same as From). Stakeholder addressing isn't implemented.
**Fix:** address the initiator/approver/team; make recipients configurable.

### P18 — Gemini client does a sync network call in its constructor · **Low (latency)**
**Evidence:** `integrations/gemini.py:_resolve` calls `self.client.models.list()` synchronously in `__init__`, invoked lazily inside async handlers via the singleton.
**Fix:** make model resolution lazy/async or cache it out of the hot path; the singleton also ignores settings changes (test friction).

### P19 — Rate limiting applies to the SSE endpoint; some metrics never emit · **Low**
**Evidence:** `SlowAPIMiddleware` default limit covers `/chat` (a long-lived SSE POST); `TOOL_RETRIES`/`AGENT_STEP_DURATION` are declared but never incremented/observed.
**Fix:** exempt or separately tune the streaming route; wire or remove the unused metrics so dashboards aren't empty.

### P20 — `messages.content`/`runs.outcome` not redaction-scanned before persist · **Low (defense-in-depth)**
**Evidence:** `_persist_result` stores `state.get("answer")` verbatim; redaction is applied to console/graph/Langfuse but not to the persisted answer/outcome.
**Fix:** run `redact()` on `answer` and `redact_dict()` on `outcome` before DB write, as a backstop against a future agent echoing a secret.

---

## Severity roll-up

| Sev | Items |
|-----|-------|
| **Critical** | P1 reveal RBAC · P2 multi-tenancy · P3 read/stream authz |
| **High** | P4 streaming/leak · P5 idempotency double-apply · P6 blocking boto3 · P7 fake SRE remediation · P8 fake policy checks |
| **Medium** | P9 Traces tab · P10 model swap · P11 ask-cloud unreachable · P12 TF state lock · P13 dead code · P14 cross-store atomicity · P15 4-eyes · P16 CI wait |
| **Low** | P17 notify recipients · P18 gemini sync init · P19 rate-limit/metrics · P20 persist redaction |
