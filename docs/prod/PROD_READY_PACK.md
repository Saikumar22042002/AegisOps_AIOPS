# PROD-READY PACK — seven operational items (paste to Claude Code with MODSEED_FINAL)

Owner-identified production gaps. These are the failure classes that don't show in demos:
"grows forever", "hangs forever", "no way back", "nobody notices". Add all seven to
FIX.md §8 as Phase-3 items with the positions stated below. Same discipline as everything
else: one item per commit, acceptance test per item, honest DLV entries where live creds
are needed.

## PR-1 TFHYGIENE — Terraform disk lifecycle (position: any time before MPP)
a. Plan-file lifecycle: delete the run's .tfplan when the run reaches a terminal state
   (applied/rejected/failed) — the reviewable record persists in runs.plan_json. Extend
   the reconciler sweep to remove stray plan files older than 7 days.
b. Destroyed-workspace pruning: a state workspace whose inventory row is destroyed AND
   whose `terraform state list` is empty for >7 days is pruned by the SWEEPER (logged,
   never silent, never inline at destroy time — no chat request can trigger a prune).
   Remote-state environments rely on bucket versioning for history.
c. Verify TF_PLUGIN_CACHE_DIR covers all MODSEED providers; .gitignore covers .terraform/
   everywhere.
Test: N runs → terminal → zero stray .tfplan; destroy → prune after threshold → day-2 on
OTHER resources unaffected.

## PR-2 LIMITS — concurrency + subprocess timeouts (position: before U6 exit if possible)
a. Max concurrent ACTIVE runs per org (Redis counter, config default e.g. 5) and per user
   (e.g. 2). At the limit, POST /chat for an actionable request returns an honest "N runs
   in progress — queued is not supported yet, retry when one completes" (429-style, clear
   UI message). Terraform processes are heavy; without this, one user can exhaust the host.
