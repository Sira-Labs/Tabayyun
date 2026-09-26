"""Request dependencies of the login (spec 013): the current session and the passkey gate.

The session is looked up once per request, in its own short transaction on the app login
without a tenant context, and kept on `request.state`.
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tabayyun.auth import store
from tabayyun.auth.store import CurrentSession
from tabayyun.auth.tokens import token_hash
from tabayyun.settings import Settings

log = structlog.get_logger()

SESSION_COOKIE = "__Host-tby_session"
LOGIN_COOKIE = "__Host-tby_login"
_STATE_KEY = "tabayyun_auth_session"


def settings_of(request: Request) -> Settings:
    """The app's settings."""
    settings: Settings = request.app.state.settings
    return settings


def factory_of(request: Request) -> async_sessionmaker[AsyncSession]:
    """The app's session factory, without a tenant context."""
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    return factory


def session_secret(settings: Settings) -> str:
    """The HMAC key of session and flow tokens; oidc mode refuses to start without one."""
    if settings.session_secret is None:
        raise RuntimeError("TABAYYUN_SESSION_SECRET is required with TABAYYUN_AUTH_MODE=oidc")
    return settings.session_secret.get_secret_value()


async def current_session(request: Request) -> CurrentSession | None:
    """The request's live session, or None. A disabled user's sessions are all revoked."""
    if hasattr(request.state, _STATE_KEY):
        cached: CurrentSession | None = getattr(request.state, _STATE_KEY)
        return cached
    settings = settings_of(request)
    token = request.cookies.get(SESSION_COOKIE)
    found: CurrentSession | None = None
    if token and settings.resolved_auth_mode == "oidc":
        async with factory_of(request)() as db, db.begin():
            found = await store.find_session(
                db, token_hash(session_secret(settings), token), idle=settings.session_idle
            )
            if found is not None and found.user_disabled:
                revoked = await store.revoke_user_sessions(db, found.user_id)
                log.info(
                    "auth.session_revoked", user_id=str(found.user_id), reason="user_disabled", count=revoked
                )
                found = None
    setattr(request.state, _STATE_KEY, found)
    return found


async def require_session(request: Request) -> CurrentSession:
    """The live session; 401 `not_authenticated` without one."""
    found = await current_session(request)
    if found is None:
        raise HTTPException(status_code=401, detail="not_authenticated")
    return found


def passkey_is_fresh(settings: Settings, session: CurrentSession | None) -> bool:
    """Whether the session signed in with a passkey within PASSKEY_FRESH (always in dev mode)."""
    if settings.resolved_auth_mode == "dev":
        return True
    if session is None or session.sign_in_method != "passkey":
        return False
    return datetime.now(UTC) - session.created_at < settings.passkey_fresh


async def require_recent_passkey(request: Request) -> None:
    """Admin gate for spec 014: 403 `second-factor-required` unless the session signed in with
    a passkey less than PASSKEY_FRESH ago (Arqam spec 012). Passes in dev mode."""
    settings = settings_of(request)
    if settings.resolved_auth_mode == "dev":
        return
    session = await require_session(request)
    if not passkey_is_fresh(settings, session):
        raise HTTPException(status_code=403, detail="second-factor-required")
