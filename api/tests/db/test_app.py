import uuid
from typing import Annotated

import pytest
from asgi_lifespan import LifespanManager
from fastapi import Depends, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

from tabayyun.authz import get_session
from tabayyun.db import DB_OK, SCHEMA_MISMATCH_EXIT_CODE, guard_schema, make_engine, migrate
from tabayyun.db.models import DEFAULT_ORG_ID, Team
from tabayyun.main import create_app
from tabayyun.settings import Settings
from tenancy import app_settings, org_session


@pytest.fixture
def settings(db_url) -> Settings:
    """Test settings bound to the test database."""
    return app_settings(db_url)


async def _count_teams(app) -> int:
    """Number of teams of the default org through a fresh session."""
    async with org_session(app) as session:
        return int(await session.scalar(select(func.count()).select_from(Team)) or 0)


async def test_session_rolls_back_on_error(settings, fresh_schema):
    """A handler that raises leaves no row behind; one that returns commits."""
    fresh_schema("auto")
    app = create_app(settings)

    @app.post("/_test/teams")
    async def create_team(session: Annotated[AsyncSession, Depends(get_session)], fail: bool = False) -> dict:
        """Insert one team and optionally fail afterwards, inside the request transaction."""
        session.add(Team(org_id=DEFAULT_ORG_ID, name=f"probe-{fail}"))
        await session.flush()
        if fail:
            raise HTTPException(status_code=418, detail="rolled back")
        return {"ok": True}

    try:
        before = await _count_teams(app)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers={"X-Tabayyun-Request": "1"}
        ) as c:
            assert (await c.post("/_test/teams", params={"fail": "true"})).status_code == 418
            assert await _count_teams(app) == before
            assert (await c.post("/_test/teams")).status_code == 200
            assert await _count_teams(app) == before + 1
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
        AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers={"X-Tabayyun-Request": "1"}
        ) as c,
    ):
        version = (await c.get("/api/version")).json()
        assert version["schema_revision"] == migrate.head_revision()
        assert (await c.get("/healthz")).json() == {
            "status": "ok",
            "db": DB_OK,
            "queue": {"pending": 0, "running": 0},
        }


async def test_rls_violation_is_logged_and_answers_500(settings, fresh_schema):
    """A write into another org is refused by the policy, logged as `db.rls_violation`, 500."""
    fresh_schema("auto")
    app = create_app(settings)

    @app.post("/_test/foreign-team")
    async def foreign_team(session: Annotated[AsyncSession, Depends(get_session)]) -> dict:
        """Insert a team into an org the request does not act in."""
        session.add(Team(org_id=uuid.uuid4(), name="intruder"))
        await session.flush()
        return {"ok": True}

    try:
        with capture_logs() as logs:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test", headers={"X-Tabayyun-Request": "1"}
            ) as c:
                r = await c.post("/_test/foreign-team")
        assert r.status_code == 500 and r.json() == {"detail": "internal error"}
        assert [e["path"] for e in logs if e["event"] == "db.rls_violation"] == ["/_test/foreign-team"]
    finally:
        await app.state.engine.dispose()
