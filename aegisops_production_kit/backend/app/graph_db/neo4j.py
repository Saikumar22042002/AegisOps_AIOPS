"""Async Neo4j driver (context graph). Full graph model implemented in M3/M4."""

from __future__ import annotations

from neo4j import AsyncDriver, AsyncGraphDatabase

from ..logging_conf import get_logger
from ..settings import Settings

log = get_logger(__name__)

_driver: AsyncDriver | None = None


def init_neo4j(settings: Settings) -> AsyncDriver:
    global _driver
    if _driver is None:
        _driver = AsyncGraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
            max_connection_lifetime=1800,
            connection_acquisition_timeout=30,
        )
        log.info("neo4j.initialised")
    return _driver


def get_driver() -> AsyncDriver:
    if _driver is None:
        raise RuntimeError("Neo4j not initialised; call init_neo4j() at startup.")
    return _driver


async def ping() -> bool:
    if _driver is None:
        return False
    await _driver.verify_connectivity()
    return True


async def close_neo4j() -> None:
    global _driver
    if _driver is not None:
        await _driver.close()
        _driver = None
        log.info("neo4j.closed")
