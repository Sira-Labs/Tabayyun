"""The admin gate (spec 014) over every `/api/admin` route, in oidc mode with real sessions:
session first (401), then a fresh passkey sign-in (403 `second-factor-required`), then the role.

The route list comes from the OpenAPI document, so a new admin route is checked without edits.
"""

from __future__ import annotations

import uuid

import pytest

from db.test_auth_flow import ADMIN, OTHER, browser, sign_in, sql
from tabayyun.db.models import DEFAULT_ORG_ID
from tabayyun.main import create_app
from tenancy import app_settings

BODY = {"name": "x", "role": "viewer", "email": "x@example.test", "org_role": "member"}


def _admin_routes() -> list[tuple[str, str]]:
    app = create_app(app_settings("postgresql+psycopg://u:p@localhost/x"))
    return sorted(
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        if path.startswith("/api/admin")
        for method in operations
    )


ROUTES = _admin_routes()


def _fill(path: str) -> str:
    """Every `{id}` placeholder replaced by a random id: the gate answers before any lookup."""
    while "{" in path:
        path = path[: path.index("{")] + str(uuid.uuid4()) + path[path.index("}") + 1 :]
    return path


async def _call(client, method: str, path: str):
    body = BODY if method in {"POST", "PUT", "PATCH"} else None
    return await client.request(method, _fill(path), json=body)


def test_the_admin_routes_are_listed():
    """A guard against an empty parametrisation: the admin API has routes of every verb."""
    assert {m for m, _ in ROUTES} == {"GET", "POST", "PATCH", "PUT", "DELETE"}
    assert len(ROUTES) >= 20


async def test_the_gate_in_order(env):
    """For every admin route: 401 without a session; 403 `second-factor-required` for an owner
    signed in with Google or with a passkey older than 12 h; 403 `forbidden` or 404 for a member
    with a fresh passkey."""
    async with browser(env.app) as anonymous, browser(env.app) as google, browser(env.app) as stale:
        await sign_in(env, google, "google", email=ADMIN)
        await sign_in(env, stale, "passkey", email=ADMIN)
        sql(
            env,
            "UPDATE sessions SET created_at = now() - interval '13 hours' "
            "WHERE sign_in_method = 'passkey' AND revoked_at IS NULL",
        )
        async with browser(env.app) as member:
            await sign_in(env, member, "passkey", email=OTHER)
            sql(
                env,
                "INSERT INTO org_memberships (org_id, user_id, role) "
                "SELECT :o, id, 'member' FROM users WHERE email = :e",
                o=DEFAULT_ORG_ID,
                e=OTHER,
            )
            await sign_in(env, member, "passkey", email=OTHER)
            for method, path in ROUTES:
                r = await _call(anonymous, method, path)
                assert r.status_code == 401, (method, path, r.text)
                for client in (google, stale):
                    r = await _call(client, method, path)
                    assert (r.status_code, r.json()["detail"]) == (403, "second-factor-required"), (
                        method,
                        path,
                    )
                r = await _call(member, method, path)
                assert r.status_code in (403, 404), (method, path, r.text)
                assert r.json()["detail"] != "second-factor-required", (method, path)


async def test_a_fresh_passkey_owner_passes(env):
    async with browser(env.app) as owner:
        await sign_in(env, owner, "passkey", email=ADMIN)
        r = await owner.get("/api/admin/org")
        assert (r.status_code, r.json()["role"]) == (200, "owner")
        assert (await owner.post("/api/admin/teams", json={"name": "ops"})).status_code == 201
    [(actor,)] = sql(env, "SELECT u.email FROM audit_events e JOIN users u ON u.id = e.actor_user_id")
    assert actor == ADMIN


@pytest.mark.parametrize("method", ["POST", "PATCH", "DELETE"])
async def test_unsafe_admin_requests_need_the_csrf_header(env, method):
    async with browser(env.app, csrf=False) as owner:
        await sign_in(env, owner, "passkey", email=ADMIN)
        r = await owner.request(method, "/api/admin/teams", json={"name": "ops"})
    assert r.status_code == 403 and r.json()["detail"] == "csrf"
