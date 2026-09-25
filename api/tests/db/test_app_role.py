"""The login the app runs as (spec 007): the startup check and the app login provisioning."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from structlog.testing import capture_logs

from tabayyun.db import make_engine
from tabayyun.db.roles import check_login, ensure_app_login, login_problems
from tabayyun.settings import Settings
from tenancy import APP_LOGIN, app_settings, app_url


async def test_owner_login_is_reported(db_url, fresh_schema):
    """The test server's owner is a superuser and owns the tables: both are named."""
    fresh_schema("auto")
    engine = make_engine(Settings(env="test", database_url=db_url))
    try:
        assert set(await login_problems(engine) or []) >= {"superuser", "table owner"}
    finally:
        await engine.dispose()


async def test_app_login_has_no_problems(db_url, fresh_schema):
    """The provisioned app login is neither superuser, nor exempt from RLS, nor owner."""
    fresh_schema("auto")
    engine = make_engine(app_settings(db_url))
    try:
        assert await login_problems(engine) == []
    finally:
        await engine.dispose()


@pytest.mark.parametrize(("env", "level"), [("prod", "error"), ("dev", "warning")])
async def test_check_login_logs_by_environment(db_url, fresh_schema, env, level):
    """A login that bypasses RLS logs `db.rls_bypassed`: an error in prod, a warning elsewhere;
    the process keeps running (spec 015 makes prod refuse)."""
    fresh_schema("auto")
    engine = make_engine(Settings(env="test", database_url=db_url))
    try:
        with capture_logs() as logs:
            problems = await check_login(engine, env)
    finally:
        await engine.dispose()
    assert problems
    events = [e for e in logs if e["event"] == "db.rls_bypassed"]
    assert len(events) == 1 and events[0]["log_level"] == level


async def test_check_login_is_quiet_for_the_app_login(db_url, fresh_schema):
    """The app login logs nothing."""
    fresh_schema("auto")
    engine = make_engine(app_settings(db_url))
    try:
        with capture_logs() as logs:
            assert await check_login(engine, "prod") == []
    finally:
        await engine.dispose()
    assert not [e for e in logs if e["event"] == "db.rls_bypassed"]


def test_ensure_app_login_skips_the_owner_and_is_idempotent(db_url, fresh_schema):
    """The owner's own URL provisions nothing; the app URL can be provisioned repeatedly."""
    fresh_schema("auto")
    assert ensure_app_login(db_url, db_url) is None
    assert ensure_app_login(db_url, app_url(db_url)) == APP_LOGIN
    assert ensure_app_login(db_url, app_url(db_url)) == APP_LOGIN
    engine = create_engine(app_url(db_url))
    try:
        with engine.connect() as conn:
            assert conn.execute(text("SELECT current_user")).scalar() == APP_LOGIN
            member = conn.execute(text("SELECT pg_has_role(current_user, 'tabayyun_app', 'member')")).scalar()
            assert member is True
    finally:
        engine.dispose()


def test_ensure_app_login_needs_a_password(db_url, fresh_schema):
    """An app URL without a password is a configuration error, not a passwordless login."""
    fresh_schema("auto")
    with pytest.raises(ValueError, match="password"):
        ensure_app_login(db_url, "postgresql+psycopg://someone@localhost/tabayyun")
