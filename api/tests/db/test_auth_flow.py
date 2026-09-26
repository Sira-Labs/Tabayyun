"""The login end to end (spec 013): the api in oidc mode against the fake IdP and the test
database, as the app login, so row-level security and the grants of migration 0005 apply."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import Depends
from httpx import ASGITransport, AsyncClient
from joserfc.jwk import RSAKey
from sqlalchemy import create_engine, text

from fake_idp import CLIENT_SECRET, ISSUER, FakeIdp, unsigned
from tabayyun.auth import SESSION_COOKIE, OidcClient, require_recent_passkey
from tabayyun.db.models import DEFAULT_ORG_ID, DEFAULT_WORKSPACE_ID
from tabayyun.main import create_app
from tenancy import app_settings

PUBLIC = "https://tabayyun.test"
ADMIN = "owner@example.org"
OTHER = "other@example.org"
CSRF = {"X-Tabayyun-Request": "1"}
OTHER_KEY = RSAKey.generate_key(2048, parameters={"kid": "k1"}, private=True)
ORG_B = uuid.UUID("00000000-0000-0000-0000-00000000000b")
WORKSPACE_B = uuid.UUID("00000000-0000-0000-0000-0000000000b2")


def auth_settings(db_url: str, **overrides: Any):
    """Test settings in oidc mode against the fake IdP."""
    values = {
        "auth_mode": "oidc",
        "public_url": PUBLIC,
        "oidc_issuer": ISSUER,
        "oidc_client_secret": CLIENT_SECRET,
        "session_secret": "test-session-secret-0123456789abcdef",
        "admin_email": ADMIN,
        **overrides,
    }
    return app_settings(db_url, **values)


@pytest.fixture
async def env(db_url, fresh_schema):
    """A fresh schema, the fake IdP, an app factory and an owner connection for setup."""
    fresh_schema("auto")
    idp = FakeIdp()
    apps = []

    def make(oidc: OidcClient | None = None, **overrides: Any):
        app = create_app(auth_settings(db_url, **overrides), oidc=oidc or idp.client())
        apps.append(app)
        return app

    owner = create_engine(db_url, isolation_level="AUTOCOMMIT")
    yield SimpleNamespace(idp=idp, make=make, app=make(), owner=owner)
    for app in apps:
        await app.state.engine.dispose()
    owner.dispose()


def browser(app, *, csrf: bool = True) -> AsyncClient:
    """A client on the public https origin (the cookies are Secure) that keeps cookies."""
    return AsyncClient(transport=ASGITransport(app=app), base_url=PUBLIC, headers=CSRF if csrf else {})


def sql(env, statement: str, **params: Any) -> Any:
    """Run one statement as the owner (no RLS) and return the result's rows."""
    with env.owner.connect() as conn:
        result = conn.execute(text(statement), params)
        return result.all() if result.returns_rows else None


def method_claims(method: str) -> dict[str, Any]:
    """What Keycloak puts in the token for each method (spec 013, implementation notes)."""
    return {"amr": ["passkey"]} if method == "passkey" else {"identity_provider": method}


async def start(c: AsyncClient, method: str, next_path: str | None = None) -> httpx.Response:
    params = {"method": method} | ({"next": next_path} if next_path else {})
    return await c.get("/api/auth/login", params=params)


async def sign_in(
    env,
    c: AsyncClient,
    method: str = "google",
    email: str = ADMIN,
    next_path: str | None = None,
    **claims: Any,
) -> httpx.Response:
    """The whole browser round trip; `claims` override the ID token's."""
    started = await start(c, method, next_path)
    assert started.status_code == 302, started.text
    signer = claims.pop("signer", None)
    callback, _ = env.idp.authorize(
        started.headers["location"],
        email=email,
        sub=f"kc-{email}",
        signer=signer,
        **(method_claims(method) | claims),
    )
    return await c.get(callback)


def live_sessions(env) -> int:
    return sql(env, "SELECT count(*) FROM sessions WHERE revoked_at IS NULL")[0][0]


