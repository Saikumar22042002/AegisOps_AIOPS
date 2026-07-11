# 06 — Memory & continuity

[← back to index](../../ANALYSIS.md)

There are **two distinct memory systems** in AegisOps, and they behave very differently. Conflating them is the fastest way to misread the product.

## 6.1 Conversational memory (what was said)

**Store & schema.** The full transcript lives in Postgres `messages` (`db/models.py:Message`): `session_id`, `role` (user|assistant), `content`, plus `analysis`, `run_id`, `confidentiality_*`, `created_at`. There is no separate summary table and no vector memory of the conversation — just the raw rows.

**Retrieval.** `agents/memory.py` is the only reader, and it is deterministic + DB-backed (no LLM in the memory path, so recall can't hallucinate):
- `load_history(session_id, limit=400)` — chronological `[{role, content}]`, ordered by `created_at, id`, capped at 400 rows.
- `build_transcript(session_id, max_chars, exclude_last_user)` — the core function. If the whole transcript fits in `max_chars`, it's included **verbatim**. If not, it renders a two-part budget: the **most recent turns verbatim** (~70% of the budget, taken from the end, each line clipped to 600 chars) **plus a digest of every OLDER *user* turn** (numbered `1. …`, each clipped to **160 chars**). Assistant replies from the older region are **not** in the digest.
- `classification_context(session_id, max_chars=1500)` — only the **last 8 turns** (each clipped to 180 chars), given to the router so it can resolve "do it again", "same but in GCP", "the previous one".
- `prior_user_questions(session_id)` — ordered user turns. **Defined but unused in app code** (only `test_memory.py` calls it); it exists for deterministic recall but nothing wires it in.

**What's passed into each LLM call:**
- `general` (`agents/general.py:47`): `build_transcript(session, max_chars=8000, exclude_last_user=msg)` prepended as "Conversation so far in this session:".
- `knowledge` (`agents/knowledge.py:45`): `build_transcript(session, max_chars=4000, …)` prepended before the RAG context.
- `router`: `classification_context` (last 8 turns) for reference resolution only.
- `cloudops`/`devops`/`sre`: **no transcript** is threaded into these agents. CloudOps parameter extraction sees only the current message (plus the Redis pending record for multi-turn *parameter* collection, which is a different mechanism).

The `general`/`knowledge` system prompts explicitly instruct the model to treat the transcript as the real history and never claim "no history" — this is the fix for the Phase-8 "my context window is blank" screenshots.

## 6.2 The concrete test — "in a 100-message conversation, if the user asks about the 20th message, will it be retrieved?"

**Trace the exact path** (assume the user asks a general question, so the `general` node runs with `max_chars=8000`):

1. `general` calls `memory.build_transcript(session, max_chars=8000, exclude_last_user=<current msg>)`.
2. `load_history` returns up to **400** rows — 100 messages is well within that, so **all 100 are loaded** (nothing is lost at the DB layer up to 400 messages).
3. **If** the full transcript ≤ 8000 chars, it's included verbatim → the 20th message (both user and assistant) is fully present → **the model can answer precisely.** But 100 messages at even ~80 chars each is ~8000 chars, and real messages are longer, so a 100-message thread almost always **exceeds** 8000 chars.
4. In the long-thread branch: the **most recent ~70% (5600 chars)** of turns are verbatim from the end. Message #20 of 100 is old, so it falls **outside** the recent window and lands in the **digest**. The digest numbers **every older *user* turn**, each clipped to **160 chars**. So:
   - **The user's 20th message: YES, retrievable — but truncated to 160 characters**, and identifiable by its position number in the digest.
   - **The assistant's 20th reply: NO** — assistant turns in the older region are not digested at all.

**Real answer: PARTIAL / lossy-yes.** The system *will* surface the gist of the user's 20th message (first 160 chars, numbered), so a question like "what did I ask around the 20th message?" can be answered approximately. It will **not** reliably reproduce the full text of that message, nor the assistant's answer from that turn, once the thread is long. If #20 happens to be within the most-recent ~70% window (short thread), it's verbatim. The router (which resolves references like "that instance") only sees the **last 8 turns**, so a reference to something said at message #20 in a 100-message thread would **not** be resolvable by the router — only the answering agent sees the digest.

**Why it's designed this way:** the digest deliberately prioritizes early *user* facts (names, codenames, decisions) surviving within a char budget, over full fidelity. It's a reasonable trade-off, but it means AegisOps is not "perfect recall like a fresh context window" — it's budgeted recall biased toward recent-verbatim + early-user-gist.

**Limits worth stating:** 400-message hard cap in `load_history` (a >400-turn thread silently drops the oldest); no token-based budgeting (char-based, which mis-estimates for code-heavy content); no summarization model (pure truncation).

## 6.3 Resource memory (what was provisioned) — a different, stronger system

This is **not** conversational memory. It's the inventory + context graph, and it's how "the instance I created" / "test-vm" resolve to real recorded facts.

- **Store:** Postgres `resources` (`db/models.py:Resource`) — one row per successful apply: stable `name`, `cloud`, `region`, `resource_type`, `provider_id` (instance/VPC id), `workspace`, `state_workspace`, `status` (active|terminated|destroyed), `attributes` (IPs/VPC/subnet/SG/key/ports from real TF outputs), `inputs` (validated TF vars, to rebuild a modify plan), `run_id`/`session_id`. Mirrored into Neo4j (`ContextGraph.add_resource`: `resource↔run↔session`).
- **Write:** `inventory.record_from_apply` on every successful apply (`cloudops_execute`).
- **Reference resolution** (`inventory.resolve`, `cloudops.py` → `_read_resource`/`_modify_resource`/`_destroy_resource`):
  1. **broad** ("all resources", "everything") → list everything active;
  2. **exact name** (case-insensitive);
  3. **name substring** (either direction);
  4. **descriptive** ("the s3 bucket I created", "the instance I just made") → most-recent active **of the mentioned type only** — whole-word tokenized so `ghost-server-99` can't fuzzy-match on "server", and an S3 question never recalls the EC2 (the Phase-7 type-safety fix).
  Not found or ambiguous → the agent asks, never guesses.
- **Live reconciliation** (`inventory.reconcile`): on a specific read, AWS EC2 resources are refreshed via a live `boto3` describe (IPs/state/VPC/subnet), and terminated instances are marked so they're no longer offered for day-2 ops. **Only AWS EC2 reconciles live** — Azure/GCP/other AWS types return recorded values.
- **Provenance** (`inventory.provenance` → Neo4j `resource_provenance`): a specific read is enriched with which run/session provisioned it — read from the graph, never inferred.

The LLM's only role here is mapping phrasing → a lookup key; the ids/VPCs/IPs come from the stores. That's the right design and it's real.

## 6.4 Cross-session recall — what persists across sessions vs within one

| Memory | Within a session | Across sessions |
|--------|------------------|-----------------|
| Conversation transcript | ✅ (budgeted, §6.2) | ❌ — `memory.*` only ever loads the current `session_id` |
| Reference resolution ("that instance") | ✅ last 8 turns (router) | ❌ |
| **Resource inventory** (`resources`) | ✅ | ✅ — org-scoped, not session-scoped; day-2 read/modify/destroy work in a new session |
| **Context graph** (Neo4j) | ✅ | ✅ — global resource/provenance reads |
| Idempotency / pending-collection (Redis) | per-session/run, TTL'd | ❌ (ephemeral) |

**Net:** *what you built* is remembered forever and across sessions (inventory + graph). *What you said* is remembered only within the current session, and lossily once the thread is long. There is no cross-session conversational memory and no user-level long-term memory/profile. For a "seamless like ChatGPT" bar this is the biggest continuity gap after the tenancy issue — see [10 · gap analysis](10_gap_analysis.md).
