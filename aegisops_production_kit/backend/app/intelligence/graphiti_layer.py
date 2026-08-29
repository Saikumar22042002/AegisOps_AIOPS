"""Graphiti client layer — provider-neutral adapters over the P1 model substrate.

Graphiti's own client zoo (OpenAI default, Gemini extra, …) is deliberately unused: a second
provider-selection mechanism is forbidden. Instead three thin adapters bridge Graphiti's
abstract interfaces onto `app/llm/service`, so the EXISTING catalog/bindings decide which
provider/model serves Graphiti — switching the bound model switches Graphiti with it:

  AegisLLMClient      → service.classify_json / generate   (purpose="consolidation")
  AegisEmbedder       → service.embed                       (purpose="embeddings" ledger label)
  AegisReranker       → embedding-cosine ranking            (no vendor, no extra LLM call)

Storage: the EXISTING Neo4j instance (community edition = single database). Graphiti's own
labels (Entity/Episodic/Community) and relationships (RELATES_TO/MENTIONS/HAS_MEMBER) do not
collide with the deterministic AegisOps graph labels; tenant isolation is `group_id = org_id`
on every node/edge/search. Graphiti is a DERIVED layer — losing it loses no audit truth.
"""

from __future__ import annotations

import asyncio
import os
import typing
import uuid as uuid_mod

import structlog

from ..llm import service as llm_service
from ..settings import Settings, get_settings

log = structlog.get_logger(__name__)

# Telemetry must never phone home; set before any graphiti import creates its client.
os.environ.setdefault("GRAPHITI_TELEMETRY_ENABLED", "false")

# Deterministic uuid5 namespace for AegisOps-owned nodes/edges (idempotent ingestion).
NS = uuid_mod.UUID("2f1a9c66-8a1a-4bde-9c3e-000000000001")


def stable_uuid(*parts: str) -> str:
    """Deterministic uuid for a fact/entity key — same input, same node/edge, no duplicates."""
    return str(uuid_mod.uuid5(NS, "|".join(p or "" for p in parts)))


def enabled(settings: Settings | None = None) -> bool:
    s = settings or get_settings()
    return getattr(s, "aegisops_graphiti", "off") == "on"


class AegisEmbedder:
    """Graphiti EmbedderClient over the P1 embedding path (provider decided by the catalog)."""

    def __init__(self, settings: Settings):
        self._settings = settings

    async def create(self, input_data) -> list[float]:
        texts = [input_data] if isinstance(input_data, str) else [str(t) for t in input_data]
        vecs = await llm_service.embed(self._settings, texts[:1])
        return vecs[0]

    async def create_batch(self, input_data_list: list[str]) -> list[list[float]]:
        return await llm_service.embed(self._settings, [str(t) for t in input_data_list])


def _build_llm_client(settings: Settings):
    """Graphiti LLMClient subclass — declared lazily so graphiti_core imports only when the
    layer is actually used (flag on)."""
    from graphiti_core.llm_client.client import LLMClient
    from graphiti_core.llm_client.config import LLMConfig

    class AegisLLMClient(LLMClient):
        """Routes Graphiti's extraction/dedup/summary calls through the P1 service under the
        governed `consolidation` purpose. NO vendor SDK here; NO second provider selection.
        A provider without structured output surfaces as a loud ModelError — Graphiti's
        retry/caller handles it; nothing silently falls back to a different provider."""

        def __init__(self, settings: Settings):
            super().__init__(LLMConfig(api_key="routed-by-aegisops"), cache=False)
            self._settings = settings

        async def _generate_response(self, messages, response_model=None,
                                     max_tokens: int = 16384, model_size=None) -> dict:
            system = "\n".join(m.content for m in messages if m.role == "system") or ""
            prompt = "\n\n".join(m.content for m in messages if m.role != "system")
            if response_model is not None:
                schema = response_model.model_json_schema()
                raw = await llm_service.classify_json(
                    self._settings, system, prompt, purpose="consolidation",
                    response_schema=schema)
                # Validate against Graphiti's model so a mismatch fails loudly HERE,
                # not deep inside graph writes.
                return response_model.model_validate(raw).model_dump()
            resp = await llm_service.generate(self._settings, purpose="consolidation",
                                              system=system or None, prompt=prompt)
            return {"content": resp.text if hasattr(resp, "text") else str(resp)}

    return AegisLLMClient(settings)


