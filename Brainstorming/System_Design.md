# System Design — Flows, Multi-Agent Model, Scaling, Failure & Security

> How the proposed architecture behaves at runtime. Mermaid versions of the key flows
> are in `Architecture_Diagrams.md`; this document is the prose + decision record.

---

## 1. End-to-end flows

### 1.1 Chat provisioning (single resource) — the bread-and-butter path

```
1  user: "create a t3.micro in mumbai for the billing service"
2  POST /chat → prepare_run: OIDC→tenancy(strict)→RBAC(can_initiate)→limits
   → RoutePlan resolved per purpose set, pinned on the run → Run(running)
3  router agent (purpose=router, fast tier): classify → {domain, action, resource,
   cloud, confidence} via structured output (native, not prompt-and-parse)
4  cloudops param path (purpose=cloudops.extract): template select (catalog),
   extract+validate params (Pydantic), missing → params card + pending record (stop)
5  discovery: read-only SDK availability + duplicate-name + S3-global-name prechecks
6  compile: single-step Workflow; plan in per-resource TF workspace; plan_guard;
   policy predicates over plan JSON; cost; impact_of(target scope)
7  approval artifact → durable interrupt → run(awaiting_approval); notify approvers
   (web + linked channels, org-scoped, four-eyes-aware)
8  human approves (web or Telegram button → click-time re-check) → resume from
   checkpoint on any worker; continuation tails the event stream from now
9  engine: claim idempotency → apply (console streamed) → verify → EvidenceCard
   → inventory + world model + run_steps (same txn) → ledger rows all along
10 finalize → ServiceNow update → notify → done{outcome, traceId, runId}
```

Latency budget (why each stage is where it is): 3-4 are fast-tier LLM calls
(~sub-second each); 5-6 dominated by `terraform init/plan` (warm-init skip + plugin
cache exist; target keeps the pre-approval segment under ~15s warm); 9 is minutes and
lives entirely post-approval where waiting is legitimate.

### 1.2 Multi-resource with parallel waves (VM + S3 + wiring)

Divergence from 1.1: planner (flagship tier, high reasoning) drafts a GoalDAG; critic
annotates; `compile_goal_dag` validates catalog/wiring/guard/compensation closure and
computes the lock plan; **one** approval artifact covers steps+policies+cost+impact+
verify+rollback; engine runs wave 1 = {ec2, s3} concurrently (disjoint outputs, both
idempotency-claimed), wave 2 = iam_attach wired from both; per-step verify; failure in
wave 2 with `on_failure=rollback` compensates in reverse (detach nothing — attach
failed — destroy s3, destroy ec2), each compensation itself planned/guarded/verified.

### 1.3 Approval via Telegram (governance over a chat channel)

```
interrupt → notify.approval_pending → linked, can_approve, org-scoped, four-eyes-
aware recipients get card (shape only: workflow, mode, +a ~c -d, policy fails, NO
diff/params) + [Approve][Reject] buttons carrying opaque untrusted tokens
press → resolve identity (unbound → how-to-link, nothing leaked) → RBAC at click
time → resolve_approval_core re-runs org/four-eyes/state/in-flight-lock → decision
→ card re-rendered buttonless (no double-press) → continuation streams into the
channel through the same preview ladder → deep link to the full run
```

Design rule that generalizes to Slack/Teams: **the channel proves who clicked;
the core decides whether that click counts.** Transports never gain authority.

### 1.4 Incident → remediation

Alertmanager webhook → incident run → INV loop triage (bounded: iterations≤6,
calls≤8, cost-capped; every hop an Evidence row) → deterministic decision matrix →
remediation proposal from catalog/day-2 keys + evidence bundle → gate by blast
radius (pre-approved tier: rate-limited auto-exec for low-radius org-listed actions;
else interrupt) → engine executes with verify + bake-time re-check of the alert
signal → postmortem draft artifact → consolidation proposes durable facts as
human-accepted memory proposals.

### 1.5 Org admin switches the planner model

Settings→Models (RBAC: platform_admin) → live catalog + health per provider → bind
`planner → claude-sonnet-5` → staged: offline eval smoke for that purpose runs →
green → `eval_state=passed`, binding live for **new** runs (in-flight runs keep their
pinned RoutePlan) → audit row (who/when/why/before/after) → every subsequent answer
badges `served_by`. Red → stays staged, failing cases shown. `waived` exists for
break-glass, requires a reason, and is loud.

