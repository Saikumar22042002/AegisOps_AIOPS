"""Neo4j schema/constraints for the context graph. Run by `make migrate`."""

from __future__ import annotations

import asyncio

import structlog

from ..settings import get_settings
from .neo4j import close_neo4j, get_driver, init_neo4j

log = structlog.get_logger("neo4j-schema")

CONSTRAINTS = [
    "CREATE CONSTRAINT context_id IF NOT EXISTS FOR (c:Context) REQUIRE c.id IS UNIQUE",
    "CREATE CONSTRAINT human_name IF NOT EXISTS FOR (h:Human) REQUIRE h.name IS UNIQUE",
    "CREATE INDEX context_org IF NOT EXISTS FOR (c:Context) ON (c.org_id)",
    "CREATE INDEX step_ctx_order IF NOT EXISTS FOR (s:Step) ON (s.context_id, s.order)",
    "CREATE INDEX evidence_ctx IF NOT EXISTS FOR (e:Evidence) ON (e.context_id)",
]


async def init_constraints() -> None:
    driver = get_driver()
    async with driver.session() as session:
        for stmt in CONSTRAINTS:
            await session.run(stmt)
            log.info("neo4j.constraint_applied", stmt=stmt.split(" IF ")[0])


async def _main() -> None:
    settings = get_settings()
    init_neo4j(settings)
    try:
        await init_constraints()
        log.info("neo4j.schema_ready")
    finally:
        await close_neo4j()


if __name__ == "__main__":
    asyncio.run(_main())
