# Context Graph — Actual Model, Provenance Reality, and the Graphiti Question

**Date:** 2026-08-16. Evidence: live Cypher against the running Neo4j 5.26 (`aegisops_production_kit-neo4j-1`), the audit chain of real mutations, and code tracing.

## 1. What the graph actually contains (live counts)

Nodes: Context/Trigger/Intent/Agent/Outcome (258 each — one set per run), Run 93, Session 86, Step 65, Evidence 52, Tool 47, Resource 43, Workflow 35, Approval 25, Reasoning 2, Human 1.

Relationships: TRIGGERED_BY / HAS_INTENT / ROUTED_TO (258 each), RESULTED_IN 254, HAS_RUN 93, USED_TOOL / HAS_STEP 65, HAS_EVIDENCE 52, RUNS 35, DEPENDS_ON 26, DECIDED_BY / REQUIRED_APPROVAL 25, NEXT 19, CREATED / PROVISIONED 15.

## 2. The ideal model vs what exists

| Ideal edge | Exists? | Actual form |
|---|---|---|
| User → requested → Run | PARTIAL | Session-[:HAS_RUN]->Run + per-run Trigger/Context; Human node exists only for approvals |
| Run → created/modified/destroyed → Resource | PARTIAL | `(:Resource)-[:CREATED]->(:Run)` — but **every touching run gets a CREATED edge**: MySource has 3 CREATED edges (create 53b76da7, port-add 724e400b, no-op removal 7176806f). No MODIFIED/DESTROYED edge types observed; destroy is `world_model.mark_destroyed` (status flip) |
| Resource → belongs_to → Cloud | NO (as edge) | cloud is a property, not a node |
| Resource → located_in → Region/VPC/Subnet | PARTIAL | generic `DEPENDS_ON`: MySource → sg-09ac316b05c7b9eca / vpc-02cc610d312254a3d / subnet-0a5905e7220717194 (real topology, untyped semantics) |
| Resource → attached_to → Resource | PARTIAL | same DEPENDS_ON |
| Change → affected → Resource, Change → caused_by → Run | **MISSING** | no Change node exists; a mutation's before/after is nowhere in the graph |
| Run → originated_from → Conversation | YES | Session-[:HAS_RUN]->Run; inventory rows also carry session_id/run_id |
| Approval provenance | YES | `(:Approval)-[:DECIDED_BY]->(:Human {name:"maya.okafor", role:"Platform Admin"})` + REQUIRED_APPROVAL; mirrored immutably in PG `approvals` (actor_user, actor_role, rationale, ts) |

## 3. Is history preserved or overwritten? (tested with the port-8501 chain)

- **PostgreSQL `resources`:** OVERWRITTEN. Upsert keyed org+workspace+name (`inventory.py:127`); `attributes`, `inputs`, `run_id`, `session_id` all reflect the LAST touch. After the port-add, the pre-change state (`ingress_ports: []`) survives only inside the creating run's `outcome` JSON — retrievable forensically, not modeled.
- **Neo4j:** ACCRETES but mislabels — each apply adds another CREATED edge; there is no diff payload, no valid-from/valid-to, no Change node. You can see *that* runs touched a resource and *when* (edge/run timestamps), not *what changed*.
- **PG `runs` + `run_steps` + `approvals`:** append-only and complete per run (23 runs for the audit day, 151 steps, immutable approval rows). The raw material for temporal answers EXISTS here.
- **Answer path:** none of this history is reachable conversationally (q3/q4/q7 all failed — see INTELLIGENCE_LAYER_AUDIT.md §4).

**Verdict: durable knowledge is partially created (topology, approvals, run provenance) but change history is not modeled, and nothing on the answer path reads what is there.** The mutation contract (user + workspace + resource + action + run + timestamp + before/after + reason + result + cloud + region) is roughly 60% persisted (scattered across PG JSON) and 0% queryable.

## 4. Graph reads on the live path (exhaustive)

1. `cloudops.py:1078-1083` — resource card provenance line ("Provisioned by run … in session … (context graph)"). Note: cites the resource node's current run pointer = last touch, not creation.
2. `cloudops.py:1107` / `_world_model_impact_check` — destroy/modify impact: `world_model.impact_of` returns dependents (DEPENDS_ON traversal). Real safety use.
3. `investigation.py:155` — SRE investigations.

Nothing else reads Neo4j. It is a write-mostly archive.

## 5. Graphiti (getzep/graphiti) assessment

Researched release: **graphiti-core v0.29.3** (2026-07-27), Apache-2.0, Python ≥3.10, ~30k stars, active. Bi-temporal knowledge-graph framework: episodes → LLM-extracted entities/edges; edges carry `valid_at`/`invalid_at` (world time) + `created_at`/`expired_at` (system time); contradictions **invalidate rather than delete**; hybrid retrieval (semantic + BM25 + graph traversal) with RRF/MMR/cross-encoder rerankers; sub-second search (no LLM at query time); `SearchFilters` supports point-in-time date-range operators natively.

