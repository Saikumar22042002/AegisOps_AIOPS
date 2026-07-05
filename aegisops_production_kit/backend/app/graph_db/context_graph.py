"""Context graph (Neo4j) — one graph per SR/Incident.

Implements the node/relationship model from 01_REQUIREMENTS §3.12: Context, Trigger, Agent,
Intent, Workflow, Step, Action, Approval, Human, Tool, Reasoning, Evidence, Outcome, Feedback.
Records ordered steps, approvals (who/when), tool used, status, errors/retries, outcomes,
resolution. Sensitive fields are tokenized via redaction. Closed contexts are immutable
(writes are refused once closed). Supports resume from the last successful step.
"""

from __future__ import annotations

import json
from typing import Any

import structlog

from ..security.redaction import redact, redact_dict
from .neo4j import get_driver

log = structlog.get_logger(__name__)


class ContextGraphError(Exception):
    pass


class ContextGraph:
    def __init__(self, context_id: str, org_id: str) -> None:
        self.context_id = context_id
        self.org_id = org_id

    async def _write(self, cypher: str, **params: Any) -> list[dict[str, Any]]:
        driver = get_driver()
        async with driver.session() as session:
            result = await session.run(cypher, **params)
            return [r.data() async for r in result]

    async def _read(self, cypher: str, **params: Any) -> list[dict[str, Any]]:
        return await self._write(cypher, **params)

    async def _ensure_open(self) -> None:
        rows = await self._read(
            "MATCH (c:Context {id:$id}) RETURN c.closed AS closed", id=self.context_id
        )
        if rows and rows[0].get("closed"):
            raise ContextGraphError(f"Context {self.context_id} is closed and immutable")

    async def create(self, *, trigger: str, snow_id: str | None = None, env: str | None = None,
                     trace_id: str | None = None) -> None:
        await self._write(
            """
            MERGE (c:Context {id:$id})
              ON CREATE SET c.org_id=$org, c.created_at=timestamp(), c.closed=false
            SET c.snow_id=$snow, c.env=$env, c.trace_id=$trace
            MERGE (t:Trigger {context_id:$id})
              SET t.description=$trigger
            MERGE (c)-[:TRIGGERED_BY]->(t)
            """,
            id=self.context_id, org=self.org_id, snow=snow_id, env=env,
            trace=trace_id, trigger=redact(trigger),
        )

    async def set_intent(self, *, intent: str, confidence: float, reason: str, domain: str) -> None:
        await self._ensure_open()
        await self._write(
            """
            MATCH (c:Context {id:$id})
            MERGE (i:Intent {context_id:$id})
              SET i.intent=$intent, i.confidence=$conf, i.reason=$reason, i.domain=$domain
            MERGE (c)-[:HAS_INTENT]->(i)
            MERGE (a:Agent {context_id:$id, type:$domain})
            MERGE (c)-[:ROUTED_TO]->(a)
            """,
            id=self.context_id, intent=intent, conf=confidence, reason=redact(reason), domain=domain,
        )

    async def set_workflow(self, *, workflow: str, version: str, template: str, inputs: dict) -> None:
        await self._ensure_open()
        await self._write(
            """
            MATCH (c:Context {id:$id})
            MERGE (w:Workflow {context_id:$id})
              SET w.name=$wf, w.version=$ver, w.template=$tmpl, w.inputs=$inputs
            MERGE (c)-[:RUNS]->(w)
            """,
            id=self.context_id, wf=workflow, ver=version, tmpl=template,
            inputs=json.dumps(redact_dict(inputs or {})),
        )

    async def add_step(self, *, order: int, name: str, agent: str, tool: str | None,
                       status: str = "running", human_vs_auto: str = "auto") -> None:
        await self._ensure_open()
        await self._write(
            """
            MATCH (c:Context {id:$id})
            MERGE (s:Step {context_id:$id, order:$order})
              SET s.name=$name, s.agent=$agent, s.tool=$tool, s.status=$status,
                  s.human_vs_auto=$hva, s.started_at=timestamp()
            MERGE (c)-[:HAS_STEP]->(s)
            WITH s
            OPTIONAL MATCH (p:Step {context_id:$cid, order:$prev})
            FOREACH (_ IN CASE WHEN p IS NULL THEN [] ELSE [1] END | MERGE (p)-[:NEXT]->(s))
            FOREACH (_ IN CASE WHEN $tool IS NULL THEN [] ELSE [1] END |
              MERGE (t:Tool {context_id:$id, name:$tool}) MERGE (s)-[:USED_TOOL]->(t))
            """,
            id=self.context_id, cid=self.context_id, order=order, prev=order - 1,
            name=name, agent=agent, tool=tool, status=status, hva=human_vs_auto,
        )

    async def update_step(self, *, order: int, status: str, error: str | None = None,
                          retries: int = 0, result: dict | None = None) -> None:
        await self._ensure_open()
        await self._write(
            """
            MATCH (s:Step {context_id:$id, order:$order})
            SET s.status=$status, s.error=$error, s.retries=$retries,
                s.ended_at=timestamp(), s.result=$result
            """,
            id=self.context_id, order=order, status=status,
            error=redact(error) if error else None, retries=retries,
            result=json.dumps(redact_dict(result or {})),
        )

    async def add_reasoning(self, *, step_order: int | None, summary: str) -> None:
        await self._ensure_open()
        await self._write(
            """
            MATCH (c:Context {id:$id})
            CREATE (r:Reasoning {context_id:$id, summary:$summary, at:timestamp()})
            MERGE (c)-[:HAS_REASONING]->(r)
            """,
            id=self.context_id, summary=redact(summary),
        )

    async def add_resource(self, *, name: str, cloud: str, resource_type: str, provider_id: str | None,
                           region: str | None, run_id: str | None, session_id: str | None,
                           attributes: dict | None = None) -> None:
        """Record a provisioned resource in the graph with resource ↔ run ↔ session relationships.

        (Context)-[:PROVISIONED]->(Resource); (Run)-[:CREATED]->(Resource); (Session)-[:HAS_RUN]->(Run).
        Sensitive attributes are redacted. Facts here mirror the DB inventory (never LLM-inferred).
        """
        await self._ensure_open()
        await self._write(
            """
            MATCH (c:Context {id:$id})
            MERGE (res:Resource {provider_id:$pid})
              SET res.name=$name, res.cloud=$cloud, res.type=$rtype, res.region=$region,
                  res.context_id=$id, res.status='active', res.attributes=$attrs, res.updated_at=timestamp()
            MERGE (c)-[:PROVISIONED]->(res)
            FOREACH (_ IN CASE WHEN $run_id IS NULL THEN [] ELSE [1] END |
              MERGE (run:Run {id:$run_id})
              MERGE (run)-[:CREATED]->(res)
              FOREACH (__ IN CASE WHEN $session_id IS NULL THEN [] ELSE [1] END |
                MERGE (sess:Session {id:$session_id})
                MERGE (sess)-[:HAS_RUN]->(run)))
            """,
            id=self.context_id, pid=provider_id or f"{cloud}:{name}", name=name, cloud=cloud,
            rtype=resource_type, region=region, run_id=run_id, session_id=session_id,
            attrs=json.dumps(redact_dict(attributes or {})),
        )

    async def add_evidence(self, *, kind: str, ref: str, detail: dict | None = None) -> None:
        await self._ensure_open()
        await self._write(
            """
            MATCH (c:Context {id:$id})
            CREATE (e:Evidence {context_id:$id, kind:$kind, ref:$ref, detail:$detail, at:timestamp()})
            MERGE (c)-[:HAS_EVIDENCE]->(e)
            """,
            id=self.context_id, kind=kind, ref=ref, detail=json.dumps(redact_dict(detail or {})),
        )

    async def add_approval(self, *, decision: str, actor: str, role: str, rationale: str | None = None) -> None:
        await self._ensure_open()
        await self._write(
            """
            MATCH (c:Context {id:$id})
            CREATE (a:Approval {context_id:$id, decision:$decision, rationale:$rationale, at:timestamp()})
            MERGE (h:Human {name:$actor}) SET h.role=$role
            MERGE (c)-[:REQUIRED_APPROVAL]->(a)
            MERGE (a)-[:DECIDED_BY]->(h)
            """,
            id=self.context_id, decision=decision, actor=actor, role=role,
            rationale=redact(rationale) if rationale else None,
        )

    async def set_outcome(self, *, status: str, summary: str, rollback: str | None = None) -> None:
        await self._ensure_open()
        await self._write(
            """
            MATCH (c:Context {id:$id})
            MERGE (o:Outcome {context_id:$id})
              SET o.status=$status, o.summary=$summary, o.rollback=$rollback, o.at=timestamp()
            MERGE (c)-[:RESULTED_IN]->(o)
            """,
            id=self.context_id, status=status, summary=redact(summary),
            rollback=redact(rollback) if rollback else None,
        )

    async def add_feedback(self, *, value: str, comment: str | None, sensitive: bool) -> None:
        # Feedback can be recorded even on closed contexts (post-hoc training signal).
        await self._write(
            """
            MATCH (c:Context {id:$id})
            CREATE (f:Feedback {context_id:$id, value:$value, comment:$comment, sensitive:$sensitive, at:timestamp()})
            MERGE (c)-[:HAS_FEEDBACK]->(f)
            """,
            id=self.context_id, value=value,
            comment=redact(comment) if comment and not sensitive else (None if sensitive else comment),
            sensitive=sensitive,
        )

    async def last_successful_step(self) -> int:
        rows = await self._read(
            "MATCH (s:Step {context_id:$id, status:'done'}) RETURN max(s.order) AS last",
            id=self.context_id,
        )
        return int(rows[0]["last"]) if rows and rows[0]["last"] is not None else -1

    async def close(self, *, resolution: str) -> None:
        await self._write(
            """
            MATCH (c:Context {id:$id})
            SET c.closed=true, c.resolution=$resolution, c.closed_at=timestamp()
            """,
            id=self.context_id, resolution=redact(resolution),
        )


