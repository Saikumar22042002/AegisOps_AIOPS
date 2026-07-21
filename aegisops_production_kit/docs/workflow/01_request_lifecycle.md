# 01 · Request Lifecycle

The complete journey of one chat message, function by function. The backbone is the same for
every path; the branch happens at the router and again at `_after_plan`.

## Backbone call chain (function → function, in order)

```
frontend/lib/store.ts:sendText
  └─ streamSSE("/chat", {sessionId, message, model, context})        store.ts:305
       │  POST, credentials:include, Accept: text/event-stream       lib/sse.ts:29
       ▼
api/chat.py:chat                        (POST /chat handler)          chat.py:211
  ├─ Depends(require_initiator)          RBAC: read-only → 403         security/deps.py:152
  ├─ get_provider(settings, body.model)  U3: unknown model → 400       chat.py:220
  ├─ repo.org_for(s, user)               S0 tenancy → org_id           chat.py:224
  ├─ _active_run_counts(org_id, user)    PR-2a admission → 429         chat.py:230 / :79
  ├─ Session (reuse or create)           org-scoped                    chat.py:241-255
  ├─ Message(role="user") + Run(status="running", initiated_by, env)  chat.py:256-264
  ├─ _memory.embed_message(...)          M2 embed-on-write (bg)        chat.py:268
  ├─ create_channel(run_id)              SSE channel                   chat.py:271 / events.py
  └─ get_supervisor().run(run_id, _drive)  B2 tracked task+heartbeat   chat.py:332
       │  returns EventSourceResponse(_sse(channel))                   chat.py:333
       ▼
   _drive()  (background, supervised)                                  chat.py:280
     ├─ emitter.run({runId, sessionId})   first SSE frame → UI binds   chat.py:289
     ├─ run_graph(run_id, channel, initial=initial)                    agents/runner.py:36
     │    ├─ lf.begin_run(run_id, ...)     Langfuse trace = run_id     runner.py:45
     │    └─ graph.ainvoke(initial, config)   the LangGraph state machine
     │         router → {plan|knowledge|general} → [approval] → execute → verify → finalize
     │         → servicenow_update → notify → END          agents/graph.py:96-108
     ├─ status_ = failed | awaiting_approval | cancelled | completed   chat.py:296-300
     ├─ _persist_result(run_id, session_id, org_id, state, status_)    chat.py:305 / :160
     │    └─ Run row updated + Message(role="assistant") + embed(bg)   chat.py:172-202
     └─ emitter.done({messageId, runId, outcome})  (unless interrupted) chat.py:308-314
```

The graph itself is documented node-by-node in [02_langgraph.md](02_langgraph.md). Auth/RBAC/
tenancy detail is in the guards: `require_initiator` (`deps.py:152`), `require_approver`
(`deps.py:143`), org resolution (`deps.py:90-106`), run authorization on reads
(`deps.py:authorize_run:116`).

## Streaming setup

`create_channel(run_id)` (`events.py`) returns a `RunChannel` (memory) or `RedisChannel`
(when `aegisops_event_bus == redis`). `_sse` (`chat.py:62`) replays the channel history after a
cursor then drains the live queue, de-duping by event id (`chat.py:66-76`) — so the leading
`run` frame is delivered exactly once. The SSE vocabulary is: `run · step · token · analysis ·
params · reference · confidentiality · console · interrupt · error · done` (frontend reducer at
`store.ts:308-363`, see [04_frontend.md](04_frontend.md)).

## Path A — Knowledge answer (read-only, no approval)

```mermaid
sequenceDiagram
    participant UI
    participant chat as api/chat.py
    participant router as router.py:61
    participant kn as knowledge.py:27
    participant rag as rag/retriever.py
    UI->>chat: POST /chat "how do I rotate an RDS password?"
    chat->>router: graph START → router
    router->>router: llm.classify_json → domain=knowledge (chat LLM)
    router-->>chat: emit step "Routed → knowledge"
    chat->>kn: _after_router → knowledge  (graph.py:97)
    kn->>rag: retriever.retrieve(org, query, k=5)  (knowledge.py:36)
    kn-->>UI: emit reference × N  (knowledge.py:39)
    kn->>kn: llm.stream_answer(context+question)  (knowledge.py:53)
    kn-->>UI: token stream + analysis card
    Note over chat: _after_plan not on this edge; knowledge → finalize (graph.py:101)
    chat-->>UI: done
```

