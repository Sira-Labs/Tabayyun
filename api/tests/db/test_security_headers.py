"""Security headers on API responses (spec 015): every status, HTML pages with their CSP."""

from __future__ import annotations

import pytest

from db.test_auth_flow import browser
from tabayyun.headers import API_HEADERS, HTML_HEADERS

PATHS = [
    ("GET", "/api/version", None, 200),
    ("GET", "/healthz", None, 200),
    ("GET", "/api/series", None, 401),  # no session
    ("POST", "/api/runs", {}, 403),  # no CSRF header: refused by the guard
    ("GET", "/api/auth/login", {"method": "nope"}, 400),
]


def assert_api_headers(response) -> None:
    for name, value in API_HEADERS.items():
        assert response.headers.get(name) == value, (response.request.url, name)


@pytest.mark.parametrize(("method", "path", "params", "status"), PATHS)
async def test_every_response_has_the_api_headers(env, method, path, params, status):
    async with browser(env.app, csrf=False) as c:
        r = await (c.get(path, params=params) if method == "GET" else c.post(path, json=params))
    assert r.status_code == status, r.text
    assert_api_headers(r)


async def test_sign_in_failure_pages_get_their_csp(env):
    async with browser(env.app) as c:
        r = await c.get("/api/auth/callback", params={"code": "x", "state": "y"})
    assert r.status_code == 400 and r.headers["content-type"].startswith("text/html")
    assert_api_headers(r)
    for name, value in HTML_HEADERS.items():
        assert r.headers[name] == value


async def test_a_validation_error_and_a_rate_limit_have_them(env):
    app = env.make()
    async with browser(app) as c:
        invalid = await c.get("/api/runs", params={"limit": "many"})
        for _ in range(20):
            await c.get("/api/auth/login", params={"method": "google"})
        limited = await c.get("/api/auth/login", params={"method": "google"})
    assert limited.status_code == 429
    assert invalid.status_code in (401, 422)
    for r in (invalid, limited):
        assert_api_headers(r)
    assert limited.headers["x-frame-options"] == "DENY"  # the 429 page is HTML


async def test_headers_are_never_duplicated(env):
    """A header the route already set (the login redirect's Cache-Control) is not added twice."""
    async with browser(env.app) as c:
        r = await c.get("/api/auth/login", params={"method": "google"})
    assert r.status_code == 302
    assert r.headers["cache-control"] == "no-store"
    assert r.headers.get_list("cache-control") == ["no-store"]
