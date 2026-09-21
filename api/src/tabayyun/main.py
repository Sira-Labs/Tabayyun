"""FastAPI application factory.

Sprint 1 exposes only health and version. Routers for auth, workspaces, series, runs,
findings and corrections are added per `docs/architecture/03-system-architecture.md`.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from tabayyun import __version__
from tabayyun.settings import Settings, get_settings

log = structlog.get_logger()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    settings.require_secrets_in_prod()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        log.info("api.start", env=settings.env, version=__version__)
        yield
        log.info("api.stop")

    app = FastAPI(
        title="Tabayyun API",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs" if settings.env != "prod" else None,
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )

    @app.get("/healthz", tags=["ops"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/version", tags=["ops"])
    async def version() -> dict[str, str]:
        return {"version": __version__, "env": settings.env}

    return app


app = create_app()
