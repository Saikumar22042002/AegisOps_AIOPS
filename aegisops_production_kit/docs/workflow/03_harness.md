# 03 · Agent Harness — the four pillars

Memory, Tools, Tracing/Ops, and Evals — mapped to what the code does, with the honest
built-vs-wired distinctions where they differ from the specs.

---

## Pillar 1 — MEMORY

The memory path is **deterministic and DB-backed — no chat LLM in the memory path itself, so
recall cannot hallucinate** (`agents/memory.py:12`). Turns live in the `messages` table
(`db/models.py:86`) with an optional pgvector `embedding` column (`db/models.py:102`). Written
on `/chat`: user turn `chat.py:256`, assistant turn `chat.py:188`, each background-embedded
after the row exists (`chat.py:268`, `chat.py:202`).

### Layer map (standard taxonomy → our implementation)

| Layer | Implementation | Written | Read | LLM-free? |
|---|---|---|---|---|
| **Short-term** (transcript window / budget) | `memory.build_context` (`memory.py:261`) assembling `build_transcript` (`memory.py:196`) | — (reads `messages`) | router (`router.py:114`), general (`general.py:61`), knowledge (`knowledge.py:45`) | Yes (assembly); the LLM only consumes the slice |
| **Episodic — positional** | `detect_recall` (`memory.py:91`) + `get_turn` (`memory.py:77`) | — | `general.py:48` short-circuit | **Yes — verbatim, survives LLM outage** |
| **Episodic — semantic** | `retrieve` (`memory.py:112`) pgvector cosine, pg_trgm keyword fallback | `embed_message` (`memory.py:172`) | `build_context` retrieval slot | query embed uses the Gemini *embedding* model; keyword fallback fully LLM-free |
| **Standing / long-term** | `user_memory.py` — `set_memory` (`:32`), `lookup` (`:70`), `render_block` (`:81`) | `PUT /memory` (`api/modules.py:332`) | leads every `build_context` (`memory.py:276`) | Yes |
| **Working / offloaded** | `plan_ref_line` (`memory.py:152`), `fetch_plan` (`memory.py:160`) | — | — | Yes |

### Budget slicing

`build_context` resolves a per-purpose char budget (`memory.py:272`, `≈4×tokens`) from
`_PURPOSE_BUDGET` (`memory.py:257`: router 1600, general 8000, knowledge 4000, cloudops/devops/
sre 3000, loop 4000) and assembles: standing block → recall/related slot → transcript
(`memory.py:291-298`). `build_transcript` (`memory.py:196`) renders verbatim when it fits, else
newest turns at ~70% budget (each clipped to 600 chars) plus a 160-char digest of every older
user turn so early facts survive (`memory.py:218-239`).

### Retrieval path: "what did I say in turn 20?"

1. `/chat` persists + background-embeds the user turn.
2. Router classifies domain via the chat LLM (`router.py:120`); on LLM outage it falls back to
   `general` (`router.py:106`).
3. `general` **short-circuits before any LLM call** (`general.py:48`): `detect_recall("turn 20")`
   → `(20, "user")` via `_RECALL_RE` (`memory.py:35`, the numeric noun-first shape at
   `memory.py:43`); `get_turn(session_id, 20, role="user")` (`memory.py:77`) reads `messages` via
   `load_history` (`memory.py:48`) and returns the 20th user turn **verbatim**. Answer at
   `general.py:52-57`. LLM-free — cannot hallucinate a different turn.
4. In parallel, `build_context` injects the same turn as an `[Exact recall]` slot
   (`memory.py:279-285`) for messages not caught by the short-circuit.

### "my usual region" (standing memory, deterministic)

`_extract_inputs` (`cloudops.py:169`) runs a regex for "usual region/location" **before** the
Gemini extraction (`cloudops.py:178`) and resolves it via `user_memory.lookup(org_id, user_id,
"usual_region")`; `setdefault` (`cloudops.py:183`) means an explicit region in the message still
wins. Azure maps to `location`, others to `region`.

### Built-vs-wired honesty (matters for accuracy)

- `build_context` is threaded by **only** router/general/knowledge. The `cloudops/devops/sre/
  loop` purpose budgets exist but no caller passes them — the "context into EVERY LLM call"
  spec claim is broader than the built call-graph.
