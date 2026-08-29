# Agent Loop (Governed Executive Loop) — ACTIVATED (Prompt 3)

> **STATUS CHANGE (2026-08-17, Prompt 3):** the dormancy documented below is OVER. The
> harness stack is the live execution spine — `AEGISOPS_EXEC_LOOP=on`,
> `AEGISOPS_HARNESS_READ_PATHS=on`, `AEGISOPS_DURABLE_ENGINE=on` (governance stamp
> live-verified). Multi-step goal DAGs execute through the P3 durable engine via the real
> terraform StepExecutor (DEF-17 closed): waves, claim-or-recover restart safety,
> RETAIN-on-failure (applied infra is never auto-destroyed by a later step's failure),
> step-boundary cancellation, per-step Prompt-1 revision bookkeeping, ALREADY_SATISFIED
> honesty on zero-change plans, and per-step live-cloud verification with an honest
> "applied but not fully verified" downgrade + bounded read-only investigation on verify
> failure. Day-2 modifies OBSERVE live security state before computing desired state.
> Engine bug fixed during activation: a FAILED step's idempotency claim was never
> released, making failed workflows unresumable (pinned in `test_p3_activation.py`).
> The activation ledger with run evidence lives in `INTELLIGENCE_LAYER_IMPLEMENTATION.md`
> and the Prompt-3 final report. The sections below are preserved as the pre-activation
> historical record.

## Prompt-3 live evidence ledger (2026-08-17, sandbox 714479440528)

| Capability | Status | Run / evidence |
|---|---|---|
| Multi-step DAG through the durable engine (real terraform executor) | **PROVEN ACTIVE** | `4d473d28`: spine-net VPC (wave 0) → SpineVM EC2 (wave 1, wired), both applied, **verified: True**; durable run_steps + revisions + full run_events lifecycle |
| Restart mid-workflow → resume | **PROVEN ACTIVE** | `8809152f`: API killed during wave-0 terraform apply; `recover_run` reclaimed the dead worker's stale claims (two layers — both fixed live), re-executed safely, completed; AWS shows exactly 1 probe-net VPC + 1 ProbeVM (no duplicates) |
| Step-boundary cancellation | **PROVEN ACTIVE** | `ffb96b2e`: cancel mid-wave-0 → terraform killed, outcome `cancelled` ("no steps applied"), nothing reached AWS; continuation-path status labeling fixed |
| Retain-on-failure (never auto-destroy) | PROVEN (container tier) | `test_p3_activation`: failed step → FAILED terminal, applied steps retained, compensator provably never called |
| Observe-before-act (live SG inspection) | **PROVEN ACTIVE** | `27088431`: "Inspected live security group · ports []" before planning; open → verified |
| ALREADY_SATISFIED / no-op | **PROVEN ACTIVE** | `a4b02899`: re-open of an open port → live-inspected → `no_change`, no approval |
| Verification gating (only VERIFIED = success) | **PROVEN ACTIVE** | verified:True on all applies; failure path downgrades honestly + bounded read-only investigation (container-proven) |
| Named-target determinism (mandate 10) | **PROVEN ACTIVE** | live catch: router target flake resolved "SpineVM" request to ProbeVM → fixed (`named_in_message` outranks fuzzy recency); retest bound correctly |
| Port lifecycle end-to-end | **PROVEN ACTIVE** | open `27088431` → verify read → no-op `a4b02899` → close `4d235b01`, each approved, live SG confirmed both ways |
| Temporal/provenance answers post-loop | **PROVEN ACTIVE** | "[] → [8501]" 16:31 / "[8501] → []" 16:37 with approvers + run ids, incl. the recovered run's revisions |
| Cloud isolation / concurrency | **PROVEN ACTIVE** | concurrent Azure-VM + AWS-VPC reads: zero leakage, both correct |
| SRE investigation flow | PARTIALLY PROVEN | routed → triage → telemetry + typed context threading; K8s disabled in env, model stream hiccup handled honestly |
| Engine bugs found & fixed by activation | — | (1) failed-step claims never released → unresumable workflows; (2) crashed-worker stale claims (engine + loop layers) treated as done-with-empty-outputs; (3) reconciler sweep missed the P3 transient statuses; (4) gw1 `exclude_user_id` drift |

---

**Date:** 2026-08-16. Proven live once (run `bed04e5a`), then the environment was restored to its default posture.

## 1. What it is

`app/agents/exec_loop.py::plan_goal_dag` — the multi-step "create-first DAG" executor. When a request needs a parent resource that doesn't exist (EC2 in a brand-new VPC), `dependency.resolve_closure` returns `status == "dag"`, and cloudops hands the whole DAG to the loop (`cloudops.py:618-622`):

- plans every step (parents first) with real `terraform plan` + policy checks per step,
- wires children to parents' real outputs (`wires`),
- pauses at ONE whole-DAG approval (`workflow: "governed-exec-loop"` interrupt),
- post-approval, the execute node applies the steps in order.

## 2. Why it never appears in Langfuse

1. **Flag off:** `aegisops_exec_loop: "off"` default (`settings.py:47`); not set in `.env` → the loop has never run in this environment before this audit.
2. **Narrow trigger:** even when on, only dependency-DAG requests enter it; single-resource requests take the ordinary path.
3. **No LLM calls of its own:** the probe produced ZERO `loop.main` ledger rows — its planning is deterministic, so there is no `llm.loop.main` generation for Langfuse even when it runs. Loop activity is visible as run steps/interrupt payloads, not as generations.
4. **Compounding:** all Langfuse traces currently land in a mismatched project ("AegisOps" ≠ "aegisops") — the dashboard under-reports everything.

With the flag OFF, the same DAG situation produces only a text message describing the ordered plan (cloudops.py:623-631) — the message *mentions* the governed loop while nothing executes it. That message is the cosmetic surface; the loop itself is real but dark.

## 3. The live proof (2026-08-16)

Procedure: set `AEGISOPS_EXEC_LOOP=on` in `.env` → recreate api → trigger → **reject at the approval gate** (zero mutation) → restore flag → recreate.

Trigger message: *"Create a t2.micro EC2 instance named loop-probe running Amazon Linux 2023 in a NEW VPC named loop-net with CIDR 10.44.0.0/16. Create a key pair, no remote access."*

Observed (run `bed04e5a`, SSE capture `loop_trigger.json`):

```
step: Target cloud · AWS (named in request)
step: Selected workflow · aws.ec2 v1
step: Planned step 1/2 · aws.vpc
step: Awaiting approval · whole goal DAG, one decision
interrupt: kind=approval, workflow=governed-exec-loop, mode=apply
  steps[0]: aws.vpc  "loop-probe-net"  plan {add:23, change:0, destroy:0} + policy checks
  steps[1]: aws.ec2  "loop-probe"      wired to step 1 outputs
```

Rejection returned `Plan rejected — no changes applied` (0.6 s); live AWS unchanged. No fake telemetry was produced — everything above is the loop's own real output.

## 4. Relationship to the dark P2/P3 stack (do not confuse)

| Thing | Package | Flag | Live callers | Status |
|---|---|---|---|---|
| Agent Loop / Governed Executive Loop | `app/agents/exec_loop.py` | `aegisops_exec_loop` (off) | cloudops.py:618 | DARK, proven working |
| Harness loop (P2) | `app/harness/loop.py` | `aegisops_harness_read_paths` (off) | none on chat path (SRE investigations use `harness/inv`; artifacts use `run_log`) | DARK |
| Engine (P3 durable execution) | `app/engine/*` | posture `durable_engine: off` | **zero** imports outside engine+harness; `tasks` table 0 rows | DARK, never executed |
| Retrieval gate / consolidation | `app/harness/memory.py` | — | none | NEVER EXECUTED |

## 5. Recommendations (post-audit)

1. Keep the loop dark until the two P0 mutation defects are fixed (silent no-op modify; failed-apply orphans) — a multi-step loop amplifies both.
2. When enabling: the loop's whole-DAG approval card is good; add per-step apply progress events (only steps/console exist today — no token stream during loop planning).
3. Decide the fate of the never-invoked governed purposes (`planner`, `judge`, `loop.main` bindings): either wire them (LLM-assisted DAG repair/critique) or remove them from the governed catalog so the model-governance story matches reality.
4. Langfuse project fix is a prerequisite for any "prove it in the dashboard" claim.
