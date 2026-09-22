"""FastAPI application factory.

Exposes health, version and stateless check execution, and owns the process-wide database
engine (spec 001). Routers for auth, workspaces, series, runs, findings and corrections are
added per `docs/architecture/03-system-architecture.md`.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from tabayyun import __version__
from tabayyun.db import check_db, guard_schema, make_engine, make_session_factory
from tabayyun.routers import checks
from tabayyun.settings import Settings, get_settings

log = structlog.get_logger()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    settings.require_secrets_in_prod()
    # Lazy: no connection is opened until the first request or the startup guard.
    engine = make_engine(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        log.info("api.start", env=settings.env, version=__version__)
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

    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)
    app.state.schema_revision = None

    app.include_router(checks.router)

    @app.get("/healthz", tags=["ops"])
    async def healthz() -> dict[str, str]:
        # Always 200: the container keeps running while the database restarts.
        return {"status": "ok", "db": await check_db(engine)}

    @app.get("/api/version", tags=["ops"])
    async def version() -> dict[str, str | None]:
        return {"version": __version__, "env": settings.env, "schema_revision": app.state.schema_revision}

    return app


app = create_app()