- **M5 offloading** (`plan_ref_line`/`fetch_plan`) is implemented and unit-tested but has **no
  caller in `app/`** — not consumed by any agent flow today.
- `classification_context` (`memory.py:301`) is superseded by `build_context` and has no caller.

Source-of-truth stores (Postgres inventory, Neo4j world model) are documented in
[05_reads.md](05_reads.md).

---

## Pillar 2 — TOOLS

### The mutation boundary (Invariant 2)

Cloud SDK clients are **read-only** — discovery/verify/telemetry only. Docstrings say so
verbatim: `tools/aws.py:1` ("Never provisions"), `tools/gcp.py:1`, `tools/azure.py:1`. Every SDK
call in those files is a `describe_*`/`list_*`/`get_*`/`head_*`; no `create/put/delete/run_*`
verb appears. Each offloads the blocking call off the event loop via `anyio.to_thread`
(`aws.py:47`, `gcp.py:37`, `azure.py:56`) and gates on `.enabled` (creds present).

| Reader | `get_*` | `.enabled` | `.ping()` | read calls |
|---|---|---|---|---|
| AWS | `aws.py:133` | `aws.py:30` (STS keys) | `sts.get_caller_identity` (`aws.py:124`) | `describe_vpcs/subnets/instances`, `eks.list/describe`, `rds.describe`, `s3.list/head_bucket` |
| GCP | `gcp.py:75` | `gcp.py:30` (project + SA key) | `get_project` (`gcp.py:65`) | `compute NetworksClient.list`, `InstancesClient.list/aggregated_list` |
| Azure | `azure.py:85` | `azure.py:36` (SP + sub) | `resource_groups.list` (`azure.py:75`) | `resource_groups.list`, `virtual_networks.list_all`, `virtual_machines.list_all` |

**The only mutation tool is Terraform.** `tools/terraform.py:TerraformRunner`.

### Terraform runner lifecycle (`tools/terraform.py`)

`init → plan → show_plan → apply | destroy`, each stage a `CommandConsole.run` subprocess.
- **`_env`** (`terraform.py:155`) sets `TF_IN_AUTOMATION`, injects cloud creds, and (STAB P0-1)
  sets `TF_PLUGIN_CACHE_DIR` (shared provider cache) + `TF_DATA_ROOT`-derived `TF_DATA_DIR` so
  `.terraform` lives on the native volume, not the 9p/OneDrive bind mount. A `plugin_cache=False`
  escape hatch (`terraform.py:_env`) retries init without the cache if the cache itself is the
  failure.
- **Warm-init skip** (`_is_initialized`, `terraform.py:224`): skip the full `init` when
  `.terraform` + lockfile are present (`aegisops_tf_skip_init_when_ready`), falling back to a
  full init on any mismatch.
- **Per-resource state isolation (N-08):** every create/modify/destroy runs in its own
  `terraform workspace` (`state_slug(name)`, threaded as `state_workspace`) so an op can never
  reconcile-and-destroy a sibling resource.
- **Remote backend (A3):** `_backend_config_args` (`terraform.py:208`) emits S3+DynamoDB
  `-backend-config` when `aegisops_tf_backend == remote`; empty in local mode.
- **Subprocess safety (PR-2b):** `CommandConsole.run` (`tools/console.py:41`) runs with
  `stdin=DEVNULL` (`console.py:53`, non-interactive), its own process group
  (`start_new_session=True`, `console.py:61`), a per-stage timeout, and a SIGTERM→10s→SIGKILL
  process-group kill on timeout (`console.py:97-117`, rc 124). Output lines are redacted
  (`console.py:71`).

### Read-only investigation registry (INV)

`agents/investigation.py` — the SRE agent's evidence-gathering surface, structurally unable to
mutate.
- `MAX_CALLS = 8` budget (`investigation.py:28`).
- Registration rejects any tool whose name contains a mutation marker
  (`apply/create/delete/destroy/patch/scale/restart/rollback/write/set_/update/push/dispatch/…`,
  `investigation.py:33-35`) via `assert_read_only` (`:49`) called from `register` (`:65`).
- The registry `freeze()`s after build (`:72`) — a running investigation cannot widen its
  surface. `spawn()` (`:125`) shares the same frozen registry AND the same call budget, so a
  sub-agent is never wider (`:102,127`).
