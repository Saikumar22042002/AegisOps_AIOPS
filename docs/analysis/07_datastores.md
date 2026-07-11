# 07 — Datastore integration (Postgres / Redis / Neo4j)

[← back to index](../../ANALYSIS.md)

## 7.1 PostgreSQL (+ pgvector, pg_trgm)

**Connection & lifecycle.** Async engine via `psycopg3` (`db/session.py:init_engine`, pool_size=10, max_overflow=20, `pool_pre_ping`, `pool_recycle=1800`), created in the FastAPI lifespan (`main.py`). `session_scope()` is the transactional context (commit on success, rollback on error). A separate psycopg **sync** pool backs the LangGraph checkpointer (`agents/checkpointer.py`, autocommit). Alembic runs sync (`alembic/env.py`).

**Schema** (`db/models.py`, migrations `0001`–`0003`):

| Table | Owns | Key columns |
|-------|------|-------------|
| `organizations` | tenant root | name, slug (unique), plan, member_count |
| `roles` | 8 RBAC roles | name (kebab), display_name |
| `users` | seeded users | org_id, keycloak_sub (unique), roles (JSONB) |
| `sessions` | conversation threads | org_id, **user_id (nullable, never set on chat)**, title, status, snow_id |
| `messages` | full transcript | org_id, session_id, role, content, confidentiality_*, trace_id, context_id, **run_id (FK)**, analysis (JSONB) |
| `runs` | one per request | org_id, session_id, intent, confidence, domain, workflow, mode, status, plan_json, input_json, outcome, trace_id, context_id, snow_id |
| `run_steps` | per-node timings | run_id, name, status, tool, human_vs_auto, started_at, ended_at, order_index, result |
| `approvals` | immutable HITL audit | org_id, run_id, decision, actor_user, actor_role, rationale, ts |
| `documents` / `document_chunks` | RAG corpus | chunk.embedding `Vector(768)` + HNSW cosine index; content + pg_trgm keyword fallback |
| `audit_log` | insert-only audit | actor, action, target, detail, correlation |
| `integrations` | health registry | name, kind, status |
| `resources` | **provisioned-resource inventory** | org_id, session_id, run_id, name, cloud, resource_type, provider_id, workspace, **state_workspace**, status, attributes (JSONB), inputs (JSONB) |
| `notifications` | in-app bell | title, body, level, color, read |
| *(langgraph checkpoint tables)* | durable graph state | created by `AsyncPostgresSaver.setup()` |
| *(langfuse schema, separate `langfuse` DB)* | traces | Langfuse-owned |

**Who reads/writes what, when:** `api/chat.py` inserts user Message + Run at request start and the assistant Message + final Run in `_persist_result`; graph nodes write `run_steps` (timing.py), `approvals` (approval.py), `resources` (inventory.py), `notifications` (notify.py), context via ContextGraph. `api/sessions.py`/`api/modules.py`/`api/artifacts.py` read for the UI. RAG writes `documents`/`document_chunks` (`rag/store.py`) and reads via cosine (`semantic_search`) or trigram (`keyword_search`).

**Indexes:** `org_id` on tenant tables, `resources(name)`/`(session_id)`, HNSW on `document_chunks.embedding`. **Missing indexes worth adding:** `messages(session_id, created_at)` (the transcript load orders by this on every chat turn), `messages(run_id)` (artifact `_load` filters by it), `runs(session_id)` / `runs(org_id, created_at)` (module/overview counts + recent lists scan these). Under load these are seq-scan risks.

## 7.2 Redis

**Connection:** `redis.asyncio.from_url` (`cache/redis.py`, `decode_responses=True`, health_check_interval=30). Keyspaces:

| Key pattern | Written by | Read by | TTL | Purpose |
|-------------|-----------|---------|-----|---------|
| `sess:<sid>` | `security/sessions.create_session` | `get_current_user` | 36000s | server-side session (user + Keycloak tokens) |
| `oauth_state:<state>` | `sso_login` | `callback` | 600s | PKCE verifier |
| `pending:collect:<session_id>` | `params.save_pending` | `router` + `cloudops_plan` | 1800s | multi-turn parameter collection |
| `idem:<hash>` | `idempotency.claim/store_result` | `cloudops_execute` | 86400s | prevent duplicate apply |
| `reveal:<run_id>:<output>` | `_claim_reveal` (NX) | `reveal_credential` | none (persists) | one-time credential reveal claim |
| `runinput:<run_id>` | `POST /runs/{id}/input` (rpush) | **nobody** | none | **dead** — no consumer exists (see 09) |

