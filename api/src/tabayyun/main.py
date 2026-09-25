"""FastAPI application factory.

Exposes health, version, stateless check execution, runs, findings and series results, and
owns the process-wide database engine (spec 001). Routers for auth, workspaces and corrections
are added per `docs/architecture/03-system-architecture.md`.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import FastAPI

from tabayyun import __version__
from tabayyun.db import DB_OK, check_db, guard_schema, make_engine, make_session_factory, worker_commits
from tabayyun.jobs.names import WORKER_APPLICATION_NAME
from tabayyun.routers import checks, datasets, findings, groups, runs, series, sources
from tabayyun.services import runs as runs_service
from tabayyun.services.cache import RunCache
from tabayyun.settings import Settings, get_settings

log = structlog.get_logger()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI app with its routers, database engine and lifespan."""
    settings = settings or get_settings()
    settings.require_secrets_in_prod()
    # Lazy: no connection is opened until the first request or the startup guard.
    engine = make_engine(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Startup: schema guard; shutdown: dispose the engine."""
        log.info("api.start", env=settings.env, version=__version__, commit=settings.commit)
        # Exits with code 3 on a schema mismatch; an unreachable database only degrades /healthz.
        app.state.schema_revision = await guard_schema(engine)
        yield
        log.info("api.stop")
        await engine.dispose()

    app = FastAPI(
        title="Tabayyun API",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs" if settings.env != "prod" else None,
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )

    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)
    # Inline jobs write the Parquet cache from the API process (spec 006); opened on first use.
    app.state.run_cache = RunCache.from_settings(settings)
    app.state.schema_revision = None

    app.include_router(checks.router)
    app.include_router(runs.router)
    app.include_router(findings.router)
    app.include_router(series.router)
    app.include_router(sources.router)
    app.include_router(groups.router)
    app.include_router(datasets.router)

    @app.get("/healthz", tags=["ops"])
    async def healthz() -> dict[str, Any]:
        """Liveness plus database reachability and queue depth."""
        # Always 200: the container keeps running while the database restarts.
        db = await check_db(engine)
        queue = await runs_service.queue_counts(engine) if db == DB_OK else None
        return {"status": "ok", "db": db, "queue": queue}

    @app.get("/api/version", tags=["ops"])
    async def version() -> dict[str, Any]:
        """Build version and commit, environment, the schema revision seen at startup, and the
        commits of the connected workers (null when the database does not answer)."""
        return {
            "version": __version__,
            "commit": settings.commit,
            "env": settings.env,
            "schema_revision": app.state.schema_revision,
            "workers": await worker_commits(engine, WORKER_APPLICATION_NAME),
        }

    return app


app = create_app()