- `default_registry` (`:131`) holds exactly 5 reads: `query_prometheus`, `list_deployments`,
  `list_pods`, `list_inventory`, `query_impact`.

---

## Pillar 3 — TRACING / OPS

### Langfuse trace tree (`integrations/langfuse_client.py`)

**trace_id == run_id** (`langfuse_client.py:109,114`) so a resumed run lands on the same trace.
Tree shape:

```
trace  chat-request / <domain>-run              begin_run (langfuse_client.py:105)
  ├─ router                 graph-node span      step_started/ended (via timing.py)
  │    └─ gemini.generate   generation           generation() (langfuse_client.py:184)
  ├─ cloudops_agent → planner   sub-step spans
  │    └─ terraform.plan    tool span            tool() ctx mgr (langfuse_client.py:217)
  ├─ approval               one span ACROSS the human interrupt (real wait)
  └─ execute → verify → finalize → servicenow → notify
```

- Graph-step spans use **deterministic ids** `<run_id>:<name>` (`langfuse_client.py:75`) so the
  span opened before the interrupt is closed by the resume in a different task/process
  (`step_ended`, `langfuse_client.py:167`). Tool spans use a random uuid (`langfuse_client.py:229`).
- Failures are recorded **on** the span (`level="ERROR"`, `langfuse_client.py:169`) and, for
  `tool()`, re-raised never swallowed (`langfuse_client.py:242-243`).
- Everything degrades to a no-op when Langfuse is unconfigured (`enabled` requires both keys,
  `langfuse_client.py:89-91`); every payload passes through redaction (`_clean`,
  `langfuse_client.py:62`).
- `assert_project` (`langfuse_client.py:286`) warns loudly at startup if the keys don't belong
  to the expected project (O2); `langfuse_browser_base` (`:334`) resolves the browser-facing
  origin for deep-links (STAB P2-1).

### timing.py → run_steps → the Traces tab

`start_step`/`end_step` (`agents/timing.py:61` / `:94`) upsert a `run_steps` row keyed by
`(run_id, name)` — preserving the original `started_at` across a resume re-entry
(`timing.py:70-72`) — then drive the Langfuse span AND observe `AGENT_STEP_DURATION`
(`timing.py:107`). The `GET /runs/{id}/traces` endpoint (`api/artifacts.py:266`) builds the
in-app trace tree from those `run_steps` rows (`_trace_spans`, `api/artifacts.py:220`) with real
durations, plus a Langfuse deep-link.

### Prometheus metrics (`metrics.py`)

Dedicated registry (`metrics.py:12`), exposed at `GET /metrics`. Key metrics:

| Metric | Type | Where observed |
|---|---|---|
| `aegisops_api_requests_total` / `_request_duration_seconds` | Counter/Histogram | correlation middleware (`main.py:120,126`) |
| `aegisops_agent_runs_total` (domain,workflow,status,env) | Counter | `router.py:134`, `chat.py:306` |
| `aegisops_agent_step_duration_seconds` (agent,step) | Histogram | `timing.py:107` |
| `aegisops_approval_wait_seconds` (domain,decision) | Histogram | `chat.py:_record_approval_wait:336` |
| `aegisops_stranded_runs` | Gauge | reconciler sweep (`reconciler.py:222`) |
| `aegisops_reconciler_sweep_failures_total` | Counter | `reconciler.py:231` |
| `aegisops_drift_findings` (kind) | Gauge | reconciler drift sweep (`reconciler.py:228`) |
| `aegisops_dependency_up` (dependency) | Gauge | health checks |

### Supervisor + heartbeats (`agents/supervisor.py`)

`RunSupervisor.run(run_id, drive)` (`supervisor.py:88`) replaces the old fire-and-forget task:
it starts a tracked drive task + a heartbeat task writing `run:<id>:hb` with a 45s TTL, refreshed
every 15s (`supervisor.py:24-25,109,112`). `is_live` (`:72`) = "executing in THIS worker now".
`drain()` (`:129`) on shutdown cancels every live drive and persists it `failed`
(`_force_terminal`, `:150`). Cooperative cancel (PR-3): `request_cancel` (`:39`) sets
`run:<id>:cancel`; `signal_cancel` cancels the live task but never mid-apply.