async def resource_provenance(*, provider_id: str | None = None, name: str | None = None) -> dict | None:
    """Global graph read: a resource's provenance (context/run/session) + relationships.

    Used to enrich a day-2 read with where a resource came from — read from the graph, not inferred.
    """
    driver = get_driver()
    async with driver.session() as session:
        result = await session.run(
            """
            MATCH (res:Resource)
            WHERE ($pid IS NOT NULL AND res.provider_id=$pid) OR ($name IS NOT NULL AND res.name=$name)
            OPTIONAL MATCH (run:Run)-[:CREATED]->(res)
            OPTIONAL MATCH (sess:Session)-[:HAS_RUN]->(run)
            OPTIONAL MATCH (c:Context)-[:PROVISIONED]->(res)
            RETURN res.name AS name, res.provider_id AS provider_id, res.status AS status,
                   run.id AS run_id, sess.id AS session_id, c.id AS context_id
            LIMIT 1
            """,
            pid=provider_id, name=name,
        )
        rows = [r.data() async for r in result]
        return rows[0] if rows else None


async def mark_resource_destroyed_graph(*, name: str | None = None, provider_id: str | None = None) -> None:
    """Global graph write: mark a resource destroyed (across whatever context created it)."""
    driver = get_driver()
    async with driver.session() as session:
        await session.run(
            """
            MATCH (res:Resource)
            WHERE ($pid IS NOT NULL AND res.provider_id=$pid) OR ($name IS NOT NULL AND res.name=$name)
            SET res.status='destroyed', res.updated_at=timestamp()
            """,
            pid=provider_id, name=name,
        )
