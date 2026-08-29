"""Intelligence layer (Prompt 2, 2026-08-17) — the ONE canonical retrieval/context path.

Boundaries (source-of-truth architecture, frozen):
  LIVE CLOUD        current infrastructure reality (cloudops deterministic reads own it)
  POSTGRESQL        durable app state; `resource_revisions` = immutable change history
  EXISTING NEO4J    deterministic topology/provenance (world model + context graph)
  PGVECTOR          message/document semantic retrieval
  GRAPHITI          temporal + semantic contextual memory — NEVER authoritative for infra

Graphiti is provider-neutral by construction: its LLM, embedder, and reranker run through
`app/llm` (purpose-routed via the P1 catalog/bindings — `consolidation`, `retrieval_gate`,
`embeddings`). No vendor SDK is imported here (P1.9 boundary holds).

Deterministic infrastructure facts are ingested from trusted AegisOps events
(resource_revisions) via `facts.py` — no LLM in that path. LLM extraction feeds ONLY the
conversational/narrative episode layer. The audit source of truth stays PostgreSQL.
"""
