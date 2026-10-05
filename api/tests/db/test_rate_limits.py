"""Rate limits end to end (spec 015): each bucket refuses the request after its limit with 429
and `Retry-After`, windows reset, keys are separate, a broken counter lets requests through,
the setting turns limits off, and the prune job keeps a day.

Sign-in buckets run in oidc mode against the fake IdP (spec 013); admin buckets in dev mode
with the principal overridden (`admin_support`). Each app gets a fixed clock.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from structlog.testing import capture_logs

from admin_support import ALICE, MIA
from db.test_auth_flow import ADMIN, browser, sign_in, sql
from tabayyun.db import migrate
from tabayyun.services import rate_limits
from tenancy import app_settings

T0 = datetime(2026, 10, 5, 9, 41, 30, tzinfo=UTC)  # 30 s into a minute


class Clock:
    """A settable clock for `app.state.clock`."""

    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def clocked(app, clock: Clock | None = None):
    app.state.clock = clock or Clock()
    return app


async def until_refused(call, limit: int):
    """Make `limit` allowed calls, then return the refused one."""
    for i in range(limit):
        r = await call()
        assert r.status_code != 429, (i, r.text)
    return await call()


# Sign-in buckets (oidc mode)


async def test_login_is_limited_per_ip_with_a_page(env):
    app = clocked(env.make())
    async with browser(app) as c:
        refused = await until_refused(lambda: c.get("/api/auth/login", params={"method": "google"}), 20)
        assert refused.status_code == 429
        assert refused.headers["retry-after"] == "30"
        assert refused.headers["content-type"].startswith("text/html")
        assert "Try again in 30 seconds" in refused.text
        # Another client address has its own counter.
        other = await c.get(
            "/api/auth/login", params={"method": "google"}, headers={"X-Real-IP": "203.0.113.7"}
        )
        assert other.status_code == 302


async def test_the_window_resets(env):
    clock = Clock()
    app = clocked(env.make(), clock)
    async with browser(app) as c:
        assert (await until_refused(lambda: c.get("/api/auth/callback"), 20)).status_code == 429
        clock.now = T0 + timedelta(seconds=30)  # the next minute
        assert (await c.get("/api/auth/callback")).status_code != 429


async def test_backchannel_is_limited_with_json(env):
    app = clocked(env.make())
    async with browser(app, csrf=False) as c:
        refused = await until_refused(
            lambda: c.post("/api/auth/backchannel-logout", data={"logout_token": "x"}), 60
        )
    assert (refused.status_code, refused.json()) == (429, {"detail": "rate_limited"})


async def test_device_routes_are_limited_per_user(env):
    app = clocked(env.make())
    async with browser(app) as c:
        await sign_in(env, c, "google", email=ADMIN)
        refused = await until_refused(lambda: c.post("/api/auth/sessions/revoke-others"), 30)
    assert refused.status_code == 429
    [(key,)] = sql(env, "SELECT key FROM rate_limits WHERE bucket = 'auth.sessions'")
    assert key.startswith("user:")


async def test_disabled_limits_never_refuse(env):
    app = clocked(env.make(rate_limits=False))
    async with browser(app) as c:
        for _ in range(25):
            assert (await c.get("/api/auth/login", params={"method": "google"})).status_code == 302
    assert sql(env, "SELECT count(*) FROM rate_limits") == [(0,)]


async def test_a_broken_counter_lets_requests_through(env, monkeypatch):
    monkeypatch.setattr(rate_limits, "HIT_SQL", text("SELECT no_such_column FROM rate_limits"))
    app = clocked(env.make())
    async with browser(app) as c:
        with capture_logs() as logs:
            for _ in range(25):
                assert (await c.get("/api/auth/login", params={"method": "google"})).status_code == 302
    assert any(e["event"] == "rate.unavailable" for e in logs)


# Admin buckets (dev mode)


async def test_admin_writes_are_limited_per_user_reads_are_not(admin_env):
    async with admin_env.client(ALICE) as alice, admin_env.client(MIA) as mia:
        for app in admin_env.apps:
            clocked(app)
        names = iter(range(1000))
        refused = await until_refused(
            lambda: alice.post("/api/admin/teams", json={"name": f"t{next(names)}"}), 60
        )
        assert (refused.status_code, refused.json()["detail"]) == (429, "rate_limited")
        assert refused.headers["retry-after"] == "30"
        assert (await alice.get("/api/admin/teams")).status_code == 200
        # Mia is not an admin: her write is counted under her own key and refused by role.
        assert (await mia.post("/api/admin/teams", json={"name": "x"})).status_code == 403
    assert admin_env.scalar("SELECT count(*) FROM teams") == 60


async def test_invitations_are_limited_per_org(admin_env):
    async with admin_env.client(ALICE) as alice:
        clocked(admin_env.apps[-1])
        n = iter(range(1000))
        refused = await until_refused(
            lambda: alice.post("/api/admin/invitations", json={"email": f"p{next(n)}@example.org"}), 50
        )
    assert refused.status_code == 429
    assert int(refused.headers["retry-after"]) == 1110  # the rest of the hour from 09:41:30
    assert admin_env.scalar("SELECT count(*) FROM invitations") == 50


# Pruning and the migration


async def test_prune_keeps_a_day(db_url, fresh_schema):
    fresh_schema("auto")
    owner = create_engine(db_url, isolation_level="AUTOCOMMIT")
    with owner.connect() as conn:
        for age in ("23 hours", "25 hours", "3 days"):
            conn.execute(
                text(
                    "INSERT INTO rate_limits (bucket, key, window_start, hits) "
                    "VALUES ('auth.login', :key, CAST(:now AS timestamptz) - CAST(:age AS interval), 1)"
                ),
                {"key": age, "age": age, "now": T0.isoformat()},
            )
    from tabayyun.main import create_app

    app = create_app(app_settings(db_url))
    try:
        assert await rate_limits.prune(app.state.session_factory, now=T0) == 2
    finally:
        await app.state.engine.dispose()
    with owner.connect() as conn:
        assert conn.execute(text("SELECT key FROM rate_limits")).scalars().all() == ["23 hours"]
    owner.dispose()


def test_0007_downgrades_and_upgrades(db_url, fresh_schema):
    fresh_schema("auto")
    migrate.downgrade(db_url, "0006")
    engine = create_engine(db_url)
    try:
        with engine.connect() as conn:
            assert conn.execute(text("SELECT to_regclass('rate_limits')")).scalar() is None
    finally:
        engine.dispose()
    migrate.upgrade(db_url, "head", timescale="auto")


@pytest.mark.parametrize("bucket", sorted(rate_limits.RULES))
def test_every_bucket_is_used_by_a_route(bucket):
    """A bucket in the table but on no route would be a limit that never applies."""
    import pathlib

    sources = "".join(
        p.read_text() for p in pathlib.Path(rate_limits.__file__).parents[1].joinpath("routers").glob("*.py")
    )
    assert f'"{bucket}"' in sources
