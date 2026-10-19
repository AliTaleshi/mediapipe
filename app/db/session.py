"""Async engine/session for the API, sync engine/session for Celery workers."""

from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from functools import lru_cache
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings


def _configure_sqlite(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn: Any, _: Any) -> None:
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=15000")
        cur.close()


@lru_cache
def get_engine() -> AsyncEngine:
    url = get_settings().database_url
    engine = create_async_engine(url, pool_pre_ping=True)
    if url.startswith("sqlite"):
        _configure_sqlite(engine.sync_engine)
    return engine


@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        yield session


@lru_cache
def get_sync_engine() -> Engine:
    url = get_settings().sync_database_url
    engine = create_engine(url, pool_pre_ping=True)
    if url.startswith("sqlite"):
        _configure_sqlite(engine)
    return engine


@lru_cache
def get_sync_sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(get_sync_engine(), expire_on_commit=False)


@contextmanager
def sync_session() -> Iterator[Session]:
    """Session for worker code: commits on success, rolls back on error."""
    with get_sync_sessionmaker()() as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise


def reset_db_caches() -> None:
    """Drop cached engines (used by tests after changing settings)."""
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    get_sync_engine.cache_clear()
    get_sync_sessionmaker.cache_clear()
