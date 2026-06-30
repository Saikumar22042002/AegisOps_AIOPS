"""Durable LangGraph checkpointer (Postgres) — enables interrupts + resume after restart.

State is persisted per thread_id (= run_id) so the approval interrupt can pause the graph and
a later POST /approvals/{runId} resumes it from the exact checkpoint, even across API restarts.
"""

from __future__ import annotations

import structlog
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import AsyncConnectionPool

from ..settings import Settings

log = structlog.get_logger(__name__)

_pool: AsyncConnectionPool | None = None
_saver: AsyncPostgresSaver | None = None


def _dsn(settings: Settings) -> str:
    # psycopg wants a plain libpq URL (no +psycopg driver suffix).
    return settings.database_url.replace("postgresql+psycopg://", "postgresql://", 1)


async def init_checkpointer(settings: Settings) -> AsyncPostgresSaver:
    global _pool, _saver
    if _saver is None:
        _pool = AsyncConnectionPool(
            conninfo=_dsn(settings),
            max_size=10,
            kwargs={"autocommit": True, "prepare_threshold": 0},
            open=False,
        )
        await _pool.open()
        _saver = AsyncPostgresSaver(_pool)
        await _saver.setup()  # creates checkpoint tables if absent
        log.info("checkpointer.initialised")
    return _saver


def get_checkpointer() -> AsyncPostgresSaver:
    if _saver is None:
        raise RuntimeError("Checkpointer not initialised; call init_checkpointer() at startup.")
    return _saver


async def close_checkpointer() -> None:
    global _pool, _saver
    if _pool is not None:
        await _pool.close()
        _pool = None
        _saver = None
        log.info("checkpointer.closed")
