# Retrieval Flow — Real Context Assembly vs the Ideal Pipeline

**Date:** 2026-08-16. Evidence: `app/agents/memory.py` (read in full), live query battery, PG counts.

## 1. The ideal pipeline vs reality

```
IDEAL:  query → entity resolution → metadata filtering → graph/vector/keyword retrieval
        → reranking → temporal filtering → retrieval gate → context assembly → LLM

REAL:   query → [positional-recall regex] → [k=3 session-scoped vector/trgm lookup]
        → transcript (recent verbatim + older-user digest, char budget) → LLM
```

| Stage | Status | Reality |
|---|---|---|
| Entity resolution | MISSING | none; `detect_recall` regex handles only "my 3rd question"-style positional recall (`memory.py:91`) |
| Metadata filtering | MISSING | retrieval is `WHERE session_id = current` — no cloud/resource/user-fact filters |
| Vector retrieval | IMPLEMENTED (narrow) | `memory.retrieve` (`memory.py:112`): cosine over `messages.embedding`, **top-3, current session only**; pg_trgm keyword fallback when embedding unavailable |
| Graph retrieval | MISSING (for context) | Neo4j never queried for context assembly |
| Keyword retrieval | fallback only | pg_trgm similarity inside `retrieve()` |
| Reranking | MISSING | raw cosine order, k=3 |
| Temporal filtering | MISSING | no time predicates anywhere |
| Retrieval gate | NEVER EXECUTED | exists only in dark `harness/memory.py`; `retrieval_gate` purpose absent from llm_usage |
| Context assembly | IMPLEMENTED | `build_context` (`memory.py:261`): standing user-memory block (M4; table empty in practice) + recall slot (positional + k=3 hits, 600-char clips) + transcript |
| Transcript | IMPLEMENTED | `build_transcript` (`memory.py:196`): short session verbatim; long session → digest of EVERY older user turn + recent turns in full, fitted to a per-purpose char budget (~3000 chars default, `budget_tokens*4` when given) |

## 2. Who receives context (decisive)

| Consumer | Context received |
|---|---|
| router | `classification_context` (≤1500 chars) — recent-window |
| general agent | full `build_context` |
| knowledge agent | full `build_context` + document retriever (corpus EMPTY) |
| **cloudops agent** | **NONE** — no build_context/transcript call anywhere in cloudops.py; only its own `params.save_pending` slot-filling state |
| devops / sre | sre uses retriever + investigation; no transcript |

This is why explicit in-message facts ("in the audit-net VPC") can be ignored by infra flows while chit-chat recall works.

## 3. Verdict: retrieval or conversation-window?

**Hybrid, honestly better than pure window-dumping, but session-bound and shallow:** recent-window transcript + digest + tiny (k=3) semantic recall over the SAME session. It is NOT: cross-session, entity-aware, graph-aware, temporally filtered, reranked, or gated. For any question about infrastructure history, retrieval contributes nothing — those answers come from deterministic inventory/live-cloud code (see INTELLIGENCE_LAYER_AUDIT.md §1).

## 4. Long-conversation measurement (audit session e4273347, 40+ messages spanning VPC/EC2/ports/queries)

- Prompt context budget: purpose-based char budgets (~3000 chars ≈ 750 tokens default; router 1500 chars). **No context bloat in prompts** — budgets are enforced by construction (verified in code; transcript fitting truncates).
- Retrieved facts per query: ≤3 message snippets (600-char clips) + optional 1 exact positional turn.
- Graph results: 0. Document-vector results: 0 (corpus empty).
- Old-resource recall across unrelated turns: "What VPC and subnet is MySource using?" after many unrelated turns → answered correctly, but from the PG inventory row, NOT from conversational retrieval — the memory system gets no credit for it.
- Failure case: after the failed t2.micro run, the immediately-following retry lost OS/key-pair/access/VPC parameters (run 6e931805) — parameter continuity is the params/pending slot store, not the transcript, and a failed run clears it.

## 5. Context bloat — where it actually lives

Prompts are budget-bounded (no bloat). The bloat is in **answers**: the broad-inventory branch dumps all 16 org rows (all clouds, all time, ghosts included) into the chat for questions like "what did I change yesterday". The user-visible symptom of "conversation dumping" is real but its source is the deterministic inventory renderer, not the LLM context path.

## 6. Minimal-change upgrade path (post-audit recommendation)

1. Give cloudops read/modify flows the same `build_context` slice the general agent gets (fixes in-message fact loss cheaply).
2. Add filters to the inventory listing (cloud from router classification, session for "this conversation", created_at ranges for temporal words) — the columns already exist.
3. Feed the document corpus (runbooks/RCAs) — the retriever is already wired and idle.
4. For real temporal/semantic history retrieval, see the Graphiti coexistence plan (`CONTEXT_GRAPH_MODEL.md` §5–6).
