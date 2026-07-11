# §3a — Fixes: Safety & state isolation · Reliability · Connectivity

[← back to FIX index](../../FIX.md) · Sizing: **S**<1d · **M** 2–4d · **L** ≥1wk. Blast radius: low/med/high.

Each fix cites the `ANALYSIS` finding + `file:function` it addresses, gives the change, size, blast radius, and how to verify.

---

## A. Safety & state isolation

The destructive-safety architecture is already the strongest part of the codebase (ANALYSIS finding #1). These fixes **close the remaining gaps**, they don't rebuild it.

### A1 — Idempotency: wait-or-abort, never fall through · **P5** · S · blast: low
**Now** (`agents/cloudops.py:935`): `if not await idempotency.claim(key): done = get_result(key); if done: return …` — a concurrent in-progress claim returns `None`, and control **falls through to `runner.apply()`** → double apply.
**Change:** on a failed claim with no stored result, **poll** `get_result` with a short backoff up to a deadline; if still in-progress at the deadline, **abort** with a `409-style` outcome ("this change is already being applied") — never execute. Also add an endpoint guard in `resolve_approval` (`api/chat.py`) rejecting a second `/approvals/{id}` while the run's execution is live (`runs.status` transitions `awaiting_approval → applying`).
**Verify:** new test — two concurrent `cloudops_execute` for one run → exactly one `apply`, the other returns the stored result or aborts (extend `tests/test_idempotency.py`).

### A2 — Action↔operation guard: keep, extend to the planner · **finding #1, P?** · S · blast: low
**Now:** `agents/plan_guard.check_plan_actions(action, diff)` is called for create/modify/destroy and is correct (create⇒no delete/replace, modify⇒update-in-place, destroy⇒deletes-only, read⇒no plan).
**Change:** (1) make it a **hard invariant on every path that reaches an approval interrupt** — add a single choke-point assertion in the approval node (`agents/approval.py`) that re-runs `check_plan_actions(state["action"], state["diff"])` before `interrupt()`, so no future code path can skip it. (2) When the bounded planner lands ([05 §DAG](05_target_architecture.md)), apply the guard **per step** in the DAG.
**Verify:** `tests/test_safety_invariants.py` already covers the pure function; add an integration test asserting the approval node refuses a mismatched plan even if a plan node forgot to call the guard.

### A3 — Per-resource state isolation: keep, unconditional plan-file + remote backend · **P12** · M · blast: medium
**Now:** `TF_WORKSPACE=res-<slug>` isolates state per resource (`tools/terraform.py:state_slug`, `ensure_state_workspace`), but **legacy resources share `aegisops.tfplan`** (`tools/terraform.py:61`) and the backend is `local` with no lock (`infra/terraform-workspaces/*/main.tf`).
**Change:** (1) Give **every** run a unique plan-file path (`aegisops-<state_workspace>-<run_id>.tfplan`) unconditionally, so no two operations ever share a plan file. (2) Default to a **remote backend with locking** (S3+DynamoDB — already env-plumbed via `TF_STATE_*`, just not defaulted): parameterize `backend` via `-backend-config` at `init` so `local` stays the dev default but staging/prod get locked remote state. (3) Document a migration for existing `local` state → remote.
**Blast radius note:** touches every module's `init` path; must be feature-flagged by env so local dev is unaffected.
**Verify:** extend `tests/test_safety_live.py` (create X → create Y with zero-destroy → both exist → destroy X → only X gone) to run against a remote backend in CI-with-creds; assert two concurrent plans don't collide on a plan file.

### A4 — Same-name-create refusal: keep, make it org+state-workspace aware · **finding #1** · S · blast: low
**Now:** `agents/cloudops.py:314` refuses a create whose name maps to an already-active resource in the same module. Correct.
**Change:** ensure the duplicate check is **org-scoped** once P2 lands (today `list_active(org)` is called with the default org). No logic change, just carry the real org.
**Verify:** covered once P2's org scoping is in; add an assertion in the dup-name test.

### A5 — Record the initiator; optional 4-eyes · **P15** · S · blast: low
**Now:** the run has no initiator; `resolve_approval` only checks `require_approver`.
**Change:** add `runs.initiated_by` (migration), populate it in `api/chat.py:chat` from the authenticated user, and add a **policy-configurable** guard in `resolve_approval` that (for `env=Production`) rejects `approver == initiator` with a clear message. Default off in non-prod.
**Verify:** `tests/test_rbac_endpoints.py` — a prod run where initiator tries to self-approve → 403; non-prod → allowed.

---

## B. Reliability — every run reaches a terminal state, survives restart, and never hangs

### B1 — Durable event bus (Redis Streams) replacing in-process channels · **P4, harness H1** · L · blast: high
**Now:** `agents/events.py:_channels` module-global; `RunChannel` = asyncio.Queue + 1000-deque; `drop_channel` never called in app (ANALYSIS P4).
**Change:** re-implement the bus on **Redis Streams**, key `run:<id>:events`, id = the Redis stream id (monotonic, replaces the seq counter):
- `Emitter.*` → `XADD run:<id>:events * event <name> data <json>`.
- `_sse` → `XREAD BLOCK` from `Last-Event-ID` (or `$`), yielding frames; exactly-once is inherent to stream ids.
- Terminal (`done`/`error`-final) → set a short TTL / `XTRIM` so the stream self-evicts (fixes the leak).
- Keep the `Emitter` interface identical so no agent node changes.
**Blast radius:** every emit path + `_sse` + `/chat/stream` reconnect; the `Emitter` façade contains it. Feature-flag `AEGISOPS_EVENT_BUS=memory|redis` for a parallel-run/rollback.
**Verify:** port `tests/test_sse_contract.py` to the Redis bus (exactly-once, replay-after-id, done stops the stream); add a **multi-worker** test: publish from worker A, `XREAD` from worker B → frames arrive.

### B2 — Supervised runner + graceful shutdown · **harness H2** · M · blast: medium
**Now:** `api/chat.py:171 asyncio.create_task(_drive())` — untracked; on shutdown or crash the task is orphaned.
**Change:** a `RunSupervisor` (module in `agents/`) owning a registry of live run tasks with a per-run **heartbeat** (Redis `run:<id>:hb` with TTL, refreshed by the drive loop). Register on start, deregister on terminal, cancel-and-persist-failed on shutdown (in `main.py` lifespan). The API can answer "is this run live here?" for reconnect decisions.
**Verify:** kill the supervisor mid-run → the run's heartbeat expires (observable); shutdown → in-flight runs persist as `failed` with a real message, not silently dropped.

### B3 — Stranded-run reconciler · **harness H2/H3** · M · blast: medium
**Now:** nothing re-drives a run stranded in `running`/`awaiting_approval` after a crash (only a user POST resumes an approval).
**Change:** a periodic reconciler (async task started in lifespan, or a `CronCreate`-style scheduled job) that finds `runs.status IN (running, applying)` with an **expired heartbeat and no live supervisor entry**, and for each: read `graph.aget_state(config)`; if resumable, re-drive via the supervisor; else mark `failed` with "recovered after an interruption — nothing was changed beyond what the Logs show." `awaiting_approval` runs are left for the human but surfaced as "waiting."
**Blast radius:** reads `runs` + checkpoints; idempotency (A1) makes re-drive safe.
**Verify:** integration test — start a run, kill the drive after `plan` but before `apply`, run the reconciler → run resumes to a terminal state exactly once (no double apply, thanks to A1).

### B4 — Verification always terminates (keep, extend cross-cloud) · **ANALYSIS finding N-01, §04/§05** · S · blast: low
**Now:** `agents/finalize.py:verify` is 30s timeout-bounded and AWS-only reconciliation.
**Change:** keep the timeout; add Azure/GCP branches to `_reconcile_checks` (mirror the AWS EC2/S3 pattern via `tools/azure.py`/`tools/gcp.py`), so a non-AWS apply gets a real live check, not just "outputs present." Ensure every branch is itself timeout-bounded and thread-offloaded (ties to B6).
**Verify:** `tests/test_run_lifecycle.py::TestVerifyTerminates` extended per cloud (mock SDK slow → returns the warn card, never hangs).

### B5 — Runs always reach a terminal state · **harness H3** · S · blast: low
**Now:** `_drive` persists `failed|awaiting_approval|completed`; but an exception path outside the graph (e.g. the persist itself) could leave `running`.
**Change:** wrap the whole `_drive` in a `finally` that guarantees the run is in a terminal or explicitly-waiting state before the channel closes; the reconciler (B3) is the backstop.
**Verify:** fault-injection test — raise inside `_persist_result` → run ends `failed`, not `running`.

### B6 — No blocking I/O on the event loop · **P6** · S · blast: low
**Now:** `agents/inventory.py:229 reconcile` builds a sync `boto3` client and calls `describe_instances` in a coroutine.
**Change:** route reconciliation through `tools/aws.py:AWSReader` (which already thread-offloads via `anyio.to_thread`), or wrap the describe in `anyio.to_thread.run_sync`. Audit all agent code for other sync SDK calls in coroutines.
**Verify:** a test that asserts `reconcile` doesn't block (e.g. concurrent reconcile + a fast endpoint stay responsive); code-review grep for `boto3.client(`/`.describe_`/`.list_` outside `tools/`.

### B7 — Idempotency lifecycle correctness (supporting A1) · S · blast: low
**Change:** add an explicit `in_progress` wait/expiry to `security/idempotency.py` (a claim TTL shorter than the run, plus a `waiting` helper) so A1's poll has a bounded, correct primitive.
**Verify:** `tests/test_idempotency.py` — claim→in_progress→(wait)→done transitions.

---

## C. Connectivity & usability

These are largely **already implemented** (N-02) — the plan is *verify + secure*, not rebuild.

### C1 — Ingress/CIDR actually applied (verify + regression-guard) · **ANALYSIS §03/§04/§05, N-02** · S · blast: low
**Now:** `allowed_cidr` opens admin port (22/3389) to that CIDR only; `ingress_ports` open app ports; GCP VM network tags fixed so firewalls attach (`infra/terraform-workspaces/gcp-gce/main.tf:86`).
**Change:** no logic change; add **plan-assertion tests** that a collected `allowed_cidr`/`ingress_ports` produce the expected SG/NSG/firewall rules in `terraform show -json` (guards against a future module edit dropping the rule — the exact class of the GCP-tags bug).
**Verify:** per-cloud plan-parse test asserting the admin rule targets the CIDR and app rules target the ports.

### C2 — Honest success cards (keep) · **ANALYSIS N-06** · S · blast: low
**Now:** `agents/cards.success_card` posts real host/user/port/ARN/endpoint per type.
**Change:** none functionally; once verify is cross-cloud (B4), ensure the card's `host`/`connection` reflect the reconciled values, not just TF outputs.
**Verify:** `tests/test_usable_outputs.py::TestSuccessCards` extended per cloud.

### C3 — Credential delivery secured (see security §3b) · **P1** · — · —
The one-time reveal is real but under-secured; the fix (mandatory re-auth + audit + ownership) is in [03 §Security](03_fixes_memory_security_obs.md#s1--credential-reveal-mandatory-re-auth--audit--ownership--p1). Cross-referenced here because it's the connectivity payoff (getting into the VM you just made).

---

## Summary — safety/reliability/connectivity effort

| Item | Size | Blast | Phase |
|------|------|-------|-------|
| A1 idempotency wait-or-abort | S | low | 1 |
| A2 guard at approval choke-point | S | low | 1 |
| A3 unique plan-file + remote backend | M | medium | 1–2 |
| A4 org-aware dup check | S | low | 1 (with P2) |
| A5 initiator + 4-eyes | S | low | 1 |
| B1 Redis Streams bus | L | high | 2 |
| B2 supervised runner | M | medium | 2 |
| B3 reconciler | M | medium | 2 |
| B4 verify cross-cloud | S | low | 2 |
| B5 terminal-state guarantee | S | low | 1 |
| B6 no blocking I/O | S | low | 1 |
| B7 idempotency lifecycle | S | low | 1 |
| C1 ingress plan-assert tests | S | low | 1 |
| C2 honest cards cross-cloud | S | low | 2 |
