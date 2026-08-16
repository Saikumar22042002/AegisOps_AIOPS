# CloudOps Harness — The Governed Execution Engine

> The execution plane of the split-trust architecture: how multi-resource, long-running,
> approval-gated, resumable, rollback-capable CloudOps workflows run. This is an
> **evolution of `agents/exec_loop.py`**, not a replacement of its principles — the
> audited behaviors that are already right (catalog-only validation, output wiring,
> deviation re-approval, per-step idempotency, boundary-only cancel, honest partials)
> become the engine's invariants.
>
> Companions: `Agent_Harness.md` (who plans), `System_Design.md` (how it all flows),
> `Implementation_Roadmap.md` (sequencing).

---

## 1. Position in the architecture

```
Intelligent Shell (LLM)                    Governed Core (deterministic)
────────────────────────                   ──────────────────────────────
planner agent drafts GoalDAG   ──────▶     compile + validate (pure code)
investigator loops read-only   ◀──────     world model / SDK reads
                                           approval artifact → HUMAN GATE
                                           WorkflowEngine walks the DAG
                                           executors: Terraform | K8s | Day-2
                                           verify → evidence; saga on failure
```

Rules of the boundary (unchanged from today, now stated as engine contract):

1. The LLM proposes **data** (a GoalDAG referencing catalog templates + params). It
   never authors HCL, never picks executors, never sees credentials.
2. Everything after approval is deterministic: same DAG + same world state → same
   actions. Where reality diverged from plan-time assumptions, the engine **stops and
   re-asks** (deviation), it never improvises.
3. One mutating entry point: `execute_governed_step(step)` — the interior is the
   existing pipeline (validate → plan → guard → policy → apply → verify → record).

---

## 2. Core model

### 2.1 The Step contract

Today a DAG node is `{template_key, params, wires}` (`exec_loop.py:100-175`). The
engine widens it into a five-phase contract — every phase deterministic, every phase
optional except `action`:

```python
# app/engine/steps.py
@dataclass(frozen=True)
class Step:
    id: str                                  # "s1"; stable across resume
    kind: Literal["module",                  # Terraform catalog template
                  "day2",                    # governed SDK verb (§6)
                  "k8s",                     # chart/manifest catalog entry
                  "read",                    # discovery/lookup, no approval weight
                  "gate"]                    # explicit human/time gate (§5.4)
    template_key: str                        # must exist in the approved catalog
    params: dict[str, Any]                   # validated against the template schema
    wires: dict[str, str] = field(default_factory=dict)
                                             # existing grammar, kept verbatim:
                                             # "<out>", "<out>[i]", "input:<field>"
                                             # + new: "steps.<id>.outputs.<name>"
    depends_on: tuple[str, ...] = ()         # explicit edges (wires imply edges too)

    preconditions: tuple[Check, ...] = ()    # world-model/SDK assertions evaluated
                                             # just before the step runs (e.g. "vpc
                                             # still exists", "quota headroom ≥ 1")
    verify: VerifyPlan | None = None         # SDK reads + health probes + timeout;
                                             # produces an EvidenceCard, not a bool
    compensation: CompensationRef | None = None   # §8 — the pre-approved inverse
    retry: RetryPolicy = RetryPolicy()       # §7 — per-error-class, bounded
    timeout_s: int = 1800
    blast_radius: Literal["low", "medium", "high"] = "medium"  # drives approval tier
```

```python
@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 1                    # default: no auto-retry on mutation
    retriable_kinds: tuple[str, ...] = ()    # from the cloud error taxonomy
                                             # (provider_errors kinds + "transient")
    backoff_s: tuple[int, ...] = (30, 120)

@dataclass(frozen=True)
class CompensationRef:
    template_key: str                        # catalog action that undoes this step
    mode: Literal["destroy_created", "restore_previous", "rollout_undo", "none"]
    params_from: dict[str, str]              # wires from THIS step's outputs/state
```

### 2.2 The Workflow

```python
@dataclass(frozen=True)
class Workflow:
    run_id: str
    steps: tuple[Step, ...]                  # ≤ max_steps (configurable; 5 today)
    on_failure: Literal["halt",              # default — today's behavior
                        "rollback",          # reverse-order compensation (§8)
                        "continue_independent"]  # only parallel branches unaffected
    window: ChangeWindow | None = None       # §5.4
    approval: ApprovalPolicy = ApprovalPolicy.SINGLE_DAG   # §4
```

