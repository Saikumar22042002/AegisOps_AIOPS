# §3b — Fixes: Memory & continuity · Security & RBAC · Observability

[← back to FIX index](../../FIX.md) · Sizing **S/M/L**, blast low/med/high.

---

## M. Memory & continuity — the Context Engine

Grounded in [ANALYSIS §06](../analysis/06_memory.md). Two systems: **conversational** (lossy) and **resource** (strong). The fixes make conversational recall guaranteed and thread it into every LLM call.

> **Stage-A amendment (decision 9, final):** M1–M4 are implemented as a **five-layer Context Engine**. Item IDs and acceptance tests below are unchanged; the amendment adds **context offloading** (M5, new) and makes **purpose-routing** explicit in M1. Layer map:
>
> | Layer | What it is | Items |
> |-------|-----------|-------|
> | 1. **Retrieval** | pgvector message embeddings (semantic, per-session) + deterministic `get_turn` positional recall; also spans operational history + runbooks | M2 |
> | 2. **Compression** | rolling LLM summary of older turns (replaces the 160-char digest) + **context offloading** — plan JSON / apply logs / discovery dumps stored as artifact references, fetched on demand, never inlined | M1, **M5** |
> | 3. **Persistent memory** | per-user/per-org standing context (preferences, naming conventions, defaults) — org/user-scoped under S0, user-editable | M4 |
> | 4. **Routing** | `build_context(session, budget_tokens, purpose)` threaded into **EVERY** LLM call — router, cloudops, devops, sre, and the executive loop — each getting a purpose-shaped slice | M1, M3 |
> | 5. **Verification** | memory answers are store-grounded (message rows, inventory rows), never LLM recall; the Phase-3 Reconciliation Engine verifies remembered state against actual cloud state | M2/M3 discipline + World Model ([04 §D3](04_fixes_ux_data.md#d3--world-model--reconciliation-engine-resolved-invest--decision-10--l--phase-3)) |

### M1 — Thread full session history into every LLM call, token-budgeted · **ANALYSIS §06.1** · M · blast: medium
**Now:** `general`/`knowledge` get `build_transcript` (char-budgeted, 8000/4000); CloudOps/DevOps/SRE get **no transcript**; the router sees only the last 8 turns (`agents/memory.py:classification_context`).
**Change:**
- Replace char-budgeting with **token-budgeting** (count with the provider tokenizer; fall back to a chars≈tokens heuristic, *flagged for verification against the real Gemini tokenizer at impl time*).
- Introduce `memory.build_context(session_id, *, budget_tokens, purpose)` returning a structured context: **(a) a rolling summary** of older turns (produced by a cheap LLM summarization pass, cached on the session and updated incrementally), **(b) the most-recent N turns verbatim**, and **(c) a slot for retrieved specific turns** (M2).
- **Purpose-routing (amendment):** `purpose` shapes the slice — the router gets summary+retrieval (replacing the last-8-turns window); CloudOps gets inventory + user memory + prior params; SRE gets telemetry + runbooks; the executive loop gets goal DAG + observations. Large payloads enter as references (M5), not inline.
- Thread this into **all** agents that call the LLM (add to `cloudops`/`devops`/`sre` extraction/classification prompts, not just general/knowledge — and the Phase-3 executive loop) so reference resolution works everywhere.
**Blast radius:** `agents/memory.py` + every agent's prompt assembly; contained behind the one `build_context` function.
**Verify:** extend `tests/test_memory.py` — a 40-turn thread yields summary+recent+retrieval within budget; CloudOps extraction sees prior turns.

### M2 — The concrete guarantee: "in a 100-message thread, recall the 20th message" · **ANALYSIS §06.2** · M · blast: medium
**Now:** the user's 20th turn survives only as a 160-char digest line; the assistant's 20th reply is dropped; the router can't see it at all.
**Change — a two-part guarantee, both deterministic:**
1. **Positional recall (exact).** `messages` already stores every turn ordered by `created_at,id`. Add `memory.get_turn(session_id, ordinal)` and a **deterministic recall detector** (regex: "the Nth message/question", "what did I ask (first|earlier|at the start)") that resolves the ordinal and injects that **full turn verbatim** into the retrieved-turns slot (M1c). No truncation, no LLM guess.
2. **Semantic recall (fuzzy).** Add a **per-session retrieval** over `messages` so "what did I say about the VPC earlier?" fetches the actual turn regardless of thread length. Reuse the existing pgvector infra: embed each message on write (a small addition to `_persist_result` / message insert) into a `message_embeddings` table (or a nullable column), and retrieve top-k for the current question, injected into M1c. Falls back to `pg_trgm` keyword search when no embedding model is configured (mirrors `rag/store.py:keyword_search`).
**Guarantee statement (what we can then claim honestly):** *for any thread length, asking about a specific earlier turn returns that turn's full text* — via positional lookup when the user names a position, via semantic/keyword retrieval otherwise. The lossy digest becomes a *fallback narrative summary*, not the only recall path.
**Blast radius:** message-write path (embed), `agents/memory.py`, one migration. Feature-flag the embedding write so a no-Gemini setup degrades to keyword recall.
**Verify:** the headline test — seed a 100-message session; assert `get_turn(session,20)` returns turn 20 verbatim; assert "what was my 20th question?" routes to a recall that includes turn 20's full text; assert semantic recall finds a turn by content. This test is the acceptance gate for M2.

### M3 — Reference resolution unified across transcript + inventory · **ANALYSIS §06.3** · S · blast: low
**Now:** router resolves "do it again" from the last 8 turns; inventory resolves "test-vm"/"the instance I created" (`agents/inventory.resolve`).
**Change:** give the router the M1 context (summary+recent+retrieval) instead of raw last-8, so mid-thread references resolve; keep inventory resolution as-is (it's good). Document the split: *conversational* references → memory; *resource* references → inventory.
**Verify:** router test — a reference to something said 30 turns ago resolves via retrieval.

### M4 — Cross-session continuity (competitive) · **ANALYSIS §06.4, §10** · M · blast: medium
**Now:** conversational memory is per-session; only inventory/graph cross sessions.
**Change:** a per-user "working context" (recent resources built, stated preferences, recurring naming conventions) surfaced into the router/general context. Build on the existing org-scoped `resources` + a new lightweight `user_memory` (key facts, TTL-less, user-editable). Scope carefully under P2 (per-user).
**Verify:** a new session references "my usual VPC" → resolves from user memory + inventory.

### M5 — Context offloading (new, Stage-A amendment) · **decision 9, compression layer** · M · blast: medium
**Now:** plan JSON, apply logs, and discovery dumps are either inlined into prompts/transcripts or dropped entirely by the char budget.
**Change:** store large operational payloads (plan JSON, apply/console logs, discovery output) as **artifacts referenced by id** — the LLM context carries a short reference line ("plan for run `<id>`: 3 to add, 0 to change, 0 to destroy — full plan available"), and agents fetch the full payload on demand via a deterministic lookup (`runs.plan_json`, `run_steps`, or an artifact store ref). Applies to `build_context` output and the executive loop's observations.
**Blast radius:** `agents/memory.py`, prompt assembly in agents; no schema change needed where `runs`/`run_steps` already hold the payload.
**Verify:** a long session with multiple plans stays within the token budget; an agent asked about a prior plan's contents fetches and answers from the stored artifact, not a truncated inline copy.

---

## S. Security & RBAC

Grounded in [ANALYSIS §08](../analysis/08_observability_security_rbac.md), P1/P2/P3/P15/P20.

### S0 — Real multi-tenancy (foundation) · **P2** · L · blast: high
**Now:** `repo.get_default_org()` everywhere; `user.org` ignored; `Session.user_id` never set.
**Change:**
- Map the authenticated principal → org: on login/`user_from_claims` (`security/deps.py`), resolve/create the `users` row by `keycloak_sub` and carry `org_id` + `user_id` on the `User` object (extend `schemas/auth.User`).
- Replace `repo.get_default_org(s)` with `repo.org_for(user)` at **every** call site (`api/chat.py`, `sessions.py`, `modules.py`, `knowledge.py`, `artifacts.py`).
- Populate `Session.user_id` in `api/chat.py:chat` and `POST /sessions`.
- Add org predicates to every list/read query.
**Blast radius:** every endpoint + the auth layer. This is the biggest Tier-0 change; do it first because P1/P3/A4 all depend on it.
**Verify:** two seeded orgs + users → each sees only its own sessions/runs/inventory/knowledge; a cross-org UUID read → 404. New `tests/test_tenancy.py`.

### S1 — Credential reveal: mandatory re-auth + audit + ownership · **P1 (locked posture)** · M · blast: medium
**Now:** `api/artifacts.py:209 reveal_credential` — `Depends(get_current_user)` only; no ownership, no re-auth, no audit.
**Change (all mandatory per the locked decision):**
1. **Authorization:** require the caller to be an **approver OR the run's initiator** (`run.initiated_by`, from A5), **and** `run.org_id == user.org` (from S0). Else 404 (not 403 — avoid enumeration).
2. **Step-up re-auth (mandatory):** the reveal request must carry a **fresh authentication proof** — a Keycloak token obtained/re-validated within a short window (e.g. ≤120s). Implementation: the client performs a step-up (re-enter password → `password_grant`, or an OIDC re-auth with `max_age=0`); the backend validates that token via `keycloak.validate` and checks its `auth_time`/`iat` is within the window before revealing. No valid fresh proof → 401 "re-authenticate to reveal a credential." *(Flag: the exact step-up mechanism — password re-entry vs. silent `max_age=0` redirect — is a UX choice to confirm; both are supported by the existing Keycloak client.)*
3. **Always-on audit (mandatory):** write an `audit_log` row (`AuditRepo.log`) on **every** reveal attempt — success *and* denial — with actor, run_id, output name, org, decision, correlation ids; the value itself is **never** logged (redaction already excludes it). Emit a Langfuse event too.
4. Keep the Redis NX one-shot claim.
**Blast radius:** `api/artifacts.py` + a small frontend step-up flow (`frontend/components/Workspace.tsx:CredentialReveal`) + `security/deps.py` (a `require_fresh_auth` dependency).
**Verify:** `tests/test_rbac_endpoints.py` — non-owner/non-approver → 404; owner without fresh proof → 401; owner with fresh proof → value once, second → 410; **every** attempt writes an audit row.

### S2 — Authorize session/run reads + streams · **P3** · M · blast: medium
**Now:** `session_messages`, `get_run`, all `runs/{id}/{tab}`, `chat_stream` are auth-only.
**Change:** a shared `authorize_run(run, user)` / `authorize_session(session, user)` helper (org + owner/approver) applied in `api/artifacts.py:_load`, `api/chat.py:get_run`/`chat_stream`, `api/sessions.py:session_messages`. 404 on mismatch.
**Blast radius:** the read endpoints; contained in two helpers.
**Verify:** `tests/test_tenancy.py` — cross-user/cross-org read of every tab → 404.

### S3 — `/chat` requires initiator role · **P?/§08** · S · blast: low
**Now:** `POST /chat` is `get_current_user` only — a read-only role can start a run (can't approve, so can't apply, but shouldn't initiate).
**Change:** `Depends(require_initiator)` on `/chat` (read-only roles get a clear 403). Keep read-only able to *view*.
**Verify:** `tests/test_rbac_endpoints.py` — read-only POST /chat → 403.

### S4 — Redaction backstop on persisted answer/outcome · **P20** · S · blast: low
**Now:** `_persist_result` stores `answer`/`outcome` verbatim; redaction covers console/graph/Langfuse but not the persisted answer.
**Change:** run `redact()` on `answer` and `redact_dict()` on `outcome` before the DB write in `api/chat.py:_persist_result`.
**Verify:** `tests/test_redaction.py` — a planted secret in `answer` is masked in the persisted `messages.content`.

### S5 — Per-tool RBAC assertion (defense-in-depth) · **§08** · S · blast: low
**Now:** `CLAUDE.md` claims per-tool RBAC; not implemented (tools don't see the user).
**Change:** thread a minimal capability object into `config.configurable` (from the run's initiator) and assert `can_execute` at the `execute` node before any mutation — a cheap belt-and-suspenders behind the approval gate. Full per-tool enforcement is optional; the choke-point assertion is the high-value 20%.
**Verify:** execute node refuses if the run's initiator lacks execute capability (shouldn't happen given the approval gate, but proves the invariant).

---

## O. Observability

Grounded in [ANALYSIS §08](../analysis/08_observability_security_rbac.md), P9/P19.

### O1 — Traces tab shows the real Langfuse trace · **P9** · M · blast: low
**Now:** `api/artifacts.py:184 traces()` returns static span names.
**Change:** query the Langfuse public API for the run's trace tree (`trace_id == run_id`) and return the real nested spans with durations/tokens/cost; or, if the Langfuse API isn't reachable, **derive** the tree from `run_steps` (which already have real start/end + order) as a faithful fallback, and deep-link to the Langfuse UI. Prefer the `run_steps`-derived tree as the primary (no external dependency, already accurate) + a "open in Langfuse" link.
**Verify:** `tests/` — the traces endpoint returns the run's real step tree with real durations; no `—` placeholders for a timed run.

### O2 — The trace/span tree already fires — keep + guard · **ANALYSIS §08** · S · blast: low
**Now:** Langfuse trace_id==run_id + nested spans + generations with cost is real and tested (`tests/test_langfuse_tracing.py`).
**Change:** none structurally; add a **project-key startup assertion** (log a warning if `LANGFUSE_PUBLIC_KEY` doesn't belong to the `aegisops` project) so the "0 traces / wrong project" regression can't recur silently. Plan the **v2→v3** migration as a Phase‑3 item (flagged — verify v3 API shape at that time).
**Verify:** startup log asserts the key/project; existing tracing tests stay green.

### O3 — Correlation, metrics hygiene · **P19** · S · blast: low
**Now:** structured logs with correlation ids are real; `TOOL_RETRIES`/`AGENT_STEP_DURATION` declared but never emitted; SSE route is rate-limited.
**Change:** wire `AGENT_STEP_DURATION` from `timing.end_step` and `TOOL_RETRIES` from the tenacity retry callbacks (or remove them so dashboards aren't empty); exempt/soften the rate limit on the SSE `/chat` route (`main.py`).
**Verify:** `/metrics` shows non-empty step-duration + retry series after a run; a long SSE stream isn't rate-limited.

---

## Summary — memory/security/observability effort

| Item | Size | Blast | Phase |
|------|------|-------|-------|
| S0 real multi-tenancy | L | high | 1 |
| S1 reveal re-auth+audit+ownership | M | medium | 1 |
| S2 session/run read authz | M | medium | 1 |
| S3 /chat initiator gate | S | low | 1 |
| S4 persist redaction backstop | S | low | 1 |
| S5 per-tool RBAC assertion | S | low | 1–2 |
| M1 token-budgeted context everywhere | M | medium | 2 |
| M2 positional + semantic recall guarantee | M | medium | 2 |
| M3 unified reference resolution | S | low | 2 |
| M4 cross-session user memory | M | medium | 3 |
| M5 context offloading | M | medium | 2 |
| O1 real Traces tab | M | low | 2 |
| O2 tracing keep + project assert | S | low | 1 |
| O3 metrics/rate-limit hygiene | S | low | 2 |
