"""The CSRF guard (spec 013, behaviour 5)."""

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from tabayyun.auth.csrf import CsrfMiddleware

PUBLIC = "https://tabayyun.example.org"


def _app(public_url: str | None = PUBLIC) -> CsrfMiddleware:
    async def ok(request):
        return PlainTextResponse("ok")

    methods = ["GET", "POST", "PUT", "PATCH", "DELETE"]
    inner = Starlette(routes=[Route("/api/x", ok, methods=methods), Route("/api/hook", ok, methods=methods)])
    return CsrfMiddleware(inner, public_url=public_url, exempt_paths=frozenset({"/api/hook"}))


async def _status(app, method: str, path: str = "/api/x", **headers: str) -> int:
    async with AsyncClient(transport=ASGITransport(app=app), base_url=PUBLIC) as c:
        return (await c.request(method, path, headers=headers)).status_code


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
async def test_csrf_header_and_origin(method):
    """Unsafe methods need the header, and a present Origin must be the public URL's."""
    app = _app()
    header = {"X-Tabayyun-Request": "1"}
    assert await _status(app, method) == 403
    assert await _status(app, method, **{"X-Tabayyun-Request": "0"}) == 403
    assert await _status(app, method, **header) == 200
    assert await _status(app, method, **header, Origin=PUBLIC) == 200
    assert await _status(app, method, **header, Origin="https://TABAYYUN.example.org") == 200
    assert await _status(app, method, **header, Origin="https://evil.example") == 403
    assert await _status(app, method, **header, Origin="http://tabayyun.example.org") == 403


async def test_safe_methods_and_exempt_path_pass():
    """GET needs nothing; the back-channel path is exempt; without a public URL only the
    header is checked."""
    assert await _status(_app(), "GET") == 200
    assert await _status(_app(), "POST", "/api/hook") == 200
    assert (
        await _status(_app(None), "POST", **{"X-Tabayyun-Request": "1", "Origin": "https://x.example"}) == 200
    )