Knowledge and general both edge straight to `finalize` (`graph.py:101-102`) — no approval, no
execute. General (`general.py:26`) additionally short-circuits **positional recall** before any
LLM call (`general.py:48`), see [05_reads.md](05_reads.md).

## Path B — CloudOps create (the full governed path)

This is the spine of the platform: params → DEP closure → plan → policy → **approval
interrupt** → apply → verify → success card.

```mermaid
sequenceDiagram
    participant UI
    participant chat as api/chat.py
    participant cp as cloudops_plan (cloudops.py:470)
    participant dep as dependency.py
    participant tf as TerraformRunner (tools/terraform.py)
    participant ap as approval.py:35
    participant xe as cloudops_execute (cloudops.py:1409)
    participant vf as verify (finalize.py:64)

    UI->>chat: POST /chat "create an ec2 in aws"
    chat->>cp: router→cloudops_plan (action=create)
    cp->>cp: _extract_inputs (Gemini + freeform)  (cloudops.py:502)
    cp->>cp: resolve_cloud → aws | ask if ambiguous  (cloudops.py:515)
    cp->>dep: params.missing_required minus slot_fields  (cloudops.py:511)
    Note over cp,dep: DEP-covered ids (vpc_id/subnet_id) never asked as raw ids (P2-4)
    cp->>dep: dependency.resolve_closure(...)  (cloudops.py, pre-validation)
    alt closure.status == ask
        cp-->>UI: token "Which vpc…?" + params card  (collecting=true)
    else closure.status == dag
        cp->>cp: exec_loop.plan_goal_dag OR text proposal  (see Path F)
    else closure.status == complete
        cp->>cp: template.schema(**collected).model_dump()  (validate)
        cp->>tf: runner.init → plan → show_plan  (cloudops.py:737-739)
        cp->>cp: plan_guard.check_plan_actions("create", diff)
        cp->>cp: template.policy_fn(validated, planned_resources)
        cp-->>UI: interrupt payload (Terraform Plan card) — approval_status=pending
    end
    Note over chat: run persisted awaiting_approval; SSE ends at interrupt
    UI->>chat: POST /approvals/{id} {decision:"approved"}  (resolve_approval chat.py:349)
    chat->>ap: graph resumes at approval interrupt (Command resume)
    ap->>ap: plan_guard re-assert (A2) + record Approval row  (approval.py:44,74)
    ap->>xe: approval_decision → execute (graph.py:103)
    xe->>xe: S5 capability assert (execute.py:23)
    xe->>tf: idempotency.claim → runner.apply  (cloudops.py:1431,1463)
    xe-->>UI: console stream (terraform apply …)
    xe->>vf: execute→verify (graph.py:104)
    vf->>tf: read-only SDK reconcile (bounded 30s)  (finalize.py:74)
    vf-->>UI: success card (cards.success_card)  (finalize.py:89)
    chat-->>UI: done {outcome, sensitive_outputs}
```

Key files/lines on this path: params collection & the P2-4 slot exclusion
(`cloudops.py:511`), DEP closure before validation (see [06_catalog.md](06_catalog.md) for the
slots), plan/guard/policy (`cloudops.py:737-739`, `plan_guard.py`, `template.policy_fn`),
interrupt (`approval.py:58`), capability gate (`execute.py:23`), idempotent apply
(`cloudops.py:1431` — A1 wait-or-abort), bounded verify (`finalize.py:64-105`).

## Path C — Day-2 read ("what's the VPC of web-01?")

`cloudops_plan` routes a non-broad read to `_read_resource` before any cloud resolution
(`cloudops.py:484`): resolve against Postgres inventory (`inventory.resolve`), live-reconcile
the match (`inventory.reconcile` — boto3 describe, offloaded), render VPC/subnet/IPs from the
reconciled attributes. Fully deterministic, no chat LLM. Broad reads ("did I create anything?")
fall to `_read_path` (`cloudops.py:925`) — per-cloud discovery merged with the inventory table.
Full trace in [05_reads.md](05_reads.md).