b. Per-stage subprocess timeouts on TerraformRunner: init/plan (default 10m) and
   apply/destroy (default 45m), env-configurable. On timeout: kill the process group,
   classify honestly ("terraform apply exceeded 45m — state may hold a lock; the
   reconciler/orphan sweep will reconcile"), run terminal `failed`, NEVER hang a live
   worker forever (today the supervisor heartbeat stays fresh while a subprocess hangs —
   the reconciler can't see it).
c. The executive loop inherits both: per-step timeout + the whole-DAG budget already
   specified in U6.
Test: fake runner that sleeps past the timeout → run fails honestly within bound; org at
limit → clear refusal, no run row leaked.

## PR-3 CANCEL — user-facing run cancellation (position: with/after U6)
a. Pre-approval phase (routing/discovery/planning): a Cancel control on the live run →
   cooperative cancellation → run terminal `cancelled` ("nothing was changed"), plan file
   cleaned (PR-1), idempotency released.
b. Awaiting approval: Reject already covers it (no new path).
c. DAG execution (U6): Cancel = halt-after-current-step — NEVER kill mid-apply. Outcome
   reports honestly: "steps 1–2 applied, cancelled before step 3."
d. Authz: initiator or approver, org-scoped; audited like approvals.
Test: cancel during plan → cancelled + no side effects; cancel mid-DAG → current step
completes, next never starts, honest partial outcome.

## PR-4 RETENTION — data-growth policy (position: Phase-3 tail)
Configurable retention sweeper (extends the reconciler's periodic loop; all OFF by
default in dev, documented prod defaults):
- messages/run_steps/notifications: archive-or-delete beyond N days (default 180) for
  CLOSED sessions only; message embeddings go with their messages.
- runs: keep the row (audit) but compact bulky plan_json beyond N days to the summary.
- audit_log + approvals: NEVER auto-deleted — retention is a compliance statement
  (document partitioning guidance instead).
- Langfuse: document its own retention config; Neo4j: closed contexts beyond N days
  pruned — the world model's LIVE resource graph is never pruned (it reconciles against
  reality; destroyed nodes age out with inventory).
Test: seeded old data → sweep → only eligible rows affected; audit rows untouched.

## PR-5 BACKUP — backup & restore, tested (position: Phase-3 tail; runbook + automation)
- Postgres: scheduled pg_dump (or WAL archiving) of the app DB to the object/backup
  volume; retention documented. Postgres is the ONLY store that must be backed up —
  state this explicitly: Redis is ephemeral coordination (rebuildable), Neo4j is a
  derived mirror (rebuildable from inventory + reconciliation sweep — provide a
  `rebuild_world_model` admin command to prove it), Terraform remote state relies on
  bucket versioning (verify enabled in the A3 backend-config docs).
- RESTORE_RUNBOOK.md: exact steps, and a TESTED restore — fresh Postgres from backup →
  app boots → sessions/runs/inventory intact → day-2 on a pre-backup resource works.
  An untested backup is not a backup.
Test: the restore drill, executed once and recorded in PROGRESS.md with evidence.

## PR-6 ALERTS — operator alerting (position: Phase-3 tail)
Ship Prometheus alert rules (infra/prometheus/alerts.yml) + docs for Alertmanager wiring:
stranded-run count > 0 for 10m · reconciler sweep failures · drift/orphan findings
(warning) · API 5xx rate · run failure-rate spike · event-bus publish errors ·
disk usage on the TF/plugin volumes (ties to PR-1) · Langfuse export failures.
Each rule has a runbook line ("what to check first"). Grafana panel additions optional.
Test: rules lint clean (promtool check rules); one alert fires in a simulated condition.

## PR-7 SUPPLY — dependency & image hygiene (position: Phase-3 tail; CI)
- CI gains pip-audit (backend) + npm audit --audit-level=high (frontend) as non-blocking
  report first, blocking on criticals after a baseline triage recorded in PROGRESS.md.
- Pin the API image base digest; document the rebuild cadence. Renovate/Dependabot config
  optional (owner choice).
Test: CI runs the audits; a seeded known-vuln in a test manifest is caught.

## Ordering note
PR-1 and PR-2 are code items and cheap — slot them before the Phase-3 exit gate. PR-3
pairs with U6. PR-4/5/6/7 form the Phase-3 tail after MPP, before the final DEFERRED
LIVE VERIFICATION run. The Phase-3 exit gate now ALSO requires: PR-1..3 demonstrated,
PR-5 restore drill executed, PR-6 rules shipped.

## Alignment notes (BINDING on PR-1..PR-6 — reuse the existing Phase-1/2 machinery, never
## build parallel mechanisms)
1. **PR-1 vs B3 resume:** the reconciler re-drives a crashed run by applying its SAVED
   plan file — plan files are deleted ONLY at terminal states, and the 7-day stray sweep
   must check run status first: a run in `awaiting_approval` may legitimately wait days;
   its plan file is NOT stray.
2. **PR-2 concurrency:** derive the active-run count from the EXISTING liveness truth
   (`runs.status` non-terminal + fresh `run:<id>:hb` heartbeats) — do NOT add a separate
   Redis counter that can drift or lock an org out after a worker crash; heartbeat-derived
   counts self-heal exactly like the reconciler does.
3. **PR-2 timeout kill:** SIGTERM → grace → SIGKILL on the process group; expect and
   classify the leftover state lock via provider_errors; rely on terraform's stale-plan
   protection for resume-after-partial-apply; add force-unlock guidance to the runbook.
4. **PR-3:** `cancelled` becomes a first-class TERMINAL status everywhere — reconciler
   scan sets, the B5 terminal guarantee, UI badges, overview counts, AGENT_RUNS metric
   labels. Grep every status-set literal.
5. **PR-4:** compacting old run_steps/plan_json changes the Traces/Terraform tabs for old
   runs — they must show an honest "compacted per retention policy" note, never
   empty/fake. Closed sessions only; embeddings cascade with their messages.
6. **PR-5:** pg_dump of the app DB includes the LangGraph checkpoint tables — the restore
   drill must PROVE an `awaiting_approval` run survives restore and can still be approved.
   Note the langfuse DB separately, and state that realm-export.json is the Keycloak
   backup — keep it current (it was modified in S0 for the org groups).
7. **PR-6:** the alert rules need gauges that don't exist yet (aegisops_stranded_runs,
   reconciler_errors, drift_findings) — emit them from the reconciler/drift sweeps as
   part of the item, O3-style.