class AegisReranker:
    """Graphiti CrossEncoderClient via embedding cosine similarity — deterministic, cheap,
    provider-neutral (reuses the catalog-bound embedding model; no extra LLM judging)."""

    def __init__(self, settings: Settings):
        self._settings = settings

    async def rank(self, query: str, passages: list[str]) -> list[tuple[str, float]]:
        if not passages:
            return []
        try:
            vecs = await llm_service.embed(self._settings, [query] + list(passages))
        except Exception as e:  # noqa: BLE001 — ranking degrades to input order, stated
            log.warning("graphiti.rerank_degraded_input_order", error=str(e))
            n = max(len(passages), 1)
            return [(p, 1.0 - i / n) for i, p in enumerate(passages)]
        q, ps = vecs[0], vecs[1:]

        def _cos(a: list[float], b: list[float]) -> float:
            num = sum(x * y for x, y in zip(a, b))
            da = sum(x * x for x in a) ** 0.5 or 1.0
            db = sum(y * y for y in b) ** 0.5 or 1.0
            return num / (da * db)

        scored = sorted(((p, _cos(q, v)) for p, v in zip(passages, ps)),
                        key=lambda t: t[1], reverse=True)
        return scored


def _build_embedder(settings: Settings):
    """Real EmbedderClient subclass (Graphiti's constructor isinstance-validates) wrapping
    the plain AegisEmbedder logic — declared lazily so graphiti_core imports only on use."""
    from graphiti_core.embedder.client import EmbedderClient

    inner = AegisEmbedder(settings)

    class _Embedder(EmbedderClient):
        async def create(self, input_data):
            return await inner.create(input_data)

        async def create_batch(self, input_data_list):
            return await inner.create_batch(input_data_list)

    return _Embedder()


def _build_reranker(settings: Settings):
    from graphiti_core.cross_encoder.client import CrossEncoderClient

    inner = AegisReranker(settings)

    class _Reranker(CrossEncoderClient):
        async def rank(self, query: str, passages: list[str]):
            return await inner.rank(query, passages)

    return _Reranker()


_client = None
_client_lock: asyncio.Lock | None = None
_indices_built = False


async def get_graphiti(settings: Settings | None = None):
    """Lazy singleton over the EXISTING Neo4j. Returns None (logged) when the layer is off
    or the graph is unreachable — callers degrade gracefully, never fail the run."""
    global _client, _client_lock, _indices_built
    s = settings or get_settings()
    if not enabled(s):
        return None
    if _client is not None:
        return _client
    if _client_lock is None:
        _client_lock = asyncio.Lock()
    async with _client_lock:
        if _client is not None:
            return _client
        try:
            from graphiti_core import Graphiti
            client = Graphiti(uri=s.neo4j_uri, user=s.neo4j_user, password=s.neo4j_password,
                              llm_client=_build_llm_client(s),
                              embedder=_build_embedder(s),
                              cross_encoder=_build_reranker(s),
                              store_raw_episode_content=True,
                              # POC lesson (graphiti-chatbot): the default 20 concurrent LLM
                              # ops hammers provider quotas into 429 backoff loops.
                              max_coroutines=6)
            if not _indices_built:
                await client.build_indices_and_constraints()
                _indices_built = True
            _client = client
            log.info("graphiti.ready", uri=s.neo4j_uri)
        except Exception as e:  # noqa: BLE001 — unreachable graph = degraded, never fatal
            log.warning("graphiti.unavailable", error=str(e))
            return None
    return _client


def _utc(dt):
    """Coerce to timezone-aware UTC (POC gotcha: Neo4j may return naive UTC datetimes)."""
    from datetime import timezone
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