@pytest.mark.parametrize("method", ["google", "github", "passkey"])
async def test_full_login_per_method(env, method):
    """Each method signs the admin in: cookie, /me, and API reads in their org."""
    async with browser(env.app) as c:
        done = await sign_in(env, c, method, next_path="/runs?tab=open")
        assert done.status_code == 302 and done.headers["location"] == "/runs?tab=open"
        cookie = done.headers["set-cookie"]
        assert SESSION_COOKIE in cookie and "HttpOnly" in cookie and "Secure" in cookie
        assert "samesite=lax" in cookie.lower() and "Path=/" in cookie
        me = (await c.get("/api/auth/me")).json()
        assert me["user"]["email"] == ADMIN and me["role"] == "owner"
        assert me["org"]["id"] == str(DEFAULT_ORG_ID) and me["sign_in_method"] == method
        assert me["passkey_fresh"] is (method == "passkey")
        assert (await c.get("/api/sources")).status_code == 200
        assert (await c.get("/api/checks")).status_code == 200
    row = sql(env, "SELECT sign_in_method, idp_sid, id_token IS NOT NULL FROM sessions")
    assert row == [(method, f"sid-kc-{ADMIN}", True)]


async def test_anonymous_requests(env):
    """Without a session: 401 on API routes, while version, health and sign-in options answer."""
    async with browser(env.app) as c:
        for path in ("/api/sources", "/api/checks", "/api/auth/me", "/api/auth/sessions"):
            response = await c.get(path)
            assert response.status_code == 401 and response.json() == {"detail": "not_authenticated"}, path
        assert (await c.get("/api/version")).status_code == 200
        assert (await c.get("/healthz")).status_code == 200
        options = (await c.get("/api/auth/sign-in-options")).json()
        assert options == {"google": True, "github": True, "passkey": True}
        forged = await c.get("/api/sources", headers={"Cookie": f"{SESSION_COOKIE}=forged-token"})
        assert forged.status_code == 401


def _bad_signature(claims: dict[str, Any]) -> str:
    return FakeIdp().sign(claims, key=OTHER_KEY)


@pytest.mark.parametrize(
    ("case", "method", "claims", "status", "code"),
    [
        ("wrong_nonce", "google", {"nonce": "other"}, 400, "invalid_token"),
        ("wrong_audience", "google", {"aud": "other", "azp": "other"}, 400, "invalid_token"),
        ("bad_signature", "google", {"signer": _bad_signature}, 400, "invalid_token"),
        ("alg_none", "google", {"signer": unsigned}, 400, "invalid_token"),
        ("email_unverified", "google", {"email_verified": False}, 400, "invalid_token"),
        ("passkey_without_amr", "passkey", {"amr": None}, 400, "passkey_required"),
        ("passkey_with_password", "passkey", {"amr": ["pwd"]}, 400, "passkey_required"),
        ("google_via_github", "google", {"identity_provider": "github"}, 400, "invalid_token"),
    ],
)
async def test_callback_rejects_tokens(env, case, method, claims, status, code):
    """Each token failure gives its status and code, and creates no session."""
    async with browser(env.app) as c:
        done = await sign_in(env, c, method, **claims)
        assert (done.status_code, code in done.text) == (status, True), case
        assert SESSION_COOKIE not in done.headers.get("set-cookie", "")
    assert live_sessions(env) == 0
    assert sql(env, "SELECT count(*) FROM login_flows")[0][0] == 0


