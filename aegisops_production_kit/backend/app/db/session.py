"""Async SQLAlchemy engine + session factory (PostgreSQL via psycopg3 async)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from ..logging_conf import get_logger
from ..settings import Settings

log = get_logger(__name__)


class Base(DeclarativeBase):
    """Declarative base for all ORM models (defined in app/db/models.py in M3)."""


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def init_engine(settings: Settings) -> AsyncEngine:
    global _engine, _sessionmaker
    if _engine is None:
        _engine = create_async_engine(
            settings.async_database_url,
            pool_size=10,
            max_overflow=20,
            pool_pre_ping=True,
            pool_recycle=1800,
            echo=False,
        )
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)
        log.info("db.engine_initialised")
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        raise RuntimeError("Database engine not initialised; call init_engine() at startup.")
    return _sessionmaker


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Transactional session scope: commit on success, rollback on error."""
    maker = get_sessionmaker()
    async with maker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a session (caller controls commits)."""
    maker = get_sessionmaker()
    async with maker() as session:
        yield session


async def ping() -> bool:
    """Readiness check: SELECT 1."""
    if _engine is None:
        return False
    async with _engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    return True


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _sessionmaker = None
        log.info("db.engine_disposed")
