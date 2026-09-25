import pytest
from httpx import ASGITransport, AsyncClient

import tabayyun.db
from tabayyun.db import DB_DEGRADED, DB_OK, check_db, guard_schema, make_engine
from tabayyun.main import create_app
from tabayyun.settings import Settings

# Nothing listens on port 1: the connection is refused immediately.
UNREACHABLE_DB = "postgresql+psycopg://nobody:nothing@127.0.0.1:1/nodb"


@pytest.fixture
def app():
    """App bound to the default (dev) settings in test mode."""
    return create_app(Settings(env="test"))


async def test_healthz(app):
    """/healthz answers 200 with a db field whatever the database state."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["db"] in {DB_OK, DB_DEGRADED}


async def test_healthz_degraded_when_db_unreachable():
    """/healthz stays 200 and reports db degraded when nothing answers."""
    app = create_app(Settings(env="test", database_url=UNREACHABLE_DB))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "db": DB_DEGRADED, "queue": None}


async def test_check_db_reports_degraded_on_timeout(monkeypatch):
    """A timeout (whose exception has no message) is reported as degraded, not raised."""
    monkeypatch.setattr(tabayyun.db, "DB_HEALTH_TIMEOUT_S", 0.0)
    engine = make_engine(Settings(env="test", database_url=UNREACHABLE_DB))
    try:
        assert await check_db(engine) == DB_DEGRADED
    finally:
        await engine.dispose()


async def test_schema_guard_tolerates_unreachable_db():
    """An unreachable database yields no revision instead of exiting."""
    engine = make_engine(Settings(env="test", database_url=UNREACHABLE_DB))
    try:
        assert await guard_schema(engine) is None
    finally:
        await engine.dispose()


async def test_version(app):
    """/api/version reports the environment and a schema_revision field."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/version")
    assert r.status_code == 200
    assert r.json()["env"] == "test"
    assert "schema_revision" in r.json()
    assert r.json()["commit"] is None


async def test_version_without_database_reports_no_workers():
    """With the database unreachable, the workers field is null rather than an error."""
    app = create_app(Settings(env="test", database_url=UNREACHABLE_DB))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/version")
    assert r.status_code == 200
    assert r.json()["workers"] is None


async def test_version_reports_the_build_commit():
    """/api/version reports the commit the image was built from (TABAYYUN_COMMIT)."""
    app = create_app(Settings(env="test", commit="ce4c37fbfc9efff2d44289d05fc6c42049be2f5c"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/version")
    assert r.json()["commit"] == "ce4c37fbfc9efff2d44289d05fc6c42049be2f5c"


def test_prod_refuses_placeholders():
    """Prod refuses missing and placeholder secrets."""
    with pytest.raises(RuntimeError):
        create_app(Settings(env="prod"))
    with pytest.raises(RuntimeError, match="SESSION_SECRET"):
        create_app(
            Settings(
                env="prod",
                session_secret="change-me-openssl-rand-base64-48",
                database_url="postgresql+psycopg://tabayyun:s3cr3t-long-enough-value@db/tabayyun",
            )
        )


def test_prod_starts_without_oidc_when_secrets_are_real():
    """Prod starts with real secrets even before OIDC is configured."""
    app = create_app(
        Settings(
            env="prod",
            session_secret="9f1c2a7d4e8b6c0f3a5d7e9b1c2d4f6a8b0c2d4e",
            database_url="postgresql+psycopg://tabayyun:s3cr3t-long-enough-value@db/tabayyun",
        )
    )
    assert app.title == "Tabayyun API"