## 7.3 Neo4j (context graph)

**Connection:** `AsyncGraphDatabase.driver` (`graph_db/neo4j.py`). Constraints/indexes via `graph_db/schema.py` (`Context.id` unique, `Human.name` unique, indexes on `Context.org_id`, `Step(context_id,order)`, `Evidence.context_id`). **Model** (`graph_db/context_graph.py`): `Context` (per run/SR) →`TRIGGERED_BY` Trigger, `HAS_INTENT` Intent, `ROUTED_TO` Agent, `RUNS` Workflow, `HAS_STEP` Step (`NEXT`-linked, `USED_TOOL` Tool), `HAS_REASONING` Reasoning, `HAS_EVIDENCE` Evidence, `REQUIRED_APPROVAL` Approval `DECIDED_BY` Human, `RESULTED_IN` Outcome, `HAS_FEEDBACK` Feedback, `PROVISIONED` Resource; plus global `(Run)-[:CREATED]->(Resource)` and `(Session)-[:HAS_RUN]->(Run)`. Closed contexts are immutable (`_ensure_open` refuses writes after `close`). All string/dict payloads pass `redact`/`redact_dict` before write.

**Every graph write is best-effort** — wrapped in try/except that logs and continues, so the graph can be entirely down and runs still complete. This is safe but means the graph is a non-authoritative mirror.

## 7.4 How the three interact for one request

For "create an EC2 instance web-01" through approval and apply:

```
POST /chat
  PG:  INSERT sessions(if new), messages(user), runs(status=running)         [session_scope]
  RAM: create_channel(run_id)   ← SSE, NOT a datastore (in-process)
router node
  Redis: GET pending:collect:<session>        (none)
  PG:    (memory) SELECT messages WHERE session_id ORDER BY created_at  ← classification_context
  Neo4j: MERGE Context / Intent / Agent
cloudops_plan (params missing)
  Redis: SET pending:collect:<session> = record  EX 1800
  PG:    run_steps upsert (cloudops_agent timing)
  Langfuse: spans
cloudops_plan (params complete → plan)
  Redis: DEL pending:collect:<session>
  PG:    run_steps (planner/policy_evaluation)
  Neo4j: set_workflow / add_step / add_evidence
  ← interrupt → PG: runs.status = awaiting_approval (via _persist_result)
POST /approvals/{id} (approved)
  PG:    INSERT approvals (immutable)
  Neo4j: add_approval + Human
cloudops_execute (apply)
  Redis: SET idem:<hash> NX ; later value=done
  PG:    UPSERT resources (inventory)
  Neo4j: add_resource (resource↔run↔session)
verify/finalize/servicenow/notify
  PG:    UPDATE runs (outcome), INSERT messages(assistant), INSERT notifications, run_steps
  Neo4j: set_outcome + close
  Langfuse: end trace (flush)
```

## 7.5 Consistency & coupling risks (evidence)

1. **Cross-store writes are not transactional.** PG inventory, Neo4j resource node, and Langfuse trace are written in separate best-effort calls. A crash between the PG `apply` result and the inventory write leaves a real cloud resource with **no inventory row** → it can't be found for day-2/destroy. Mitigated slightly by idempotency, but there's no reconciliation job.
2. **Idempotency is not race-tight** (`agents/cloudops.py:935`): if `claim` fails because another run holds an in-progress claim (no result yet), the code falls through and **executes anyway** → concurrent double-apply is possible. Needs a wait-or-abort on in-progress claims.
3. **Local Terraform backend, no locking.** State is on a local volume per module dir; per-resource workspaces isolate *resources* but two operations on the *same* resource (or two legacy resources sharing the default workspace) can race the state file and the shared `aegisops.tfplan` plan file (`tools/terraform.py:61` — legacy resources all use `aegisops.tfplan`). No DynamoDB/state lock.
4. **`Session.user_id` never populated** on the chat path (`api/chat.py:117` constructs `Session(org_id=…)` only) → no per-user session ownership, feeding the authorization gap in [09](09_problems.md).
5. **Missing indexes** (§7.1) → seq scans on the hot transcript/run queries at scale.
6. **`resources` upsert key is (org, workspace, name, status=active)** — correct for isolation, but a legacy row (`state_workspace=NULL`) and a new isolated row can coexist for the same name across the migration boundary; destroy-by-name may act on the wrong one for pre-`0003` rows.
7. **Neo4j `Resource` MERGE key is `provider_id` or `cloud:name`** — if two clouds produce the same `cloud:name` fallback (no provider id yet), nodes could collide; low risk but unbounded.
