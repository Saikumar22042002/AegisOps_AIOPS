# Intelligence Layer Audit — What Actually Answers Questions

**Date:** 2026-08-16. Method: live SSE captures + store queries + code tracing. Full run IDs in `AEGISOPS_CURRENT_STATE.md` §5.

## 1. The flagship query: "What are the resources I've created in AWS?"

**Observed answer (run 99454ced):** a live-discovery summary line + a 16-row inventory table that included 3 GCP resources and 13 ghosts from dead sandbox accounts.

**Complete data lineage (traced):**

```
User message
→ POST /chat (app/api/chat.py:421) — require_initiator, prepare_run (runs row, Redis channel)
→ LangGraph router node (app/agents/router.py)
    context: memory.classification_context(session_id)          [PG messages — recent window]
    LLM: purpose=router, gemini-3.5-flash, JSON classification
    guards: normalize_classification → _clamp_target → apply_post_guard_rules
    result: domain=cloudops, action=read, target="all" (broad-inventory rule, router.py:112)
→ cloudops_plan node (app/agents/cloudops.py) — read path
    ① LIVE CLOUD: boto3 describe calls (EC2/S3/RDS/VPC counts)   [AWS APIs — real]
    ② INVENTORY: inventory.resolve(org_id, "all")                [PostgreSQL resources table,
       WHERE org + status='active' — NO cloud filter, NO session filter, NO liveness check]
    render: counts line + markdown table of ②
→ finalize → confidentiality classify → SSE tokens → browser
```

**What it uses / does not use:**

| Source | Used? | How |
|---|---|---|
| Live AWS APIs | YES | summary counts line only |
| PostgreSQL | YES | `resources` table dump (org-scoped only) |
| Neo4j | NO | not on this path |
| pgvector | NO (for the answer) | message embeddings only feed router context |
| Redis | transport only | SSE event stream |
| Conversation history/digest | router classification context only | not in the answer content |
| RAG / Knowledge layer / Retrieval gate | NO | knowledge agent not routed; gate exists only in dark harness |
| Graph or vector retrieval | NO | — |
| LLM-generated summary | NO | the table is deterministic code (`_render_inventory_list`) |

**Why AWS queries can return GCP resources:** the broad-inventory branch (`kind == "all"`, cloudops.py:~1042) lists EVERY active row for the org. The router's extracted `cloud=aws` is never applied as a filter to the listing. Same mechanism ignores "in this conversation" (rows carry `session_id` — unused) and any time qualifier.

**User/workspace filtering:** org scoping IS correct (org_id predicate everywhere; cross-org proven non-enumerable). Cloud, session, and liveness scoping are absent on the listing path.

## 2. Component-by-component proof table

Chain tested: exists → initialized → invoked → output → consumed → persisted → observable → tested.

