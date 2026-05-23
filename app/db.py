"""Async SQLAlchemy engine + session factory.

Why async + asyncpg
  - FastAPI is asyncio-native; running blocking psycopg in an event loop
    burns threads under load.
  - asyncpg is the fastest pure-Python Postgres driver and is well-supported
    by SQLAlchemy 2.0.

Connection pooling
  - We pool inside the API container (pool_size=10, max_overflow=10).
  - In Kubernetes you typically want one pgbouncer per node and a small per-
    container pool to avoid storming Postgres on rolling deploys.  Adjust the
    sizes via env if you front this with pgbouncer.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from .config import Settings


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def init_engine(settings: Settings) -> AsyncEngine:
    """Build the engine + session factory from Settings.

    Idempotent so that tests can rebuild against a different DSN.
    """
    global _engine, _session_factory
    if _engine is not None:
        return _engine
    _engine = create_async_engine(
        settings.resolved_database_url,
        pool_size=10,
        max_overflow=10,
        pool_pre_ping=True,  # detects stale connections after pg restarts
        pool_recycle=1800,
        future=True,
    )
    _session_factory = async_sessionmaker(
        bind=_engine,
        expire_on_commit=False,
        autoflush=False,
    )
    return _engine


async def dispose_engine() -> None:
    """Tear down on shutdown so we don't leak sockets in tests."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


def get_engine() -> AsyncEngine:
    if _engine is None:
        raise RuntimeError("DB engine not initialised; call init_engine() first.")
    return _engine


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Yield a session inside a transaction.  Commit on success, rollback on raise."""
    if _session_factory is None:
        raise RuntimeError("DB engine not initialised; call init_engine() first.")
    async with _session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency that yields a transactional session per request."""
    async with session_scope() as session:
        yield session
