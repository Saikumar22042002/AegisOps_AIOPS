# Intelligence Layer Implementation — Prompt 2 (2026-08-17)

Status ledger for the temporal-infrastructure-intelligence phase. Every capability is
classified with its CURRENT proof level; this file is updated as live evidence lands.
Companion history: `AEGISOPS_CURRENT_STATE.md` (forensic baseline), `RETRIEVAL_FLOW.md`
(pre-Prompt-2 reality), `PROD_CORRECTNESS_REMEDIATION.md` (Prompt 1).

## Architecture (implemented — one canonical path, nothing duplicated)

```
USER QUERY (chat)
  → router (unchanged) → agent (general/knowledge/cloudops…)
  → memory.build_context (THE façade — same signature, flag-gated)
      → deterministic fast-path: greetings/parameter answers skip retrieval entirely
      → RETRIEVAL GATE            harness/memory.gate (P2 seam, now LIVE; fail-open;
                                  `agent_gate` run_events)
      → PLANNER                   deterministic source selection (entities from inventory,
                                  temporal window words, history/provenance/recall shape)
      → SOURCES (parallel, per-leg fault-tolerant, only what the plan needs):
          resource_revisions (PG)         CHANGE HISTORY — audit-authoritative
          Graphiti facts (Neo4j)          INFRASTRUCTURE MEMORY — derived temporal facts
          Graphiti episodes               PAST SESSIONS — consolidated narrative
          messages pgvector (existing)    CONVERSATIONAL MEMORY
          memory_items (accepted)         ACCEPTED OPERATIONAL FACTS
          (documents stay with the knowledge agent — DOCUMENT KNOWLEDGE is its block)
      → dedup (normalized fact keys) → temporal filter (valid/invalid/superseded)
      → budgets (per-source caps + total; dropped_by_budget counted)
      → typed, provenance-labeled context blocks with [since <date>] stamps
      → COMPACTION                on transcript overflow: deterministic SESSION STATE block
                                  from durable stores (resources/revisions/approvals/pending
                                  params/failures) — never blind truncation
      → RetrievalTrace            observable evidence → `observation` run_event + Analysis card
```

Graphiti provider neutrality: `AegisLLMClient` / `AegisEmbedder` / `AegisReranker`
(app/intelligence/graphiti_layer.py) subclass Graphiti's abstract clients and route through
`app/llm/service` under the catalog purposes `consolidation` / `embeddings` — the EXISTING
model routing decides Graphiti's provider; switching the binding switches Graphiti. No
vendor SDK enters the intelligence package (P1.9 boundary holds). Ranking/budget mechanics
adapted from the operator's graphiti-chatbot POC (dedup, relevance+recency blend, token
budgets, `[since]` stamps, max_coroutines quota lesson, naive-UTC coercion).

Deterministic facts: `facts.py` derives Graphiti temporal edges from `resource_revisions`
(the immutable Prompt-1 journal) with **no LLM** — action facts, port open/close facts with
deterministic invalidation (a close sets `invalid_at` on the matching open fact), topology
BELONGS_TO facts, destroy-expiry. uuid5 business keys make ingestion idempotent; a Redis
cursor bounds batches; every fact carries org/session/run/revision/cloud/region/actor
provenance. `group_id = org_id` isolates tenants on every write and search.

Consolidation: post-run hook (chat.py) → `harness/memory.consolidate` (P2 seam, now live;
LLM via the P1 adapter) → episode into Graphiti (idempotent per run) + fact PROPOSALS
(memory_items writes stay human-gated per 06 §2). Raw provenance is never destroyed.

## Capability classification (LIVE battery completed 2026-08-17 ~15:30 UTC)

