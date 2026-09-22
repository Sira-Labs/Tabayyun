from typing import Annotated

import pytest
from asgi_lifespan import LifespanManager
from fastapi import Depends, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tabayyun.db import DB_OK, SCHEMA_MISMATCH_EXIT_CODE, get_session, guard_schema, make_engine, migrate
from tabayyun.db.models import Org
from tabayyun.main import create_app
from tabayyun.settings import Settings


@pytest.fixture
def settings(db_url) -> Settings:
    """Test settings bound to the test database."""
    return Settings(env="test", database_url=db_url)


async def _count_orgs(app) -> int:
    """Number of rows in orgs through a fresh session."""
    async with app.state.session_factory() as session:
        return int(await session.scalar(select(func.count()).select_from(Org)) or 0)


async def test_session_rolls_back_on_error(settings, fresh_schema):
    """A handler that raises leaves no row behind; one that returns commits."""
    fresh_schema("auto")
    app = create_app(settings)

    @app.post("/_test/orgs")
    async def create_org(session: Annotated[AsyncSession, Depends(get_session)], fail: bool = False) -> dict:
        session.add(Org(name="probe"))
        await session.flush()
        if fail:
            raise HTTPException(status_code=418, detail="rolled back")
        return {"ok": True}

    try:
        before = await _count_orgs(app)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            assert (await c.post("/_test/orgs", params={"fail": "true"})).status_code == 418
            assert await _count_orgs(app) == before
            assert (await c.post("/_test/orgs")).status_code == 200
            assert await _count_orgs(app) == before + 1
    finally:
        await app.state.engine.dispose()


async def test_startup_refuses_old_schema(settings, db_url, fresh_schema):
    """A database behind head makes the startup guard exit with code 3."""
    fresh_schema("auto")
    migrate.downgrade(db_url, "-1")
    engine = make_engine(settings)
    try:
        with pytest.raises(SystemExit) as excinfo:
            await guard_schema(engine)
        assert excinfo.value.code == SCHEMA_MISMATCH_EXIT_CODE
    finally:
        await engine.dispose()


async def test_lifespan_reports_schema_revision_and_db_ok(settings, fresh_schema):
    """After startup /api/version reports the head revision and /healthz reports db ok."""
    fresh_schema("auto")
    app = create_app(settings)
    async with (
        LifespanManager(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c,
    ):
        version = (await c.get("/api/version")).json()
        assert version["schema_revision"] == migrate.head_revision()
        assert (await c.get("/healthz")).json() == {"status": "ok", "db": DB_OK}