### 1.6 Crash mid-workflow

Worker dies between wave 1 and 2 → heartbeat expires (45s TTL) → reconciler sweep
(60s) finds executing run with dead heartbeat → checkpoint says resumable → redrive
on any worker → engine recomputes readiness from `run_steps`: wave-1 steps hold
stored idempotency results (no re-apply), wave 2 proceeds → UI stream reattaches via
`Last-Event-ID` on the Redis stream. Not resumable → honest `failed` with partial
outcome. (All existing machinery; the engine only widened what "resumable position"
means.)

### 1.7 Budget breach

Ledger check at iteration/step boundary trips → `BudgetExceeded` → kernel/engine exit
through the honest-partial path ("steps 1-2 applied; investigation stopped at
$4.90/$5.00 budget; not attempted: …") → run `failed(budget)` with resume-after-
raise affordance. Never mid-apply; a running `terraform apply` always completes or
fails on its own terms.

---

## 2. Multi-agent model (bounded on purpose)

| Agent | Purpose binding | Tools policy | Writes |
|---|---|---|---|
| Router | `router` (fast) | none | classification state |
| Planner | `planner` (flagship, high reasoning) | GOVERNED_PROPOSE | GoalDAG **draft** |
| Critic (optional pass) | `judge` | none (reads draft + world model summary) | advisory notes on the artifact |
| Investigator | `inv_loop` | READ_ONLY_FROZEN | Evidence rows |
| SRE triage | `sre.triage` | READ_ONLY_FROZEN | signals, hypothesis |
| Knowledge/General | `knowledge`/`general` (user-pinnable) | RAG read | answers |
| Judge (offline) | `judge` | none | eval verdicts in CI |

Coordination rules:

1. **Handoffs are typed state, not chat.** Agents communicate through the run state
   (LangGraph channels today) — the blackboard pattern; no agent parses another's prose.
2. **Fan-out only below the boundary of mutation.** Parallel investigators on
   independent hypotheses share one budget pool (spawn semantics); the planner and the
   engine are strictly singular per run.
3. **The orchestrator is the graph, not a model.** Routing between agents is code
   (conditional edges/waku's routers-are-code rule). An LLM classifies; code routes.
4. Depth cap 1 for subagents; results return as typed `AgentResult` summaries —
   context isolation without transcript bleed.

This is deliberately not a free-form agent society: CloudOps' failure mode is not
"too little autonomy," it is "an unauditable decision touched infrastructure."

---

## 3. Scaling model

| Concern | Mechanism | Today | Target delta |
|---|---|---|---|
| API horizontal scale | stateless workers; Redis Streams events; PG checkpoints | proven with api + api-b | flip `aegisops_event_bus` default to `redis` |
| Stream reattach | monotonic ids + `Last-Event-ID` + continuation cursor | exists | unchanged |
| Run concurrency | per-org 5 / per-user 2 from heartbeat liveness | exists | + per-org engine-step concurrency + lock waits visible |
| Long executors | supervisor-tracked tasks + heartbeats | exists | optional dedicated executor pool consuming a step queue (Executor protocol is the seam) |
| Mutual exclusion | TF state workspaces (data plane) | exists | + Redis lock plan (control plane) so conflicts queue instead of erroring |
| LLM throughput | per-provider breaker + fallback chains; purpose tiers push bulk work to fast models | none | provider layer |
| DB growth | run/message retention sweeps | exists | ledger is append-only + partitioned by month |

Capacity honesty: the binding constraint on user-perceived latency is `terraform
init/plan`, not the LLM. Keep optimizing the TF path (plugin cache, warm init — both
exist) before micro-optimizing prompts.

---

## 4. Failure matrix (component → behavior)

| Failure | Behavior (design intent) |
|---|---|
| LLM provider degraded | retries w/ jitter → breaker opens → fallback hop (visible event; badge) — except planner/judge, which pause with the classified card |
| All providers down | run fails honestly pre-mutation; approvals/resumes unaffected (no LLM in that path); read APIs unaffected |
| Redis down | new runs refused at admission (event bus + locks unavailable = not safe); in-flight TF applies complete, results land in PG; reconciler backfills stream-less consumers. Redis is availability-critical, not integrity-critical |
| Postgres down | full stop, loudly — it is the system of record; no degraded-write mode, ever |
| Worker killed | §1.6 — heartbeat → reconciler → redrive or honest fail |
| Terraform hang | per-stage timeout, rc-124 process-group kill + classification (exists); step fails with observation; retry/deviate per policy |
| K8s API down mid-rollout | verify deadline expires → step not-done → deviation/failure path; `rollout_undo` compensation available |
| Langfuse down | tracing degrades silently (best-effort contract); **ledger keeps recording** — that is why the ledger exists |
| Keycloak down | no new authenticated actions; running non-interactive work completes; approvals wait (interrupt is durable — it can wait hours) |
| Neo4j down | impact section on artifacts says "impact graph unavailable" (never a fake pass — existing destroy-gate rule extended) |
| Eval service red | bindings stay staged; runtime unaffected |

---

## 5. Security enforcement points (defense in depth, audited locations)

| Layer | Check | Where |
|---|---|---|
| Admission | OIDC → strict tenancy (refusal on org-less) → `can_initiate` → limits | `prepare_run` (single point) |
| Reads | org-scoped 404s | `authorize_run`/`authorize_session` |
| Approval | route RBAC **and** core re-check **and** four-eyes **and** state+lock | `resolve_approval_core` (route-independent — gateways get it for free) |
| Mutation choke | recorded approver's `can_execute` re-asserted; plan_guard re-run | `execute` node / engine step lifecycle |
| Gateway | identity re-resolved every message/click; store-down = not authenticated; outbound redact/withhold/truncate | `gateways/identity.py`, `render.py` |
| Provider layer | keys only in adapters; no payload logging pre-redaction; org residency routing; binding changes RBAC'd+audited+eval-gated | `app/llm/*` |
| Day-2 verbs | registry-only SDK writes in one auditable file; approval-tiered by blast radius | `engine/executors/day2.py` |
| Confidentiality | per-message classifier + badge; High withheld on channels; thinking never persisted raw | existing + kernel rule |
| Audit | approvals (immutable rows) + bindings + identity link/unlink + tool middleware audit stage | existing + harness middleware |

Governance-drift guard (new, from audit finding D9): security-relevant flags
(`four_eyes`, `tenancy`, `exec_loop`, `event_bus`) are stamped onto every approval
artifact and the `/healthz` payload — a weakened posture is visible on the card the
approver signs, never silent in an `.env`.

---

## 6. Design patterns — used and rejected

**Used (named, so reviews can point at them):**
- **Ports & adapters** — the provider layer and Executor protocol.
- **Strangler fig** — `agents/llm.py` shim → `app/llm`; `exec_loop` → engine.
- **Registry** — templates (exists), day-2 verbs, tools, models, prompts.
- **Saga with pre-approved compensation** — the rollback model.
- **Event sourcing (lite)** — run truth = `run_steps` + event stream projections;
  UI tabs are read models (CQRS-lite).
- **Circuit breaker / bulkhead** — per provider-model; budgets and locks as bulkheads.
- **Blackboard** — typed run state between agents.
- **Policy-as-code** — plan_guard + template predicates + capability/needs checks.
- **Supervisor + reconciler** — heartbeat liveness, sweep-and-redrive (exists; the
  pattern extends to scheduled windows and executor pools).
- **Progressive disclosure** — context recipes and gateway cards (shape first, deep
  link for detail).

**Rejected (recorded so they aren't relitigated by accident):**
- Choreographed event-driven microservices (no owner of a run's truth).
- Unbounded ReAct autonomy on mutation paths.
- Prompt-embedded governance ("please don't destroy prod") — governance is code.
- Dynamic tool synthesis / LLM-written tools.
- Multi-model consensus voting for approvals (humans approve; models propose).

---

## 7. What "done" looks like (system-level acceptance)

1. Switch `planner` org-binding Gemini→Claude in the UI; next run plans on Claude;
   zero code changed; eval gate green; every message badges the serving model.
2. Kill -9 a worker mid-3-step workflow; run resumes on the second worker; no
   double-apply; UI stream reattaches; ledger shows the whole story.
3. VPC+VM+S3 lands with one approval; forced failure at step 3 rolls back 2 and 1
   in order, each compensation verified; final card reads like a truthful incident
   report.
4. Anthropic outage drill: knowledge answers arrive via fallback with visible badge;
   planner runs pause with the classified card; nothing silent.
5. An org with $50/day budget stops cleanly at $50 with honest partials.
6. A Telegram click by a non-approver is refused at click time with the exact
   refusal the web would give; the card in-channel never exposes the diff.
