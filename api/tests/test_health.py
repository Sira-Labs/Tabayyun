import pytest
from httpx import ASGITransport, AsyncClient

from tabayyun.db import DB_DEGRADED, DB_OK, guard_schema, make_engine
from tabayyun.main import create_app
from tabayyun.settings import Settings

# Nothing listens on port 1: the connection is refused immediately.
UNREACHABLE_DB = "postgresql+psycopg://nobody:nothing@127.0.0.1:1/nodb"


@pytest.fixture
def app():
    return create_app(Settings(env="test"))


async def test_healthz(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["db"] in {DB_OK, DB_DEGRADED}


async def test_healthz_degraded_when_db_unreachable():
    app = create_app(Settings(env="test", database_url=UNREACHABLE_DB))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "db": DB_DEGRADED}


async def test_schema_guard_tolerates_unreachable_db():
    engine = make_engine(Settings(env="test", database_url=UNREACHABLE_DB))
    try:
        assert await guard_schema(engine) is None
    finally:
        await engine.dispose()


async def test_version(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/version")
    assert r.status_code == 200
    assert r.json()["env"] == "test"
    assert "schema_revision" in r.json()


def test_prod_refuses_placeholders():
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
    app = create_app(
        Settings(
            env="prod",
            session_secret="9f1c2a7d4e8b6c0f3a5d7e9b1c2d4f6a8b0c2d4e",
            database_url="postgresql+psycopg://tabayyun:s3cr3t-long-enough-value@db/tabayyun",
        )
    )
    assert app.title == "Tabayyun API"