async def test_login_flow_is_single_use(env):
    """State mismatch, an expired flow, a reused flow and a missing cookie give 400
    `login_expired`; a cancelled login and an IdP error their own codes."""
    async with browser(env.app) as c:
        started = await start(c, "google")
        callback, _ = env.idp.authorize(
            started.headers["location"], email=ADMIN, sub="kc", **method_claims("google")
        )
        forged = callback.replace("state=", "state=x")
        assert "login_expired" in (await c.get(forged)).text
        assert "login_expired" in (await c.get(callback)).text  # the flow went with the first try

        started = await start(c, "google")
        callback, _ = env.idp.authorize(
            started.headers["location"], email=ADMIN, sub="kc", **method_claims("google")
        )
        sql(env, "UPDATE login_flows SET created_at = now() - interval '11 minutes'")
        assert (await c.get(callback)).status_code == 400

        started = await start(c, "google")
        state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
        cancelled = await c.get("/api/auth/callback", params={"state": state, "error": "access_denied"})
        assert cancelled.status_code == 400 and "login_cancelled" in cancelled.text

        started = await start(c, "google")
        callback, _ = env.idp.authorize(
            started.headers["location"], email=ADMIN, sub="kc", **method_claims("google")
        )
        env.idp.grants.clear()  # the IdP no longer knows the code
        assert (await c.get(callback)).status_code == 502

        started = await start(c, "google")
        callback, _ = env.idp.authorize(
            started.headers["location"], email=ADMIN, sub="kc", **method_claims("google")
        )
    async with browser(env.app) as other:  # another browser has no flow cookie
        assert "login_expired" in (await other.get(callback)).text
    assert live_sessions(env) == 0

    async with browser(env.app) as c:
        assert (await sign_in(env, c)).status_code == 302
        # Replaying a used callback URL finds no flow.
    assert live_sessions(env) == 1


async def test_next_and_methods(env):
    """`next` outside the site becomes `/`; a disabled or unknown method gives 400."""
    async with browser(env.app) as c:
        for target in ("https://evil.example", "//evil.example", "/\\evil.example"):
            assert (await sign_in(env, c, next_path=target)).headers["location"] == "/"
    limited = env.make(sign_in_methods="google,passkey")
    async with browser(limited) as c:
        assert (await start(c, "github")).status_code == 400
        assert (await start(c, "password")).status_code == 400
        assert (await c.get("/api/auth/sign-in-options")).json()["github"] is False


async def test_admin_email_becomes_owner_once(env):
    """The admin email becomes owner at its first verified login; another email gets a
    session without access; a changed setting later grants nothing."""
    async with browser(env.app) as c:
        await sign_in(env, c, "github", email=OTHER)
        me = await c.get("/api/auth/me")
        assert me.status_code == 403 and me.json() == {"detail": "no_access", "email": OTHER}
        assert (await c.get("/api/sources")).json() == {"detail": "no_access"}
    async with browser(env.app) as c:
        await sign_in(env, c, "google", email=ADMIN)
        assert (await c.get("/api/auth/me")).json()["role"] == "owner"
    moved = env.make(admin_email=OTHER)
    async with browser(moved) as c:
        await sign_in(env, c, "google", email=OTHER)
        assert (await c.get("/api/auth/me")).status_code == 403
    owners = sql(
        env, "SELECT u.email FROM org_memberships m JOIN users u ON u.id = m.user_id WHERE m.role = 'owner'"
    )
    emails = {e for (e,) in owners}
    assert ADMIN in emails and OTHER not in emails


async def test_idle_and_absolute_expiry(env):
    """Revoked, idle-expired and absolute-expired sessions each give 401."""
    for change in (
        "UPDATE sessions SET revoked_at = now()",
        "UPDATE sessions SET last_seen_at = now() - interval '13 hours'",
        "UPDATE sessions SET expires_at = now() - interval '1 second'",
    ):
        async with browser(env.app) as c:
            await sign_in(env, c)
            assert (await c.get("/api/auth/me")).status_code == 200
            sql(env, change)
            assert (await c.get("/api/auth/me")).status_code == 401, change
            assert (await c.get("/api/sources")).status_code == 401, change


async def test_disabled_user_loses_sessions(env):
    """A disabled user gets 401, every session of theirs is revoked, and a new login fails."""
    async with browser(env.app) as c1, browser(env.app) as c2:
        await sign_in(env, c1)
        await sign_in(env, c2, "passkey")
        sql(env, "UPDATE users SET disabled_at = now() WHERE email = :e", e=ADMIN)
        assert (await c1.get("/api/sources")).status_code == 401
        assert live_sessions(env) == 0
        sql(env, "UPDATE users SET disabled_at = NULL WHERE email = :e", e=ADMIN)
        assert (await c2.get("/api/auth/me")).status_code == 401  # revoked stays revoked
        sql(env, "UPDATE users SET disabled_at = now() WHERE email = :e", e=ADMIN)
        again = await sign_in(env, c2)
        assert again.status_code == 403 and "account_disabled" in again.text
    assert live_sessions(env) == 0


