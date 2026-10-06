"""Per-process database handles for the worker: one engine, created on first use."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from tabayyun.connectors import NetPolicy
from tabayyun.db import make_engine, make_session_factory
from tabayyun.secrets import Keyring
from tabayyun.services.cache import RunCache
from tabayyun.services.dataset_runs import RunFetch
from tabayyun.services.fetches import FetchDeps
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


def fetch_deps() -> FetchDeps:
    """What fetches need in the worker: the cache, the network policy and the keyring (spec 021)."""
    settings = get_settings()
    return FetchDeps(
        cache=run_cache(), net=NetPolicy.from_settings(settings), keyring=Keyring.from_settings(settings)
    )


def run_fetch() -> RunFetch:
    """How dataset runs in this worker fill their gaps from connectors (spec 021)."""
    return RunFetch(deps=fetch_deps(), budget_s=float(get_settings().run_fetch_budget_s))


async def dispose() -> None:
    """Close the engine at worker shutdown."""
    global _engine, _factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _factory = None