## Path D — Day-2 modify

`cloudops_plan` with `action == "modify"` → `_comp_intercept` first (honest handling of
compound / attach / OS-change asks, `cloudops.py:489`), then `_modify_resource`
(`cloudops.py:1280`). Only the resource types in `_MODIFY_CAPS` (`cloudops.py:124-130`) are
modifiable — `aws.ec2` {ingress_ports, power, tags}, `gcp.vm` {ingress_ports, power}, `azure.vm`
{ingress_ports}, `aws.s3` {versioning, lifecycle_expire_days, tags}, `aws.rds` {instance_class,
allocated_storage, tags}. A modify still runs the same gate: plan → plan_guard (in-place check)
→ policy → approval interrupt → apply, in the resource's own state workspace. Power state is
Terraform-encoded (`power_state` var), never an SDK call; Azure power returns an honest "use the
portal" answer (`cloudops.py:132-133`).

## Path E — Destroy

`action == "destroy"` → `_destroy_resource` (`cloudops.py:1122`). It resolves the target from
inventory (never collects create-params), runs a **world-model impact check** (`world_model.
impact_of` via `_world_model_impact_check`, `cloudops.py:1098`) that names active dependents and
blocks or warns, a destroy-only plan guard, and the approval interrupt, then tears down that
resource's own state workspace. `"undo that"` (`router.py:96`, `intent_guard.is_undo`) is a
deterministic destroy of `__last_applied__` — no LLM, full gate.

## Path F — Goal-DAG (U6, `aegisops_exec_loop == on`)

When the DEP closure returns a create-first DAG (e.g. "an EC2 inside a new VPC"), `plan_goal_dag`
(`exec_loop.py:100`) terraform-plans every step whose inputs are concrete, lists the whole DAG
on ONE approval card, and returns `workflow="governed-exec-loop"`. On approve, `execute`
dispatches to `execute_goal_dag` (`execute.py:32`, `exec_loop.py:267`) which runs each step
(`execute_governed_step`, `exec_loop.py:198`) in order — plan → guard → policy → apply — wiring
each step's real outputs into the next (`resolve_wires`, `exec_loop.py:51`). Bounds:
`MAX_STEPS=5`, `MAX_REPLANS_PER_STEP=1` (`exec_loop.py:36-37`). A replan is a **deviation** →
fresh approval interrupt (`exec_loop.py:301`). Cancel is honored only at the step boundary,
never mid-apply (`exec_loop.py:279`). With the flag off, the DAG is proposed as text
(`cloudops.py`, the `closure.status == "dag"` branch).

## Path G — Retry / Cancel

- **Retry-with-fix (U7):** a classified provider failure attaches `retry={label, retry_message}`
  to the `error` SSE event (`cloudops.py:1483`, `provider_errors.suggest_retry`); the UI renders
  a button that re-sends the corrected message as a genuine new turn (`store.ts:351`, `Workspace.
  tsx` retry button).
- **Cancel (PR-3):** `POST /runs/{id}/cancel` (`chat.py:472`) — authz initiator-or-approver,
  org-scoped. Pre-approval → the supervised drive is cancelled, `_mark_cancelled` writes terminal
  `cancelled` (`chat.py:320`, "nothing was changed"). Mid-DAG → halt-after-current-step
  (`exec_loop.py:279`). `cancelled` is a first-class terminal status everywhere.

## Terminal-state guarantee (B5)

Every run reaches a terminal state. `_drive`'s `except` calls `_force_terminal`
(`chat.py:322-324`); its `finally` closes the channel (`chat.py:330`); a pre-approval cancel is
caught and marked terminal (`chat.py:315-321`); and if a worker dies entirely, the reconciler's
stranded-run sweep re-drives or force-fails it (`reconciler.py`, see [03_harness.md](03_harness.md)).
`_persist_result` cleans plan files and releases the cancel flag on terminal states
(`chat.py:205-207`).