async def test_devices_list_and_revoke(env):
    """Users list and revoke only their own sessions; revoke-others keeps the current one."""
    async with browser(env.app) as c1, browser(env.app) as c2, browser(env.app) as c3:
        await sign_in(env, c1, "google")
        await sign_in(env, c2, "passkey")
        await sign_in(env, c3, "github", email=OTHER)
        devices = (await c1.get("/api/auth/sessions")).json()
        assert len(devices) == 2 and sum(d["current"] for d in devices) == 1
        assert set(devices[0]) == {
            "id", "current", "sign_in_method", "created_at", "last_seen_at", "user_agent", "ip_address"
        }  # fmt: skip
        assert {d["sign_in_method"] for d in devices} == {"google", "passkey"}
        foreign = (await c3.get("/api/auth/sessions")).json()[0]["id"]
        assert (await c1.delete(f"/api/auth/sessions/{foreign}")).status_code == 404
        assert (await c1.delete(f"/api/auth/sessions/{uuid.uuid4()}")).status_code == 404
        assert (await c3.get("/api/auth/me")).status_code == 403  # untouched, still no access

        assert (await c1.post("/api/auth/sessions/revoke-others")).status_code == 204
        assert (await c2.get("/api/auth/me")).status_code == 401
        remaining = (await c1.get("/api/auth/sessions")).json()
        assert [d["current"] for d in remaining] == [True]

        own = remaining[0]["id"]
        revoked = await c1.delete(f"/api/auth/sessions/{own}")
        assert revoked.status_code == 204 and SESSION_COOKIE in revoked.headers["set-cookie"]
        assert (await c1.get("/api/auth/me")).status_code == 401


async def test_logout_revokes_and_returns_end_session(env):
    """Logout revokes the session and returns the end-session URL with the ID token hint."""
    async with browser(env.app) as c:
        await sign_in(env, c)
        id_token = sql(env, "SELECT id_token FROM sessions")[0][0]
        out = await c.post("/api/auth/logout")
        url = out.json()["logout_url"]
        query = parse_qs(urlsplit(url).query)
        assert url.startswith(f"{ISSUER}/protocol/openid-connect/logout?")
        assert query["id_token_hint"] == [id_token] and query["post_logout_redirect_uri"] == [f"{PUBLIC}/"]
        assert (await c.get("/api/auth/me")).status_code == 401
        again = (await c.post("/api/auth/logout")).json()["logout_url"]
        assert "id_token_hint" not in parse_qs(urlsplit(again).query)
    assert live_sessions(env) == 0


async def test_backchannel_logout_revokes_by_sid(env):
    """A logout token revokes the sessions of its sid, or of its sub; no CSRF header needed;
    an invalid token gives 400."""
    async with browser(env.app) as c1, browser(env.app) as c2, browser(env.app, csrf=False) as idp_side:
        await sign_in(env, c1, sid="s1")
        await sign_in(env, c2, "passkey", sid="s2")
        hook = "/api/auth/backchannel-logout"
        bad = await idp_side.post(hook, data={"logout_token": env.idp.logout_token(sid="s1", nonce="n")})
        assert bad.status_code == 400
        assert (await idp_side.post(hook, data={"logout_token": "garbage"})).status_code == 400
        ok = await idp_side.post(hook, data={"logout_token": env.idp.logout_token(sid="s1")})
        assert ok.status_code == 200 and ok.headers["cache-control"] == "no-store"
        assert (await c1.get("/api/auth/me")).status_code == 401
        assert (await c2.get("/api/auth/me")).status_code == 200
        by_sub = await idp_side.post(hook, data={"logout_token": env.idp.logout_token(sub=f"kc-{ADMIN}")})
        assert by_sub.status_code == 200
        assert (await c2.get("/api/auth/me")).status_code == 401