### 2.3 Compilation and validation (pure code, before any human sees it)

`compile_goal_dag(draft) -> Workflow | RefusalReport` — extends `validate_dag`
(`exec_loop.py:81-97`), keeping its two hard rules and adding four:

1. **Catalog-only** (kept): every `template_key` resolves in the approved catalog —
   "never generated infrastructure code."
2. **Bounded** (kept): step count, replan bounds.
3. Wiring closure (new): every wire target exists, is type-compatible with the
   consuming param's schema, and the graph is acyclic. Unresolvable wire → refusal,
   never a guess (today's `KeyError` rule, formalized).
4. Guard closure (new): per-step `plan_guard` action-class check at compile time
   *and* re-asserted at execute time (today's double-check, kept).
5. Compensation closure (new): if `on_failure=rollback`, every mutating step must
   carry a valid `CompensationRef` whose template also passes catalog validation —
   **a rollback plan that can't be compiled is caught before approval, not at 3am.**
6. Lock plan (new): the resource scopes each step touches (vpc-id, cluster, state
   workspace) are computed here so the scheduler can enforce mutual exclusion (§5.2).

---

## 3. Execution semantics

### 3.1 Waves, not just sequence

Today the loop is strictly sequential (`for index, step in enumerate(...)`). The engine
schedules by readiness — waku's graph engine demonstrates the discipline that keeps
parallelism debuggable (`waku/graph/engine.py`):

- A step is **ready** when all `depends_on`/wire sources completed and its locks are
  free. All ready steps of a wave may run concurrently.
- Parallel steps must write **disjoint outputs** (compile-time check; a collision is a
  compile error, not a race).
- Wave membership is recorded on the run, so traces read deterministically and a
  resume reconstructs the same schedule.
- Concurrency cap per run (default 3) and per org (existing run-level caps stay).

### 3.2 One step's lifecycle (the inner pipeline, today's code preserved)

```
claim idempotency key ("loop-step", run_id, step.id)        # exists — kept verbatim
  └─ lost claim → get_result | wait_for_result | abort       # never a second apply
preconditions → unmet? → DEVIATION (§4.3)
plan   : terraform plan in the step's own state workspace    # exists
guard  : plan_guard.check_plan_actions                        # exists, both ends
policy : template.policy_fn over planned_resources()          # exists — real predicates
apply  : executor.apply(plan_artifact) streaming console      # exists
verify : VerifyPlan → EvidenceCard (SDK reads, health)        # today: AWS-only verify
record : inventory + world model + run_steps, same txn        # exists
store idempotency result                                      # exists
```

### 3.3 Crash/resume

Nothing new is invented here — the engine formalizes what the audit confirmed works:
LangGraph Postgres checkpoint (thread == run) + heartbeat supervisor + 60s reconciler
redrive (`reconciler.py:42-80,146-201`) + per-step idempotency = a worker killed
mid-workflow resumes on another worker, already-applied steps return their stored
results, and the wave scheduler recomputes readiness from `run_steps` ground truth.
The one addition: **engine state is derivable from the step log alone** (event-sourced
projection), so "what happened" never depends on process memory — the OpenHands
event-stream lesson applied to infrastructure.

---

## 4. The approval model

### 4.1 One approval per DAG (kept), richer artifact (new)

The approval artifact is the product — Antigravity's artifacts-first lesson applied to
change management. It contains, per the whole workflow:

| Section | Source | Today? |
|---|---|---|
| Step list with per-step TF plan summaries (`+a ~c -d`) | compile phase | yes (`exec_loop.py:154-162`) |
| Policy table (real predicates; "not evaluated" rows stay honest) | policy phase | yes |
| Cost estimate + guardrail verdict | cost catalog | yes (static catalog, labeled) |
| Blast radius: world-model `impact_of` per touched resource | Neo4j world model | destroy-only today → extend to all mutations |
| **Verification plan**: what will be checked after each step | `Step.verify` | new |
| **Rollback plan**: the compiled compensation chain, human-readable | `Step.compensation` | new — approving the change approves its undo |
| Deviation policy: what triggers re-approval | engine constants | implicit today → stated |
| Window: when it may execute | `ChangeWindow` | new |

### 4.2 Approval tiers

`ApprovalPolicy` per workflow, chosen by policy (env + blast radius), not by the model:

- `SINGLE_DAG` (default) — one interrupt for the whole plan (today's behavior).
- `PER_STEP_HIGH` — steps with `blast_radius=high` in production get their own gate
  even inside an approved DAG (enterprise change boards want this for destroys).
- `PRE_APPROVED` — §9's auto-remediation tier: catalog actions an org has standingly
  approved (restart deployment, scale +1) with rate limits; still audited, still
  verified, still four-eyes-exempt only below a blast-radius line.

Four-eyes for production stays enforced at the approval core (`chat.py:453-457`) for
every tier that interrupts. (Audit note: this install currently runs with four-eyes
**disabled** via `.env` — the roadmap makes governance flags part of the approval
artifact so a weakened posture is visible on every card, not silent.)

### 4.3 Deviations (kept, broadened)

Today: any revision to an approved step interrupts with a was/now diff
(`exec_loop.py:302-311`). The engine keeps that and names the triggers:

- parameter change (existing) · failed precondition (new) · verify failure with a
  proposed fix (new) · retry budget exhausted with an alternative available (e.g.
  `bad_location` → alternate region via `suggest_retry`, once D1 is fixed) ·
  compensation about to run in `rollback` mode when the plan didn't pre-approve it.

A rejected deviation → honest partial outcome (existing `_partial_outcome` path).

---

## 5. Long-running workflows

### 5.1 Progress
Executors stream (the pi/waku subprocess pattern — newline-JSON events over stdio):
console lines (exists for TF), phase transitions, percentage where the tool gives one
(K8s rollout). All onto the run's Redis stream; gateways' progressive preview already
renders it.

### 5.2 Locks
From the compile-time lock plan: Redis locks on resource scopes
(`lock:org:{org}:scope:{state_slug}`) with TTL + heartbeat re-extension. Two runs
touching the same VPC serialize; unrelated runs parallelize. Lock wait > threshold →
visible "queued behind run X" event (honesty rule).

### 5.3 Waits that don't burn workers
Approval waits are already durable interrupts (indefinite, checkpointed). Long
polls (RDS available ~10min, GKE ~15min) run as supervisor-tracked tasks with
heartbeats; the reconciler treats a heartbeat-dead poll like any stranded run.

### 5.4 Change windows
`ChangeWindow(cron_or_range, timezone)` on the workflow: approved-but-outside-window
runs park as `scheduled` (a first-class status replacing the vestigial `"applying"`
literal — audit D5) and the reconciler launches them when the window opens.
Approval-to-execution gap > policy max (default 24h) → preconditions re-evaluated;
drift found → deviation re-approval. **An old approval is not a blank check.**

---

## 6. Day-2 verbs — the honest Terraform carve-out

Start/Stop/Restart/Resize don't map cleanly onto "Terraform-only mutation": stop/start/
restart are not desired-state changes Terraform expresses (resize is — `-var
instance_type` — and stays TF). Today the platform's honest answer is refusal or a
COMP intercept. The engine adds a **governed day-2 action registry** instead of
pretending:

```python
DAY2_ACTIONS = {
  "aws.ec2.stop":    Day2Action(sdk_call="ec2.stop_instances",  preconditions=[running],
                                verify=[state_is("stopped")],   compensation="aws.ec2.start",
                                blast_radius="low"),
  "aws.ec2.start":   ...,
  "aws.ec2.restart": Day2Action(sdk_call="ec2.reboot_instances", verify=[status_checks_ok]),
  "azure.vm.stop":   Day2Action(sdk_call="vm.deallocate", ...),   # deallocate ≠ stop: billing
  "k8s.deploy.restart": Day2Action(sdk_call="rollout.restart", verify=[rollout_complete]),
}
```

Governance identical to Terraform steps: typed params, RBAC, approval interrupt (tier
by blast radius — stop dev VM can be `PRE_APPROVED`; stop prod DB is `SINGLE_DAG` +
four-eyes), idempotency keys, audit rows, verify with evidence, inventory + world-model
update. **The registry is code-reviewed like catalog modules; the LLM selects from it
by key, exactly as with templates.** The module contract "cloud SDKs are read-only"
becomes "read-only *except* `app/engine/executors/day2.py`, which is registry-only" —
one file to audit instead of a platform-wide fiction that lifecycle verbs don't exist.

Resize stays Terraform (it *is* desired state): `modify` template path, plan diff shows
the instance_type change, normal approval.

---

## 7. Failure model

Error classes reuse the audited cloud taxonomy (`provider_errors.py`: credentials_
expired, api_disabled, iam_denied, name_taken, quota_exceeded, bad_location) plus
`transient` (throttling/5xx) and `timeout`.

| Class | Engine behavior |
|---|---|
| transient / timeout | auto-retry within `RetryPolicy` (idempotency key makes it safe); TF apply retries only after a state-consistency read |
| bad_location / name_taken | no blind retry — `suggest_retry` alternates become a **deviation proposal** (fix D1 first: the classifier/suggester kind mismatch makes this branch unreachable today) |
| credentials_expired / api_disabled / iam_denied | halt + the existing classified card (what/why/next-step); resumable after the operator fixes cause |
| quota_exceeded | halt; propose smaller/other-region as deviation |
| verify failure | step is **not** done (evidence over claims); one re-verify after grace; then deviation (proposed fix) or failure path |
| unknown | halt + honest partial; never guess |

Then the workflow's `on_failure` policy: `halt` (report applied/failed/not-attempted —
today's behavior, kept as default), `rollback` (§8), or `continue_independent`
(only branches with no path through the failed step; the join step then reports a
partial workflow honestly).

---

## 8. Rollback (saga semantics)

- **Pre-approved:** compensation compiles with the plan and appears in the approval
  artifact (§4.1). Executing it after a failure needs no fresh human — the human
  already approved "on failure, undo like this." A compensation *not* in the plan is a
  deviation (fresh approval).
- **Reverse completion order**, respecting dependency edges (detach before delete).
- Each compensation runs the same step lifecycle (plan → guard → apply → verify) in
  the same state workspace — `destroy_created` is a targeted `terraform destroy` of
  exactly the resources that step's state holds (per-resource state isolation is what
  makes this surgical — the existing design pays off here).
- Modes: `destroy_created` (fresh resources), `restore_previous` (day-2/modify — data
  captured in the step's pre-state), `rollout_undo` (K8s), `none` (explicitly
  irreversible — RDS snapshot deletion class; flagged loudly in the approval artifact:
  **"this workflow cannot be auto-rolled-back past step 4"**).
- **Compensation failure** = the one place the engine stops trying: freeze, page,
  present state honestly ("applied: s1,s2; failed: s3; rolled back: s2; rollback of s1
  FAILED: <cause>"). No second-order automation.
- Post-success rollback ("undo my last apply") = the same compensation chain surfaced
  as a new approval-gated run — the July gap analysis' missing "revert this change"
  affordance, powered by data the engine now always has.

---

## 9. Incident remediation pipeline

The SRE path, upgraded from single-pass to loop-armed but keeping its gates:

```
detect    Alertmanager webhook → incident run (source="alert"); dedupe by fingerprint
triage    INV loop (Agent_Harness §5.1) over the frozen read-only registry:
          PromQL signals → deployments/pods → world-model impact_of → hypothesis
          (today: one hardcoded list_deployments call — the loop is the upgrade)
decide    deterministic decision matrix (sre.py:36-48, kept — auditable, not vibes)
propose   remediation = catalog/day-2 keys only (rollback|scale_out|restart today)
          + evidence bundle + blast radius + verification plan
gate      blast_radius low + org policy → PRE_APPROVED (rate-limited, audited)
          else approval interrupt — approver sees evidence, not just a claim
execute   engine step with verify (rollout status, error-rate re-check after bake)
learn     postmortem draft artifact: timeline from run_steps + evidence + outcome;
          consolidation proposes durable facts ("service X flaps on deploy") as
          human-accepted memory proposals — never auto-written
```

Honesty rules kept from today's SRE: no kubeconfig → `proposed_not_executed`; the
remaining hardcoded policy row (`sre.py:146`) is replaced by a real predicate
(remediation key ∈ approved set for this org/env).

---

## 10. The named workflows, mapped

| Workflow | Shape under the engine |
|---|---|
| **Create VM** | 1 module step (`aws.ec2` / `azure.vm` / `gcp.vm`) + verify (describe + status checks) + compensation `destroy_created`. Today's single-resource path, unchanged in feel. |
| **Start/Stop/Restart VM** | 1 day-2 step (§6); low blast radius; verify = state read; compensation = inverse verb. |
| **Resize VM** | modify-mode module step; plan diff is the artifact; verify = instance_type read-back; compensation `restore_previous`. |
| **Create VPC + attach VM** | `s1: aws.vpc` → `s2: aws.ec2` with `wires: {vpc_id: "steps.s1.outputs.vpc_id", subnet_id: "steps.s1.outputs.public_subnet_ids[0]"}`; sequential wave; rollback = destroy s2 then s1. The wiring grammar already exists (`exec_loop.py:51-78`) — this is its flagship case. |
| **Create VM + attach S3** | wave 1: `s1: aws.ec2` ∥ `s2: aws.s3` (disjoint outputs, parallel); wave 2: `s3: aws.iam_attach` (new catalog template: instance-profile policy scoped to the bucket ARN) wired from both. Rollback reverse order: detach → destroy. |
| **Multi-resource Terraform (3-tier)** | VPC → {ALB ∥ RDS ∥ EC2-ASG} → DNS. One approval artifact with per-step plans + rollback chain. Raise `max_steps` from 5 → 8 behind config once parallel waves land. Prefer a composite catalog module when one exists (fewer moving parts beats a prettier DAG). |
| **Kubernetes deployment** | `k8s` steps from a chart/manifest catalog (pinned, reviewed — the K8s analog of TF templates): plan = **server-side dry-run diff** (the approval artifact's K8s equivalent of `terraform plan`), apply = manifest apply, verify = rollout status + readiness within deadline, compensation = `rollout_undo`. DevOps' existing dispatch→find→poll GitHub pattern feeds the image tag in. |
| **Incident remediation** | §9. |

---

## 11. Interfaces

```python
# app/engine/engine.py
class WorkflowEngine:
    async def execute(self, wf: Workflow, ctx: RunCtx) -> WorkflowOutcome:
        """Walk approved DAG by waves; per-step lifecycle §3.2; deviations raise
        NeedsApproval; cancel honored at boundaries; outcome always honest-partial-
        capable. Pure orchestration — zero cloud SDK imports in this module."""

# app/engine/executors/base.py
class Executor(Protocol):
    kind: str                                # "terraform" | "k8s" | "day2"
    async def plan(self, step: Step, ctx: StepCtx) -> PlanArtifact: ...
    async def apply(self, step: Step, plan: PlanArtifact, ctx: StepCtx) -> ApplyResult: ...
    async def verify(self, step: Step, ctx: StepCtx) -> EvidenceCard: ...
    async def compensate(self, step: Step, ctx: StepCtx) -> ApplyResult: ...
```

`TerraformExecutor` wraps today's `TerraformRunner` unchanged (state workspaces, saved
plan files, `-var`-only inputs, per-stage timeouts, rc-124 classification, never-fake
summaries). `K8sExecutor` and `Day2Executor` are new but small — the contract does the
heavy lifting.

---

## 12. Migration from `exec_loop.py`

| Keep verbatim (engine invariants) | Generalize | New |
|---|---|---|
| catalog-only `validate_dag` refusal | sequential walk → wave scheduler | preconditions |
| wiring grammar + no-guess `KeyError` | `_replan_step=None` → deviation-proposal taxonomy (§7) | VerifyPlan/EvidenceCard everywhere (verify exists AWS-only today) |
| one-interrupt DAG approval | interrupt payload → full approval artifact (§4.1) | compensation compile + saga |
| deviation re-approval w/ was/now diff | `MAX_STEPS` constant → config | day-2 registry + executor |
| per-step idempotency claim/wait/abort | | change windows + `scheduled` status |
| cancel at boundaries, never mid-apply | | lock plan + resource serialization |
| `_partial_outcome` honest reporting | | postmortem artifact |

Sequencing and the LangGraph/Temporal decision gate live in
`Implementation_Roadmap.md` — short version: the engine ships **inside** the current
LangGraph node structure first (`execute` node calls `WorkflowEngine`), so checkpoint/
interrupt/reconciler machinery is reused, and a later Temporal adoption (if workflows
outgrow PG checkpoints: hours-long, high fan-out, versioned mid-flight migrations)
swaps the engine's substrate without touching the step contract, the catalog, or any
agent.