| Component | exists | init | invoked (live path) | output | consumed | persisted | observable | tested | Contributes to final LLM context? |
|---|---|---|---|---|---|---|---|---|---|
| Agent Loop (`agents/exec_loop`) | ✓ | flag `aegisops_exec_loop=off` | **NO by default**; proven when on (run bed04e5a) | DAG plan + one approval | execute node | runs/steps | steps+interrupt SSE | this audit | n/a (planning is deterministic; `loop.main` purpose never in llm_usage) |
| Retrieval Gate | ✓ (`harness/memory.py` only) | never | **NEVER** — no live caller | — | — | — | — | unit only | **NO** |
| Knowledge Layer (knowledge agent + rag) | ✓ | ✓ | on knowledge-routed queries | retriever over `documents` | prompt refs | `documents`=0 → nothing | Langfuse `llm.knowledge` (0 uses in ledger) | — | **NO (empty corpus)** |
| Consolidation | ✓ (`harness/memory.py` only) | never | **NEVER** | — | — | `memory_items`=0 | — | — | **NO** |
| Context management (`agents/memory.py`) | ✓ | ✓ | router/general/knowledge every call; **cloudops: never** | standing block + recall slot + transcript | LLM prompts | reads `messages` | code-level | this audit | **YES (the only real contributor)** |
| Context Graph writes | ✓ | ✓ | every run + mutation | nodes/edges | — | Neo4j (93 Runs, 43 Resources…) | graph queries | this audit | NO |
| Neo4j reads | ✓ | ✓ | 3 paths: provenance line, `impact_of` destroy check, investigations | provenance string / dependents | resource card / destroy gate | — | card text "(context graph)" | this audit | Only via the card text |
| pgvector messages | ✓ | ✓ | `embed_message` on every user+assistant msg (chat.py:234,327); `retrieve()` k=3 in build_context | top-3 related turns | prompt slot | 537/1845 embedded | llm_usage `embedding`=214 | this audit | YES (small) |
| pgvector documents | ✓ | ✓ | retriever wired | — | — | 0 rows | — | — | **NO** |
| Hybrid retrieval | partial | — | vector→trgm fallback inside `memory.retrieve` | — | — | — | — | — | session-scope only |
| Embeddings/reranking | embeddings ✓ / reranking ✗ | — | gemini-embedding-001 | — | — | — | — | — | no reranker exists |
| Provenance | ✓ write / ✗ answerable | ✓ | approvals table + graph on every decision/apply | immutable approvals rows; accreting graph edges | resource-card line only | PG+Neo4j | ✓ | this audit | not queryable conversationally |
| Temporal memory | ✗ | — | — | — | — | timestamps exist, no temporal query path | — | q3/q4/q7 all failed | **NO** |
| Verification | ✓ | ✓ | post-apply (finalize.py:64) | check list | tool_results + graph Evidence | ✓ | console frames | this audit | NO |
| Planner/Router | ✓ | ✓ | every run | classification / tf plan | graph edges | runs/steps | Langfuse `llm.router` | this audit | router yes |

## 3. Why Langfuse shows router/cloudops_agent but no Agent Loop

Langfuse generations are emitted per LLM call, named `llm.<purpose>` (`app/llm/service.py`). Router/classify/extract/general purposes fire constantly → those traces exist. The Agent Loop:

1. is behind `AEGISOPS_EXEC_LOOP` (default **off**) — it has never executed in this environment until this audit's probe;
2. is entered only when `dependency.resolve_closure` returns a create-first DAG (cloudops.py:618) — single-resource requests never reach it even with the flag on;
3. even when it ran (probe bed04e5a), its planning was deterministic — **zero `loop.main` LLM calls in the ledger** — so there is no `llm.loop.main` generation to display.

Additionally, ALL traces currently land in the wrong Langfuse project ("AegisOps" vs expected "aegisops" — boot warning `langfuse.wrong_project`), so the dashboard under-reports everything.

**Safe query that genuinely triggers the Agent Loop** (requires `AEGISOPS_EXEC_LOOP=on` + api recreate):

> "Create a t2.micro EC2 instance named loop-probe running Amazon Linux 2023 in a NEW VPC named loop-net with CIDR 10.44.0.0/16. Create a key pair, no remote access."

The missing parent VPC forces the dependency DAG → governed-exec-loop plans every step and pauses at ONE whole-DAG approval. **Reject the approval** and nothing is mutated — the loop's planning, events, and steps are still fully exercised and traced (proven: run bed04e5a, interrupt payload `workflow: "governed-exec-loop"`). No fake telemetry was created during this audit.

## 4. Fifteen answers in one table (query battery, session e4273347)

| Question | Verdict | What actually happened |
|---|---|---|
| Resources created in AWS | WRONG SCOPE | live counts ✓ + unfiltered inventory (GCP rows + ghosts) |
| VPC/subnet of MySource | CORRECT | inventory row + live reconcile; provenance line cites LAST run, not creating run |
| Ports opened/removed + when | NO HISTORY | static current-state card |
| Previous configuration | FAIL | generic discovery dump, 128 s |
| Who approved + when | MISROUTE | devops agent → "needs GITHUB_TOKEN" (data complete in `approvals` + graph) |
| Created in this conversation | NO SESSION FILTER | full 16-row dump (rows have session_id — unused) |
| What changed yesterday | NO TEMPORAL FILTER | full dump |
