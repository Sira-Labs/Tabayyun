import pytest
from httpx import ASGITransport, AsyncClient

from tabayyun.main import create_app
from tabayyun.settings import Settings


@pytest.fixture
def app():
    return create_app(Settings(env="test"))


async def test_healthz(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def test_version(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/version")
    assert r.status_code == 200
    assert r.json()["env"] == "test"


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
