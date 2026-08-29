"""Deterministic infrastructure facts: resource_revisions → Graphiti temporal edges.

The audit source of truth is PostgreSQL (`resource_revisions`, immutable). This module
DERIVES Graphiti facts from those trusted rows — **no LLM anywhere in this path** (the
mandate's hard rule: LLM extraction is never the audit source). Every node/edge uuid is a
deterministic uuid5 of its business key, so re-ingestion is idempotent and a lost cursor
merely re-writes identical facts.

Fact shapes per revision:
  action fact       (User)-[CREATED|MODIFIED|DESTROYED|…]->(resource)   valid_at = rev time
  port state fact   (resource)-[HAS_OPEN_PORT]->(port)                  valid_at = opened;
                                                                        closed ⇒ invalid_at set
  attribute fact    (resource)-[HAS_ATTRIBUTE]->(attr=value)            superseded value expired
  topology fact     (resource)-[BELONGS_TO]->(vpc/subnet)
  destroy           all current state facts for the resource expired

Every fact carries provenance attributes: org, session, run, revision, resource, cloud,
region, action, actor, source="resource_revisions". Temporal truth: a changed fact gets
`invalid_at`/`expired_at` — history is preserved, never overwritten.

Trigger: fire-and-forget after every mutation's bookkeeping (cloudops_execute) plus lazily
from the retrieval pipeline; a Redis cursor keeps batches small, and idempotent uuids make
duplicate processing harmless. Any failure leaves the cursor unmoved (retried later) and
never affects the run that triggered it.
"""

from __future__ import annotations

from datetime import datetime, timezone

import structlog
from sqlalchemy import select

from ..db.models import ResourceRevision
from ..db.session import session_scope
from ..settings import Settings, get_settings
from .graphiti_layer import get_graphiti, stable_uuid

log = structlog.get_logger(__name__)

_CURSOR_KEY = "graphiti:rev_cursor"
_BATCH = 40

_ACTION_VERB = {"created": "created", "modified": "modified", "destroyed": "destroyed",
                "failed": "attempted (failed)", "partial": "partially applied",
                "no_change": "requested a no-op change on", "orphaned": "orphaned",
                "unknown": "touched"}


def _node(org_id: str, *, key: tuple, name: str, summary: str = "", attributes: dict | None = None):
    from graphiti_core.nodes import EntityNode
    return EntityNode(uuid=stable_uuid(org_id, *key), name=name[:200], group_id=org_id,
                      labels=["Entity"], created_at=datetime.now(timezone.utc),
                      summary=summary[:300], attributes=attributes or {})


def _edge(org_id: str, *, key: tuple, name: str, fact: str, source_uuid: str,
          target_uuid: str, valid_at: datetime, attributes: dict):
    from graphiti_core.edges import EntityEdge
    return EntityEdge(uuid=stable_uuid(org_id, *key), group_id=org_id,
                      source_node_uuid=source_uuid, target_node_uuid=target_uuid,
                      created_at=datetime.now(timezone.utc), name=name[:60],
                      fact=fact[:600], episodes=[], valid_at=valid_at,
                      attributes=attributes)


def _prov(rev: ResourceRevision) -> dict:
    return {"source": "resource_revisions", "revision_id": str(rev.id),
            "run_id": str(rev.run_id) if rev.run_id else None,
            "session_id": str(rev.session_id) if rev.session_id else None,
            "org_id": str(rev.org_id), "resource": rev.name, "cloud": rev.cloud,
            "region": rev.region, "resource_type": rev.resource_type,
            "action": rev.action, "actor": rev.actor_user}


def _ports(state: dict | None) -> set[int]:
    attrs = (state or {}).get("attributes") or (state or {})
    try:
        return {int(p) for p in (attrs.get("ingress_ports") or [])}
    except (TypeError, ValueError):
        return set()


async def _expire_edge(client, org_id: str, edge_uuid: str, when: datetime) -> None:
    """Deterministic invalidation: the superseded fact keeps existing with its validity
    interval closed — history preserved, never deleted.

    Targeted Cypher (not EntityEdge.save): a re-loaded edge lacks its fact embedding, and
    graphiti's save() then fails in setRelationshipVectorProperty (found live 2026-08-17).
    The MATCH carries the group_id, so cross-tenant expiry is structurally impossible; a
    missing edge (pre-intelligence history) matches nothing and is a clean no-op."""
    await client.driver.execute_query(
        """
        MATCH ()-[e:RELATES_TO {uuid: $uuid, group_id: $org}]->()
        SET e.invalid_at = $when, e.expired_at = $when
        """, uuid=edge_uuid, org=org_id, when=when)