**Compatibility with our stack:**
- Neo4j 5.26+ required — exactly our version. Best isolation: **separate Neo4j database on the same instance** (`Neo4jDriver(uri, database="graphiti")` since v0.17) — zero namespace collision with our labels. Same-database coexistence would work (its labels `Entity/Episodic/Community`, rels `RELATES_TO/MENTIONS/HAS_MEMBER` don't collide) but is unsupported territory, and custom entity types named `Resource`/`Run` would collide with our label strings — use the separate database.
- **Gemini natively supported** (`graphiti-core[google-genai]`: GeminiClient, GeminiEmbedder, GeminiRerankerClient) — a Gemini-only stack works; pass our `gemini-3.5-flash` / `gemini-embedding-001` explicitly.
- No conflict with PG/pgvector/Redis — Graphiti stores its own embeddings inside Neo4j properties (a second embedding space to manage, not a conflict).
- Ingestion: LLM-heavy (extraction, dedup, invalidation, summarization), seconds per episode, ~50 episodes/min — must run async off the Redis stream, never in-request. `add_triplet(EntityNode, EntityEdge, EntityNode)` bypasses the LLM entirely for deterministic facts. `add_episode(uuid=…)` gives idempotency for outbox replay.

**Replace / augment / coexist:** **COEXIST.** Do not replace the existing graph: our provenance (approvals, runs, DEPENDS_ON topology) must stay deterministic — audit facts produced by LLM extraction are not audit-grade (nondeterministic extraction, LLM-judged dedup, extracted `valid_at`). PostgreSQL remains the system of record; the existing Neo4j graph remains the deterministic provenance/topology layer; Graphiti becomes the **temporal/semantic memory layer** that finally answers "what changed / when / what was true before X".

**What Graphiti would materially fix** (all NOT PROVEN or MISSING today): temporal facts with validity intervals, invalidation-not-overwrite (our port-add/remove pair maps exactly to a fact being created then invalidated), episode provenance (every fact → source episode → our run_id), point-in-time queries, entity resolution across name variants, hybrid retrieval over history. What it does NOT fix: live-cloud reconciliation, verb inversion, dependency binding — those are AegisOps code defects.

## 6. Safe POC design (isolated — no production data modified)

**Isolation:** new Neo4j database `graphitipoc` on the existing instance (`CREATE DATABASE graphitipoc` — Neo4j 5 multi-db; our app never opens it). Alternatively a throwaway Neo4j container. Pin `graphiti-core==0.29.3`, `GRAPHITI_TELEMETRY_ENABLED=false`. Read-only access to production stores; writes go only to `graphitipoc`.

**Feed (replay, don't re-execute):** the audit chain is already recorded — replay it from PG `runs.outcome` JSON as deterministic facts + JSON episodes with `reference_time = run.created_at` and run_id embedded:

1. `add_triplet`: (audit-net:Vpc) −[CREATED_BY]→ (run e066b76d); (MySource:Ec2) −[PLACED_IN]→ (subnet/vpc); key facts as typed edges.
2. `add_episode` (EpisodeType.json) per mutation: create MySource (ingress []), open 8501 (ingress [8501]), attempted close 8501 (no-op — episode text states intent AND result honestly).
3. Custom Pydantic entity types: `CloudResource`, `Run`, `PortRule`; edge types `Provisioned`, `OpenedPort`, `ClosedPort` with `edge_type_map` constraints.

**Acceptance questions** (must beat today's proven failures):
- "What ports did I open on MySource and when?" → fact with `valid_at` ≈ 17:04 UTC.
- "What was MySource's configuration before the port change?" → point-in-time `SearchFilters(valid_at ≤ T)`.
- "What changed on 2026-08-16 between 16:30 and 17:30?" → `created_at` range.
- "Which resources depend on audit-net?" → traversal.
- Negative control: "was port 8501 ever removed?" → must answer no/attempted-but-not-applied (episode 3), not hallucinate a removal.

**Measure:** ingestion cost (Gemini tokens per episode — `token_tracker`), latency, extraction fidelity vs the known ground truth (we have exact expected answers), search latency. **Go/no-go:** promote only if extraction fidelity on the deterministic replay is 100% for `add_triplet` facts and acceptable for episodes, with per-mutation cost within the LLM budget guardrail.

**Production shape if promoted:** outbox pattern — after every applied run, enqueue (Redis) an episode job keyed by run_id (idempotent via `add_episode(uuid=run_id)`); `add_triplet` for the audit-grade skeleton, episodes for narrative; answer-path integration = a new deterministic "history" intent that calls `graphiti.search_()` with temporal filters and cites run IDs.
