"""Async engine + schema init for the Postgres+pgvector store."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy import text

from jim.store.models import Base

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker | None = None


def get_engine(database_url: str) -> AsyncEngine:
    global _engine, _sessionmaker
    if _engine is None:
        _engine = create_async_engine(database_url, pool_pre_ping=True)
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_sessionmaker(database_url: str) -> async_sessionmaker:
    get_engine(database_url)
    assert _sessionmaker is not None
    return _sessionmaker


async def init_db(database_url: str) -> None:
    """Create the pgvector extension and all tables (idempotent)."""
    engine = get_engine(database_url)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)


async def dispose() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _sessionmaker = None
