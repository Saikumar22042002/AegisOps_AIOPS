"""World Model (D3) — the org-scoped live graph of what AegisOps manages.

Neo4j holds one `Resource` node per managed resource (shared with the per-run context graph —
same `provider_id` merge key, enriched here with `org_id`, Terraform state refs and dependency
edges) plus `DEPENDS_ON` relationships extracted from the resource's REAL inputs/outputs (never
LLM-inferred). This is what answers "what depends on this?" before a destroy, and what the
drift/orphan reconciliation engine annotates.

Every query is org-scoped (S0): the world of org A does not exist for org B.
"""

from __future__ import annotations

import json
from typing import Any

import structlog

from ..security.redaction import redact_dict
from .neo4j import get_driver

log = structlog.get_logger(__name__)

# Which input/attribute keys name a parent resource, and what kind of parent that is. These are
# the dependency facts our approved modules actually carry — extraction is a pure lookup, so an
# edge can never be hallucinated.
_DEP_KEYS: list[tuple[str, str]] = [
    ("vpc_id", "vpc"),
    ("subnet_id", "subnet"),
    ("subnet_ids", "subnet"),
    ("security_group_ids", "security_group"),
    ("security_group_id", "security_group"),
    ("resource_group", "resource_group"),
    ("resource_group_name", "resource_group"),
    ("network", "network"),
    ("cluster_name", "cluster"),
]


def dependencies_from(payload: dict) -> list[dict[str, str]]:
    """Parent references from a resource's validated inputs + apply outputs (pure).

    Returns [{"ref": <provider id or name>, "kind": <vpc|subnet|…>}], de-duplicated, skipping
    empty/placeholder values. `payload` is the same dict `inventory.inventory_payload` builds.
    """
    sources: list[dict] = [payload.get("inputs") or {}, payload.get("attributes") or {}]
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for src in sources:
        for key, kind in _DEP_KEYS:
            val = src.get(key)
            refs = val if isinstance(val, list) else [val]
            for ref in refs:
                if not ref or not isinstance(ref, str):
                    continue
                ref = ref.strip()
                if not ref or ref.lower() in {"default", "none", "auto"} or ref in seen:
                    continue
                seen.add(ref)
                out.append({"ref": ref, "kind": kind})
    return out


async def _run(cypher: str, **params: Any) -> list[dict[str, Any]]:
    driver = get_driver()
    async with driver.session() as session:
        result = await session.run(cypher, **params)
        return [r.data() async for r in result]


async def ensure_schema() -> None:
    """Idempotent constraints — one Resource node per provider_id."""
    await _run("CREATE CONSTRAINT resource_pid IF NOT EXISTS "
               "FOR (r:Resource) REQUIRE r.provider_id IS UNIQUE")


async def upsert_resource(org_id: str, payload: dict) -> None:
    """MERGE the resource into the world model (same node the context graph writes, enriched
    with org scope + Terraform state refs) and its DEPENDS_ON edges from real inputs/outputs."""
    pid = payload.get("provider_id") or f"{payload.get('cloud')}:{payload.get('name')}"
    await _run(
        """
        MERGE (res:Resource {provider_id:$pid})
          SET res.org_id=$org_id, res.name=$name, res.cloud=$cloud, res.type=$rtype,
              res.region=$region, res.status='active', res.workspace=$workspace,
              res.state_workspace=$state_workspace, res.attributes=$attrs,
              res.updated_at=timestamp()
        FOREACH (_ IN CASE WHEN $run_id IS NULL THEN [] ELSE [1] END |
          MERGE (run:Run {id:$run_id})
          MERGE (run)-[:CREATED]->(res)
          FOREACH (__ IN CASE WHEN $session_id IS NULL THEN [] ELSE [1] END |
            MERGE (sess:Session {id:$session_id})
            MERGE (sess)-[:HAS_RUN]->(run)))
        """,
        pid=pid, org_id=org_id, name=payload.get("name"), cloud=payload.get("cloud"),
        rtype=payload.get("resource_type"), region=payload.get("region"),
        workspace=payload.get("workspace"), state_workspace=payload.get("state_workspace"),
        attrs=json.dumps(redact_dict(payload.get("attributes") or {})),
        run_id=payload.get("run_id"), session_id=payload.get("session_id"),
    )
    deps = dependencies_from(payload)
    for dep in deps:
        # Parents may be resources we did not create (an existing VPC): MERGE a stub node so the
        # edge is real either way; ON CREATE only, so a richer managed node is never overwritten.
        await _run(
            """
            MATCH (child:Resource {provider_id:$child})
            MERGE (parent:Resource {provider_id:$parent})
              ON CREATE SET parent.org_id=$org_id, parent.type=$kind, parent.status='external',
                            parent.name=$parent, parent.updated_at=timestamp()
            MERGE (child)-[d:DEPENDS_ON]->(parent)
              SET d.kind=$kind
            """,
            child=pid, parent=dep["ref"], kind=dep["kind"], org_id=org_id,
        )
    if deps:
        log.info("world_model.dependencies_recorded", resource=pid,
                 parents=[d["ref"] for d in deps])


async def impact_of(org_id: str, *, provider_id: str | None = None,
                     name: str | None = None) -> list[dict[str, Any]]:
    """ACTIVE resources that depend on the given one — the destroy-gating question.

    Matches the target by provider_id or name (org-scoped); returns each dependent's
    name/type/provider_id and the dependency kind.
    """
    rows = await _run(
        """
        MATCH (dep:Resource)-[d:DEPENDS_ON]->(target:Resource)
        WHERE dep.org_id=$org_id AND dep.status='active'
          AND (($pid IS NOT NULL AND target.provider_id=$pid)
               OR ($name IS NOT NULL AND target.name=$name))
        RETURN dep.name AS name, dep.type AS type, dep.provider_id AS provider_id,
               d.kind AS kind
        """,
        org_id=org_id, pid=provider_id, name=name,
    )
    return rows


async def mark_destroyed(org_id: str, *, provider_id: str | None = None,
                          name: str | None = None) -> None:
    await _run(
        """
        MATCH (res:Resource {org_id:$org_id})
        WHERE ($pid IS NOT NULL AND res.provider_id=$pid)
           OR ($name IS NOT NULL AND res.name=$name)
        SET res.status='destroyed', res.drift=null, res.drift_detail=null,
            res.updated_at=timestamp()
        """,
        org_id=org_id, pid=provider_id, name=name,
    )


async def set_drift(org_id: str, ref: str, detail: str) -> None:
    """Annotate a resource with a detected drift/orphan finding (idempotent)."""
    await _run(
        """
        MATCH (res:Resource {org_id:$org_id})
        WHERE res.provider_id=$ref OR res.name=$ref
        SET res.drift=true, res.drift_detail=$detail, res.drift_at=timestamp()
        """,
        org_id=org_id, ref=ref, detail=detail[:500],
    )


async def list_active(org_id: str) -> list[dict[str, Any]]:
    return await _run(
        """
        MATCH (res:Resource {org_id:$org_id})
        WHERE res.status='active'
        RETURN res.name AS name, res.type AS type, res.provider_id AS provider_id,
               res.cloud AS cloud, res.drift AS drift
        ORDER BY res.updated_at DESC
        """,
        org_id=org_id,
    )