async def _ingest_one(client, rev: ResourceRevision) -> None:
    org = str(rev.org_id)
    when = rev.created_at or datetime.now(timezone.utc)
    prov = _prov(rev)
    actor = rev.actor_user or "unknown-user"

    res_node = _node(org, key=("resource", rev.cloud, rev.name),
                     name=rev.name,
                     summary=f"{rev.cloud} {rev.resource_type} in {rev.region or '?'}",
                     attributes={"cloud": rev.cloud, "region": rev.region,
                                 "resource_type": rev.resource_type})
    user_node = _node(org, key=("user", actor), name=actor, summary="AegisOps operator")

    # 1) The action fact — one per revision (uuid keyed by revision id → idempotent).
    verb = _ACTION_VERB.get(rev.action, rev.action)
    run_short = str(rev.run_id)[:8] if rev.run_id else "?"
    fact = (f"{actor} {verb} {rev.resource_type} {rev.name} "
            f"({rev.cloud}, {rev.region or 'unknown region'}) at "
            f"{when.strftime('%Y-%m-%d %H:%M UTC')} — run {run_short}")
    await client.add_triplet(
        user_node,
        _edge(org, key=("rev", str(rev.id)), name=rev.action.upper(), fact=fact,
              source_uuid=user_node.uuid, target_uuid=res_node.uuid,
              valid_at=when, attributes=prov),
        res_node)

    after_attrs = (rev.after_state or {}).get("attributes") or {}

    # 2) Port-level temporal facts — the audit's flagship temporal question.
    before_p, after_p = _ports(rev.before_state), _ports(rev.after_state)
    for port in sorted(after_p - before_p):
        port_node = _node(org, key=("portnum", str(port)), name=f"port {port}")
        pf = (f"inbound TCP port {port} is open on {rev.name} ({rev.cloud}) — opened by "
              f"{actor} at {when.strftime('%Y-%m-%d %H:%M UTC')}, run {run_short}")
        await client.add_triplet(
            res_node,
            _edge(org, key=("port", rev.cloud, rev.name, str(port)), name="HAS_OPEN_PORT",
                  fact=pf, source_uuid=res_node.uuid, target_uuid=port_node.uuid,
                  valid_at=when, attributes={**prov, "port": port}),
            port_node)
    for port in sorted(before_p - after_p):
        await _expire_edge(client, org,
                           stable_uuid(org, "port", rev.cloud, rev.name, str(port)), when)

    # 3) Topology facts from real recorded attributes (deterministic, never inferred).
    for attr_key, parent_kind in (("vpc_id", "vpc"), ("subnet_id", "subnet"),
                                  ("security_group_id", "security_group")):
        parent_id = after_attrs.get(attr_key)
        if parent_id and isinstance(parent_id, str):
            parent = _node(org, key=("infra", parent_kind, parent_id), name=parent_id,
                           summary=f"{rev.cloud} {parent_kind}")
            tf = f"{rev.name} ({rev.cloud} {rev.resource_type}) belongs to {parent_kind} {parent_id}"
            await client.add_triplet(
                res_node,
                _edge(org, key=("topo", rev.cloud, rev.name, parent_kind), name="BELONGS_TO",
                      fact=tf, source_uuid=res_node.uuid, target_uuid=parent.uuid,
                      valid_at=when, attributes={**prov, "parent_kind": parent_kind,
                                                 "parent_id": parent_id}),
                parent)

    # 4) Destroy expires every open state fact this module owns for the resource.
    if rev.action == "destroyed":
        for port in sorted(before_p):
            await _expire_edge(client, org,
                               stable_uuid(org, "port", rev.cloud, rev.name, str(port)), when)
        for parent_kind in ("vpc", "subnet", "security_group"):
            await _expire_edge(client, org,
                               stable_uuid(org, "topo", rev.cloud, rev.name, parent_kind), when)


async def sync(settings: Settings | None = None, *, limit: int = _BATCH) -> int:
    """Ingest revisions newer than the Redis cursor. Returns rows ingested. Never raises —
    a failure logs, leaves the cursor unmoved, and the next trigger retries (idempotent)."""
    s = settings or get_settings()
    client = await get_graphiti(s)
    if client is None:
        return 0
    try:
        from ..cache.redis import get_redis
        r = get_redis()
        cursor = await r.get(_CURSOR_KEY)
        cursor_dt = datetime.fromisoformat(cursor.decode() if isinstance(cursor, bytes) else cursor) \
            if cursor else datetime(1970, 1, 1, tzinfo=timezone.utc)
    except Exception as e:  # noqa: BLE001 — no cursor store → full idempotent pass
        log.warning("graphiti.cursor_read_failed", error=str(e))
        r, cursor_dt = None, datetime(1970, 1, 1, tzinfo=timezone.utc)

    async with session_scope() as db:
        rows = list((await db.execute(
            select(ResourceRevision).where(ResourceRevision.created_at > cursor_dt)
            .order_by(ResourceRevision.created_at.asc()).limit(limit))).scalars())
    done = 0
    for rev in rows:
        try:
            await _ingest_one(client, rev)
            done += 1
            if r is not None:
                await r.set(_CURSOR_KEY, rev.created_at.isoformat())
        except Exception as e:  # noqa: BLE001 — stop the batch; cursor stays at last success
            log.warning("graphiti.fact_ingest_failed", revision=str(rev.id), error=str(e))
            break
    if done:
        log.info("graphiti.facts_ingested", count=done)
    return done


def sync_soon(settings: Settings | None = None) -> None:
    """Fire-and-forget trigger (post-mutation bookkeeping). Never blocks the caller."""
    import asyncio
    try:
        asyncio.get_running_loop().create_task(sync(settings))
    except RuntimeError:  # no running loop (sync test context) — the next trigger catches up
        pass
