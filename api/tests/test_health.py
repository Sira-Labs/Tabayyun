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
    s = Settings(env="prod")
    with pytest.raises(RuntimeError):
        create_app(s)