| Capability | Status | Evidence (run ids / live checks) |
|---|---|---|
| Retrieval gate on the live path | **PROVEN ACTIVE** | live gate decisions with real reasoning in `agent_gate` run_events (run `ae6a7d6a`); unit pins (skip honored, fail-open) |
| Deterministic no-retrieval fast-path | **PROVEN ACTIVE** | live "hi!" turn → Analysis card "skipped — deterministic … ~0 tokens", zero sources queried |
| Retrieval planner (selective sources) | **PROVEN ACTIVE** | live: entity question → revisions 3/3 + graphiti 6/8 + dropped 5 by budget (run `81fba1c4`); no-entity question → messages leg only |
| Typed context blocks + budgets | **PROVEN ACTIVE** | live context: [CHANGE HISTORY]/[INFRASTRUCTURE MEMORY]/[CONVERSATIONAL MEMORY]/[SESSION STATE] blocks; dropped_by_budget live-observed |
| Graphiti client (provider-neutral) | **PROVEN ACTIVE** | live on shared Neo4j 5.26 community; 11 entities / 17 facts / 1 episode, org-scoped |
| Deterministic fact ingestion | **PROVEN ACTIVE** | 18 revisions ingested; idempotent cursor-reset replay produced zero duplicates |
| Temporal facts + invalidation | **PROVEN ACTIVE** | port-8501 fact: `valid 14:51 → invalid 14:58` (SUPERSEDED) after the real close; topology BELONGS_TO facts live |
| Episodes / conversational memory | **PROVEN ACTIVE** | post-run consolidation wrote a real episode ("applied the env=battery tag to MyVM…"), retrievable via recent_episodes |
| Compaction (durable SESSION STATE) | **PROVEN ACTIVE** | long battery session's context carries the [SESSION STATE] block rebuilt from durable stores |
| Entity resolution (temporal refs) | **PROVEN ACTIVE** | "the VM I created today" resolved to MyVM in a NEW session (cross-session, run `32071161`) |
| Knowledge corpus / RAG | **PROVEN ACTIVE** | RUNBOOK.md ingested (12 chunks, 7 embedded); live Q cited 5 references and quoted the real Flow A commands |
| Observability (trace evidence) | **PROVEN ACTIVE** | `observation` + `agent_gate` run_events on real runs (UI Agent-Loop tab source); Analysis retrieval-evidence cards live |
| Graphiti failure degradation | **PROVEN ACTIVE** | real dead-key ingest failure: honest `fact_ingest_failed`, cursor unmoved, self-healed after key refresh |
| Multi-LLM neutrality | **PROVEN ACTIVE** (single-vendor env) | binding switch → live consolidation served by `gemini-flash-latest`; schema-requiring calls stayed on the structured-output-capable model (P1 capability-aware routing, explicit not silent). Second-vendor key unavailable in this environment — cross-vendor run BLOCKED BY ENVIRONMENT |
| Org / cloud isolation | **PROVEN ACTIVE** | bob.chen (acme) sees zero northwind facts/resources; AWS-named questions filter facts by cloud attribute |
| Prompt-1 flagship (port close) | **PROVEN ACTIVE** | live open→verify→close→verify: SG rule genuinely removed (required the sentinel-rule template fix below) |
| Frontend evidence surface | **PROVEN ACTIVE** (API level) | SSE analysis cards + run_events + timeline consumed by the existing UI components; rendered strings verified in SSE captures |

## Defects found & fixed during the live battery (all uncommitted, all pinned)

1. **AWS SG inline-ingress gotcha**: with zero inline ingress blocks the provider leaves
   rules unmanaged — "close the last port" planned NO change ("without changing any real
   infrastructure"). Fixed with an always-present self-scoped sentinel rule in
   `aws-ec2/main.tf`; `plan_guard` now permits rule-type deletions under modify (allowlist).
2. **AMI drift replacement**: a port change planned an instance REPLACEMENT the day Amazon
   published a newer AL2023 AMI (caught live by the N-08 plan guard). Day-2 modifies now pin
   the recorded `ami_used`.
3. **Approval-continuation runs skipped the post-run hooks** (the main fact source!) — hook
   added to the continuation path in chat.py.
4. **`EntityEdge.save()` fails re-persisting loaded edges** (embedding lost) — deterministic
   expiry now uses a targeted, group-guarded Cypher update.
5. **`add_episode(uuid=…)` means resume-existing, not create-with-id** — removed.
6. **Router context ran unattributed** — `run_id` threaded so gate/retrieval evidence lands
   on the run.

## Source-of-truth boundaries (enforced, stated in-context)

Every INFRASTRUCTURE MEMORY block carries the header "live cloud is authoritative for
CURRENT state"; CHANGE HISTORY is labeled audit-authoritative; superseded facts render with
`— SUPERSEDED`. Graphiti holds no authority: deleting its namespace loses derived memory
only (rebuildable from `resource_revisions` via the idempotent cursor reset).

## Files

New: `app/intelligence/{__init__,graphiti_layer,facts,pipeline,compaction}.py`,
`tests/test_intelligence.py`. Modified: `agents/memory.py` (build_context façade),
`agents/general.py` (+evidence card, run_id), `agents/knowledge.py` (run_id),
`agents/inventory.py` (temporal narrowing), `api/chat.py` (post-run hooks),
`settings.py` (+2 flags, default on, off = byte-identical rollback), `pyproject.toml`
(graphiti-core==0.29.3 pinned, telemetry disabled).
