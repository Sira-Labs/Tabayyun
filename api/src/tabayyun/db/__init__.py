"""Database access: engine factory, request-scoped sessions, health check and the schema
revision guard (spec 001).

One async engine per process is created by `create_app()` and disposed at shutdown. Request
handlers receive a session through `get_session`; every session is one transaction that
commits when the handler returns and rolls back on any exception.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import structlog
from fastapi import Request
from sqlalchemy import MetaData, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from tabayyun.settings import Settings

log = structlog.get_logger()

# Deterministic constraint names so that models and migrations agree and `alembic check`
# has nothing to argue about.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# Status values for /healthz and how long `SELECT 1` may take before the database counts
# as degraded.
DB_HEALTH_TIMEOUT_S = 2.0
DB_OK = "ok"
DB_DEGRADED = "degraded"

# Exit code when the database schema does not match the migrations shipped with the code.
SCHEMA_MISMATCH_EXIT_CODE = 3


def _error_line(exc: BaseException) -> str:
    """First line of the exception message, or the class name when the message is empty."""
    message = str(exc).strip()
    return message.splitlines()[0] if message else type(exc).__name__


class Base(DeclarativeBase):
    """Declarative base shared by every model; `metadata` is the Alembic target."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def make_engine(settings: Settings) -> AsyncEngine:
    """Build the process-wide async engine. No connection is opened until first use."""
    return create_async_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_pool_max_overflow,
        pool_pre_ping=True,
    )


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Session factory bound to `engine`; objects stay usable after commit."""
    return async_sessionmaker(engine, expire_on_commit=False)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one transaction per request, committed on success."""
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with factory() as session, session.begin():
        yield session


async def check_db(engine: AsyncEngine) -> str:
    """`ok` when `SELECT 1` answers within DB_HEALTH_TIMEOUT_S seconds, else `degraded`."""
    try:
        async with asyncio.timeout(DB_HEALTH_TIMEOUT_S), engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except (SQLAlchemyError, OSError, TimeoutError) as exc:
        log.warning("db.unreachable", error=_error_line(exc))
        return DB_DEGRADED
    return DB_OK


async def worker_commits(engine: AsyncEngine, application_name: str) -> list[str] | None:
    """Commits of the workers connected to this database, or None when it does not answer.

    Read from the `<application_name>/<commit>` names the worker sets on its connections
    (`tabayyun.jobs.worker_application_name`); a worker without a commit is not listed.
    """
    try:
        async with asyncio.timeout(DB_HEALTH_TIMEOUT_S), engine.connect() as conn:
            rows = await conn.execute(
                text(
                    "SELECT DISTINCT application_name FROM pg_stat_activity "
                    "WHERE datname = current_database() AND application_name LIKE :prefix"
                ),
                {"prefix": f"{application_name}/%"},
            )
            names = rows.scalars().all()
    except (SQLAlchemyError, OSError, TimeoutError) as exc:
        log.warning("db.workers_unknown", error=_error_line(exc))
        return None
    return sorted(name.split("/", 1)[1] for name in names)


async def current_revision(engine: AsyncEngine) -> str | None:
    """Alembic revision stamped in the database, or None when it has never been migrated."""
    from alembic.runtime.migration import MigrationContext

    def _read(conn: Any) -> str | None:
        """Read the alembic_version row on a sync connection."""
        return MigrationContext.configure(conn).get_current_revision()

    async with engine.connect() as conn:
        return await conn.run_sync(_read)


async def guard_schema(engine: AsyncEngine) -> str | None:
    """Refuse to serve on a schema that is not exactly at the head migration.

    Returns the head revision when the database matches. Returns None when the database is
    unreachable: the process keeps running and `/healthz` reports `degraded` so a database
    restart does not crash-loop the API. Any revision mismatch (behind, ahead or never
    migrated) logs both revisions and exits with code 3.
    """
    from tabayyun.db.migrate import head_revision

    head = head_revision()
    try:
        current = await current_revision(engine)
    except (SQLAlchemyError, OSError) as exc:
        log.warning("db.schema_unverified", error=_error_line(exc), head=head)
        return None
    if current != head:
        log.error("db.schema_mismatch", current=current, head=head, hint="run: alembic upgrade head")
        raise SystemExit(SCHEMA_MISMATCH_EXIT_CODE)
    log.info("db.schema_ok", revision=head)
    return head
