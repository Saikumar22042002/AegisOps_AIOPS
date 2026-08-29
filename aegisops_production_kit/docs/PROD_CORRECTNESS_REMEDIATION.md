# Production Correctness Remediation — 2026-08-17

Closes the P0/P1 defects proven by the forensic audit (`AEGISOPS_CURRENT_STATE.md`,
2026-08-16). Every fix reuses the existing implementation — no new orchestrator, loop,
engine, retrieval, or state store was added. All work is **uncommitted** on
`feature/cloudops-v3`, per the standing acceptance pattern.

Proof levels: **LIVE-PROVEN** (real cloud + running stack, run IDs cited) ·
**UNIT-PROVEN** (deterministic pins in `tests/test_prod_correctness.py`) ·
**BLOCKED BY ENVIRONMENT** (sandbox limitation, stated honestly).

## 1. Fixes and their evidence

| # | Defect (audit) | Fix (existing code, repaired) | Proof |
|---|---|---|---|
| D-1a | Zero-change mutation reported `applied: true` (port "removal" no-op) | `plan_guard.zero_change` + NO_CHANGE guards in `_modify_resource` (input-level + plan-level) and the create path; new outcome status `no_change` never enters approval | LIVE: run `d18f4ba3` — repeated "versioning off" → `status: no_change`, "no plan was sent for approval and nothing was applied" |
| D-1b | remove/close verbs became open/add | verb-aware `_extract_port_changes` (open_ports/close_ports), `ingress_ports_remove` change key + capability, desired-state merge (union opens − closes), deterministic close-verb override that beats the LLM | UNIT ×8 (incl. the exact live failure replayed: model inverts, guard corrects). LIVE port cycle **BLOCKED BY ENVIRONMENT** (sandbox blocks ALL `ec2:RunInstances` — "api error Blocked") |
| D-2 | Failed applies orphaned real infra: invisible, unretryable, undestroyable | `inventory.record_partial` (status `partial` + revision) called from `cloudops_execute`'s failure path; `resolve(statuses=…)` lets destroy see partial rows; `mark_destroyed_txn` flips them; failure outcome carries `partial`/`partial_resources` | LIVE full loop: run `52aecb9b` fail → `partial` row + revision; run `2567c8c8` destroy resolved the partial row, terraform removed the real SG/key-pair (AWS verified empty), trail `partial → partial → destroyed` |
| D-3 | `aws.vpc` placed every subnet in Local Zones | AZ data source filtered to `opt-in-status = opt-in-not-required`; `map_public_ip_on_launch = true` | LIVE: run `d0e4058a` — `fix-net` subnets in `us-east-1a/b/c` in the SAME account class that produced bos/chi/atl before the fix |
| D-4 | VPC named in the message ignored; default VPC unreachable; menu loop | `dependency._candidates_named_in` (message binds a single named parent), `__default__` choice + `_WANTS_DEFAULT` (bare-reply + in-sentence), default offered in the ask text | LIVE: run `e8cf13f4` — "in the fix-net VPC" bound `subnet-097734fda018d577c` (fix-net public[0]); "in the default VPC" run `52aecb9b` planned without a menu; "default" reply accepted (run `bb47d2b1`) |
| D-5a | AWS question listed GCP rows | broad listing scoped to clouds named in the question (`named_clouds`) | LIVE: run `39199ac4` — AWS listing has zero GCP rows |
| D-5b | Ghost rows from dead sandboxes rendered "active" | `inventory.verify_aws_liveness` — batched read-only describes mark missing rows `unreachable` before listing | LIVE: 20 rows swept to `unreachable`; listing dropped from 16 ghosts to 2 honest rows |
| D-6 | "who approved / what changed / previous config / yesterday" unanswerable or misrouted to devops | router post-guard reroutes provenance/history questions to a deterministic read; `_history_answer` renders `resource_revisions` ⋈ `approvals`; hooks in `_read_resource` + `_read_path` | LIVE: runs `2a48f021` (today's timeline, 7 events), `1f63dd27` (who approved fix-net → maya.okafor 03:35 UTC), `a09c1435` (previous config timeline) |
| D-7 | Parameters forgotten after a failed run | failure path preserves validated inputs as pending (collection-spec shape via new `params.from_tf_vars`); load honors `after_failure` with a same-template guard | LIVE partial (name/type/OS carried, run `c4298ff8`); spec-shape round-trip UNIT-PROVEN; full continuity re-proof blocked by the RunInstances block |
| D-8 (new, found live 2026-08-17) | "Destroy FixProbe." swallowed as a pending collection's ANSWER | `intent_guard.message_shape`: destructive-verb start = always a new request | LIVE: run `2567c8c8` routed to destroy after the fix; UNIT pin |
| D-9 (new, found live) | Approval card claimed "account's default VPC" while a named VPC was bound | `_defaulted_dependencies` keyed on the real placement input (`subnet_id`, not the nonexistent `vpc_id`) | UNIT (updated `test_defaults_honesty` pin — the old pin tested a field no input carries) |
| D-10 | Every touching run accreted a `CREATED` graph edge | `action` threaded to `world_model.upsert_resource` AND `ContextGraph.add_resource` → `MODIFIED` edges for modifies | LIVE (world model): bucket has `CREATED` (create run) + `MODIFIED` (modify run); context-graph writer fixed after the live check caught its extra edge |

## 2. Resource state & history (Task 2)

New **`resource_revisions`** table (migration `0016`, applied; model `ResourceRevision`) —
append-only, written in the SAME transaction as each inventory mutation:
org/resource/run/session/actor + name/cloud/region/type + action
(`created|modified|destroyed|failed|partial|orphaned|no_change|unknown`) + reason (user
message) + before_state/after_state/inputs + execution_result + timestamp. The `resources`
table remains the current-state inventory (now with `partial` and `unreachable` statuses in
active use); revisions are never updated or deleted. A modify records `modified` — never
another `created` — in PG and in both graphs.

## 3. Source-of-truth enforcement (Task 4)

- Live cloud now overrides stale memory on the listing path (liveness sweep) and remains
  authoritative on single-resource reads (existing reconcile).
- PG = durable records incl. the new immutable journal. Neo4j = topology/provenance mirror
  (edge types now truthful). Terraform state = per-resource workspaces, now always
  represented in inventory (active OR partial) — never invisible.

## 4. Testing summary

- `tests/test_prod_correctness.py`: 25 deterministic pins (all green in container).
- Affected suites green: mod_day2, dependency, inventory, p0_defects, exec_loop,
  bugfix2_dep_convergence, defaults_honesty (one pin updated — it tested a field the schema
  never carried).
- **Eval gate: OPEN 10/10 + self-test** after the router post-guard change (rule zero held).
- Full container regression: see final report (env-tier terraform failures unchanged).
- LIVE battery (sandbox account `670219536150`, session `065271d5`): VPC create (standard
  AZs), EC2 fail→partial→destroy recovery, named-VPC binding, default-VPC binding + menu,
  S3 create→modify→NO_CHANGE, history/provenance ×3, scoped listing, ghost sweep.

## 5. Honest limitations

- **EC2 instance launches are blocked account-wide in the current sandbox** ("api error
  Blocked", both t2.micro and t3.micro) — live port add/remove on a real instance, live
  named-VPC APPLY (plan-level proven), and full retry-continuity re-proof are BLOCKED BY
  ENVIRONMENT until a sandbox that permits RunInstances is available.
- Ghost resources still appear as DEP menu candidates (liveness sweep runs on listing
  paths only; the resolver is pure/sync). Choosing one fails at plan time, honestly.
- Liveness sweep covers AWS ec2/vpc/s3 only; Azure/GCP rows stay unverified (annotated).
- Pre-0016 history exists only as run records; the journal starts 2026-08-16.
- `verification` column on revisions is reserved (the verify node's evidence still lands in
  tool_results + graph Evidence nodes).

Residue in sandbox (auto-expires): `fix-net` VPC, `aegis-fixp-260817` bucket (versioning
suspended), UserProbe/FixProbe partial SG+key remnants destroyed or rejected before apply.