### Stranded-run reconciler (`agents/reconciler.py`)

A 60s sweep loop (gated by `aegisops_reconciler`, started at `main.py:78`). `EXECUTING_STATES =
("running","applying")` (`reconciler.py:33`) — `awaiting_approval` is deliberately excluded (a
human wait, not stranded). For each executing run: skip if this worker is driving it
(`is_live`, `:54`) or another worker's heartbeat is alive (`:61`); otherwise it's stranded →
`_redrive` if the checkpoint is resumable (non-empty `snapshot.next`, `:149`) else `_mark_failed`
(`:205`). `_redrive` **persists the result** (`:189`) so a re-driven run isn't force-failed on
the next sweep. Also `sweep_orphans` (D2 inventory rebuild from the run outcome, no cloud read,
`:130`) and `sweep_tf_hygiene` (PR-1 stray-plan-file + destroyed-workspace pruning, sweeper-only,
`:97`).

### Event bus (`agents/events.py`)

`RunChannel` (in-memory, `events.py:34`) or `RedisChannel` (Redis Streams, `events.py:62`,
selected when `aegisops_event_bus == redis`). Both feed the same `_sse` consumer. Redis mode is
worker-agnostic: `emit` XADDs (`events.py:78`), a background XREAD pump feeds the queue
(`events.py`), a terminal `__eos__` marker + TTL evicts the stream (`events.py:126-134`).
`current_cursor()` (added for P0-3) lets the approval continuation tail from "now".

### Idempotency (`security/idempotency.py`)

Redis keys (`idem:` + sha256). `claim` is atomic (`SET NX`, `idempotency.py:24`) — True = newly
claimed. The **A1 wait-or-abort** contract (`idempotency.py:51`): if a claim is held, `get_result`
returns the stored result if done, else `wait_for_result` polls to a 20s deadline and returns
`None` — and the caller **must abort, never fall through to a second apply**
(`cloudops.py:1431-1444` honors this; `exec_loop.py:210` too).

---

## Pillar 4 — EVALS (honest)

### What exists

A large **deterministic** test suite across three tiers, run by `make test`:

- **Backend pytest — 91 test files** (`backend/tests/test_*.py`). Integration tests run inside
  the `api-test` container against **live** Postgres/Redis/Neo4j (fixtures `live_db`/`live_redis`
  gated on `AEGISOPS_TEST_LIVE_DATASTORES`, `backend/tests/conftest.py:28,38,60`). Coverage spans
  safety invariants (`test_safety_invariants.py`), idempotency, tenancy/RBAC, the module catalog
  (per-module `test_modseed_*` + C1 plan-assertion + committed `terraform test` back-compat
  gates), policy predicates, memory recall, the reconciler/supervisor, and the STAB fixes
  (`test_stab_p0*/p1*/p2*`).
- **Frontend vitest — 7 files** (`frontend/tests/*.test.*`): the store reducer, SSE frame
  parsing, and component rendering.
- **Playwright e2e — 8 specs** (`frontend/e2e/*.spec.ts`): real browser flows against the
  running stack — core flow, tenancy/roles/reveal, and the STAB live specs (P0-2 DEP
  convergence, P0-3 approve→live-progress, P1-6 queue).

Determinism is deliberate: LLM-dependent assertions are avoided or made robust to latency; the
recall/guard/policy paths are LLM-free and tested as such.

### What does NOT exist (the gap)

- **No LLM-as-judge / generative-quality eval harness.** A grep for an `llm_judge`/`eval_judge`
  module across `backend/app` + `backend/tests` finds nothing. There is no scored rubric over
  model outputs, no golden-answer regression for generated prose, no automated hallucination
  scoring. This is the **Phase-4 IU-6 gap** — answer *correctness* for the generative paths
  (router classification quality, knowledge answers, SRE analysis) is validated by humans and by
  the deterministic guards around the LLM, not by an eval judge.

The deterministic guards are what make the LLM's mistakes safe rather than measured: a
misclassification can't mutate (Invariant 3, `intent_guard.py`), a bad plan can't apply
(plan_guard at the approval choke-point, `approval.py:44`), and recall can't hallucinate
(LLM-free positional recall, `memory.py:77`).