async def test_csrf_on_the_app(env):
    """Unsafe requests without the header or from a foreign origin get 403 `csrf`."""
    async with browser(env.app, csrf=False) as c:
        await sign_in(env, c)
        assert (await c.post("/api/auth/logout")).json() == {"detail": "csrf"}
        foreign = await c.post("/api/auth/logout", headers=CSRF | {"Origin": "https://evil.example"})
        assert foreign.status_code == 403
        same = await c.post("/api/auth/logout", headers=CSRF | {"Origin": PUBLIC})
        assert same.status_code == 200


async def test_require_recent_passkey_gate(env):
    """The spec 014 gate passes a fresh passkey session only."""

    @env.app.get("/api/_test/admin", dependencies=[Depends(require_recent_passkey)])
    async def admin_only() -> dict[str, bool]:
        return {"ok": True}

    async with browser(env.app) as google, browser(env.app) as passkey:
        await sign_in(env, google, "google")
        await sign_in(env, passkey, "passkey")
        refused = await google.get("/api/_test/admin")
        assert refused.status_code == 403 and refused.json() == {"detail": "second-factor-required"}
        assert (await passkey.get("/api/_test/admin")).status_code == 200
        sql(
            env,
            "UPDATE sessions SET created_at = now() - interval '13 hours' WHERE sign_in_method = 'passkey'",
        )
        assert (await passkey.get("/api/_test/admin")).status_code == 403
        assert (await passkey.get("/api/auth/me")).json()["passkey_fresh"] is False


async def test_cross_tenant_isolation_with_sessions(env):
    """A session in org B cannot see org A's workspace; org A's owner can."""
    sql(
        env,
        "INSERT INTO sources (id, org_id, workspace_id, type, name) "
        "VALUES (gen_random_uuid(), :o, :w, 'upload', 'a')",
        o=DEFAULT_ORG_ID,
        w=DEFAULT_WORKSPACE_ID,
    )
    sql(env, "INSERT INTO orgs (id, name) VALUES (:id, 'org-b')", id=ORG_B)
    sql(env, "INSERT INTO workspaces (id, org_id, name) VALUES (:id, :o, 'ws-b')", id=WORKSPACE_B, o=ORG_B)
    async with browser(env.app) as a, browser(env.app) as b:
        await sign_in(env, a)
        await sign_in(env, b, email=OTHER)  # creates the user, without access
        sql(
            env,
            "INSERT INTO org_memberships (org_id, user_id, role) "
            "SELECT :o, id, 'owner' FROM users WHERE email = :e",
            o=ORG_B,
            e=OTHER,
        )
        await sign_in(env, b, email=OTHER)
        assert (await b.get("/api/auth/me")).json()["org"]["id"] == str(ORG_B)
        assert (await b.get("/api/sources")).status_code == 404
        assert [s["name"] for s in (await a.get("/api/sources")).json()["items"]] == ["a"]


async def test_idp_unavailable_only_affects_login(env):
    """With the IdP down, login answers 503 and the rest of the api keeps working."""

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    down = env.make(
        oidc=OidcClient(
            ISSUER, "tabayyun-api", CLIENT_SECRET, httpx.AsyncClient(transport=httpx.MockTransport(refuse))
        )
    )
    async with browser(down) as c:
        assert (await start(c, "google")).status_code == 503
        assert (await c.get("/api/version")).status_code == 200
        assert (await c.post("/api/auth/logout")).json() == {"logout_url": "/"}


async def test_dev_mode_has_no_login(db_url, fresh_schema):
    """dev mode: /me is the bootstrap user with method `dev`, the login routes answer 404."""
    fresh_schema("auto")
    app = create_app(app_settings(db_url))
    try:
        async with browser(app) as c:
            me = (await c.get("/api/auth/me")).json()
            assert me["sign_in_method"] == "dev" and me["role"] == "owner" and me["passkey_fresh"] is True
            assert (await c.get("/api/auth/login", params={"method": "google"})).status_code == 404
            assert set((await c.get("/api/auth/sign-in-options")).json().values()) == {False}
            assert (await c.get("/api/auth/sessions")).json() == []
            assert (await c.post("/api/auth/logout")).json() == {"logout_url": "/"}
            assert (await c.get("/api/sources")).status_code == 200
    finally:
        await app.state.engine.dispose()