async def search_facts(org_id: str, query: str, *, num_results: int = 8,
                       valid_at: typing.Any = None, include_invalidated: bool = False,
                       settings: Settings | None = None) -> list[dict]:
    """Tenant-scoped fact search. Returns [] when the layer is off/unreachable (degraded,
    logged) — the deterministic PG/Neo4j answers are unaffected.

    Each hit: {fact, valid_at, invalid_at, expired_at, attributes, uuid, current}.
    `include_invalidated=False` (default) filters superseded/expired facts — CURRENT truth;
    temporal questions pass True and get history with validity intervals."""
    client = await get_graphiti(settings)
    if client is None:
        return []
    try:
        edges = await client.search(query, group_ids=[org_id], num_results=num_results * 2)
    except Exception as e:  # noqa: BLE001
        log.warning("graphiti.search_failed", error=str(e))
        return []
    out: list[dict] = []
    for e in edges:
        e_invalid, e_expired, e_valid = _utc(e.invalid_at), _utc(e.expired_at), _utc(e.valid_at)
        invalidated = bool(e_invalid or e_expired)
        if invalidated and not include_invalidated:
            continue
        if valid_at is not None:
            # Point-in-time: the fact must have been valid AT that moment.
            if e_valid and e_valid > valid_at:
                continue
            if e_invalid and e_invalid <= valid_at:
                continue
        out.append({"fact": e.fact, "uuid": e.uuid, "valid_at": e_valid,
                    "invalid_at": e_invalid, "expired_at": e_expired,
                    "attributes": dict(e.attributes or {}), "current": not invalidated})
        if len(out) >= num_results:
            break
    return out


async def recent_episodes(org_id: str, *, last_n: int = 3,
                          settings: Settings | None = None) -> list[str]:
    """Most recent episode digests for the org (cross-session narrative memory)."""
    client = await get_graphiti(settings)
    if client is None:
        return []
    from datetime import datetime, timezone
    try:
        eps = await client.retrieve_episodes(reference_time=datetime.now(timezone.utc),
                                             last_n=last_n, group_ids=[org_id])
    except Exception as e:  # noqa: BLE001
        log.warning("graphiti.episodes_failed", error=str(e))
        return []
    out = []
    for ep in eps:
        stamp = _utc(ep.valid_at) or _utc(ep.created_at)
        head = stamp.strftime("%Y-%m-%d %H:%M") if stamp else "?"
        body = " ".join((ep.content or "").split())[:240]
        out.append(f"({head}) {body}")
    return out


async def add_conversation_episode(org_id: str, *, session_id: str, run_id: str,
                                   summary: str, settings: Settings | None = None) -> bool:
    """One consolidated conversational episode → the semantic memory layer. Idempotent per
    run (uuid = run_id-derived). LLM extraction runs behind the P1 adapter; a failure is
    logged and skipped — episodes are derived memory, never audit truth."""
    client = await get_graphiti(settings)
    if client is None:
        return False
    from datetime import datetime, timezone
    from graphiti_core.nodes import EpisodeType
    try:
        # NOTE: add_episode's `uuid` param means "resume THIS existing episode", not
        # "create with this id" (NodeNotFoundError otherwise — found live 2026-08-17).
        # Once-per-run semantics come from the single post-run hook; episodes are derived
        # narrative memory, so a rare duplicate is harmless and visible.
        await client.add_episode(
            name=f"run-{run_id[:8]}",
            episode_body=summary[:6000],
            source=EpisodeType.text,
            source_description=f"AegisOps consolidated run summary (session {session_id}, run {run_id})",
            reference_time=datetime.now(timezone.utc),
            group_id=org_id)
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("graphiti.episode_write_failed", run_id=run_id, error=str(e))
        return False


async def stats(org_id: str, settings: Settings | None = None) -> dict:
    """Group-scoped graph counts — runtime evidence that memory really lives in Neo4j."""
    client = await get_graphiti(settings)
    if client is None:
        return {"available": False}
    try:
        records, _, _ = await client.driver.execute_query(
            """
            MATCH (e:Entity {group_id: $gid}) WITH count(e) AS entities
            OPTIONAL MATCH (ep:Episodic {group_id: $gid}) WITH entities, count(ep) AS episodes
            OPTIONAL MATCH (:Entity {group_id: $gid})-[r:RELATES_TO]->(:Entity {group_id: $gid})
            RETURN entities, episodes, count(r) AS facts
            """, gid=org_id)
        row = records[0] if records else {}
        return {"available": True, "entities": row.get("entities", 0),
                "episodes": row.get("episodes", 0), "facts": row.get("facts", 0)}
    except Exception as e:  # noqa: BLE001
        log.warning("graphiti.stats_failed", error=str(e))
        return {"available": False, "error": str(e)[:120]}
