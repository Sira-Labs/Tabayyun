"""Per-process database handles for the worker: one engine, created on first use."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from tabayyun.db import make_engine, make_session_factory
from tabayyun.services.cache import RunCache
from tabayyun.settings import get_settings

_engine: AsyncEngine | None = None
_factory: async_sessionmaker[AsyncSession] | None = None
_cache: RunCache | None = None


def engine() -> AsyncEngine:
    """The worker's engine, built from settings on first call."""
    global _engine
    if _engine is None:
        _engine = make_engine(get_settings())
    return _engine


def session_factory() -> async_sessionmaker[AsyncSession]:
    """Session factory bound to the worker's engine."""
    global _factory
    if _factory is None:
        _factory = make_session_factory(engine())
    return _factory


def run_cache() -> RunCache:
    """The worker's Parquet cache handle; the store itself opens on the first write."""
    global _cache
    if _cache is None:
        _cache = RunCache.from_settings(get_settings())
    return _cache


async def dispose() -> None:
    """Close the engine at worker shutdown."""
    global _engine, _factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _factory = None
