"""Login, session and device endpoints under `/api/auth` (spec 013).

None of these routes goes through `authorize()`: they establish who the caller is. In `dev`
mode there is no login: `/me` describes the bootstrap user and the login routes answer 404.
"""

from __future__ import annotations

import html
import ipaddress
import uuid
from datetime import datetime
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Form, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select

from tabayyun.auth import store
from tabayyun.auth.deps import (
    LOGIN_COOKIE,
    SESSION_COOKIE,
    current_session,
    factory_of,
    passkey_is_fresh,
    require_session,
    session_secret,
    settings_of,
)
from tabayyun.auth.oidc import IdpError, IdpUnavailableError, InvalidTokenError, OidcClient
from tabayyun.auth.store import CurrentSession, Flow
from tabayyun.auth.tokens import new_token, safe_next, token_hash
from tabayyun.db import for_org
from tabayyun.db.models import BOOTSTRAP_USER_ID, DEFAULT_ORG_ID, SIGN_IN_METHODS, Org, OrgMembership, User
from tabayyun.settings import Settings

log = structlog.get_logger()

router = APIRouter(prefix="/api/auth", tags=["auth"])

CALLBACK_PATH = "/api/auth/callback"
BACKCHANNEL_PATH = "/api/auth/backchannel-logout"
FLOW_COOKIE_MAX_AGE_S = 600
USER_AGENT_MAX = 512
# Keycloak's account console page listing passkeys ("Signing in"), relative to the issuer.
ACCOUNT_SIGNING_IN = "/account/account-security/signing-in"


class UserOut(BaseModel):
    """The signed-in user."""

    id: str
    email: str
    display_name: str


class OrgOut(BaseModel):
    """The org the session acts in."""

    id: str
    name: str


class MeOut(BaseModel):
    """`GET /api/auth/me`."""

    user: UserOut
    org: OrgOut
    role: str
    sign_in_method: str
    passkey_fresh: bool


class DeviceOut(BaseModel):
    """One signed-in browser of the user; never tokens."""

    id: str
    current: bool
    sign_in_method: str
    created_at: datetime
    last_seen_at: datetime
    user_agent: str | None
    ip_address: str | None


class LogoutOut(BaseModel):
    """Where the browser goes to end the IdP session too."""

    logout_url: str


def _oidc(request: Request) -> OidcClient:
    """The app's OIDC client; 404 in dev mode, where there is no login."""
    client: OidcClient | None = request.app.state.oidc
    if client is None:
        raise HTTPException(status_code=404, detail="oidc_disabled")
    return client


def _public_url(settings: Settings) -> str:
    # Set whenever the OIDC client exists (create_app checks it).
    return (settings.public_url or "").rstrip("/")


def _set_cookie(response: Response, name: str, value: str, max_age: int) -> None:
    response.set_cookie(name, value, max_age=max_age, path="/", secure=True, httponly=True, samesite="lax")


def _clear_cookie(response: Response, name: str) -> None:
    response.delete_cookie(name, path="/", secure=True, httponly=True, samesite="lax")


def _failure(status: int, code: str) -> HTMLResponse:
    """A failed callback: the browser is mid-navigation, so a short page with a way back."""
    safe = html.escape(code)
    body = (
        "<!doctype html><meta charset='utf-8'><title>Sign-in failed</title>"
        f"<p>Sign-in failed ({safe}).</p><p><a href='/login'>Sign in again</a></p>"
    )
    response = HTMLResponse(body, status_code=status, headers={"Cache-Control": "no-store"})
    _clear_cookie(response, LOGIN_COOKIE)
    return response


def _client_ip(request: Request) -> str | None:
    """The client's IP from `X-Real-IP` behind the proxy, else the peer; informational only."""
    for candidate in (request.headers.get("x-real-ip"), request.client.host if request.client else None):
        if candidate:
            try:
                return str(ipaddress.ip_address(candidate.strip()))
            except ValueError:
                continue
    return None


def method_proven(method: str, claims: dict[str, Any]) -> bool:
    """Whether the ID token proves the sign-in method the flow asked for.

    Only that method's claim counts: within one Keycloak session the other one can be stale
    (spec 013, implementation notes). `kc_idp_hint` only routes, so a Google flow could return
    through GitHub; the `identity_provider` claim tells.
    """
    if method == "passkey":
        amr = claims.get("amr")
        return isinstance(amr, list) and "passkey" in amr
    return claims.get("identity_provider") == method


@router.get("/sign-in-options")
async def sign_in_options(request: Request) -> dict[str, bool]:
    """Which sign-in buttons the login page shows; none in dev mode."""
    settings = settings_of(request)
    enabled = set(settings.enabled_sign_in_methods) if request.app.state.oidc is not None else set()
    return {method: method in enabled for method in SIGN_IN_METHODS}


@router.get("/login")
async def login(
    request: Request, method: Annotated[str, Query()], next: Annotated[str | None, Query()] = None
) -> Response:
    """Start a login: store the flow and send the browser to the IdP."""
    oidc = _oidc(request)
    settings = settings_of(request)
    if method not in settings.enabled_sign_in_methods:
        raise HTTPException(status_code=400, detail="unknown_method")
    flow = Flow(
        state=new_token(),
        nonce=new_token(),
        code_verifier=new_token(),
        method=method,
        next_path=safe_next(next),
    )
    try:
        url = await oidc.authorization_url(
            method=method,
            state=flow.state,
            nonce=flow.nonce,
            code_verifier=flow.code_verifier,
            redirect_uri=_public_url(settings) + CALLBACK_PATH,
        )
    except IdpUnavailableError as exc:
        raise HTTPException(status_code=503, detail="idp_unavailable") from exc
    flow_token = new_token()
    async with factory_of(request)() as db, db.begin():
        await store.create_flow(db, token_hash(session_secret(settings), flow_token), flow)
    response = RedirectResponse(url, status_code=302, headers={"Cache-Control": "no-store"})
    _set_cookie(response, LOGIN_COOKIE, flow_token, FLOW_COOKIE_MAX_AGE_S)
    return response


@router.get("/callback")
async def callback(
    request: Request,
    state: Annotated[str | None, Query()] = None,
    code: Annotated[str | None, Query()] = None,
    error: Annotated[str | None, Query()] = None,
) -> Response:
    """Finish a login: check the flow and the ID token, link the user, start a session."""
    oidc = _oidc(request)
    settings = settings_of(request)
    secret = session_secret(settings)
    flow_token = request.cookies.get(LOGIN_COOKIE)
    taken = None
    if flow_token:
        # Its own transaction: the flow is gone whatever happens next (single use).
        async with factory_of(request)() as db, db.begin():
            taken = await store.take_flow(db, token_hash(secret, flow_token))
    if taken is None or not taken[1] or state is None or taken[0].state != state:
        log.info("auth.rejected", reason="login_expired")
        return _failure(400, "login_expired")
    flow = taken[0]
    if error is not None or not code:
        log.info("auth.rejected", reason="idp_redirect_error", method=flow.method, error=(error or "")[:64])
        return _failure(400, "login_cancelled" if error == "access_denied" else "idp_error")

    try:
        tokens = await oidc.exchange_code(
            code=code, code_verifier=flow.code_verifier, redirect_uri=_public_url(settings) + CALLBACK_PATH
        )
        id_token = str(tokens["id_token"])
        claims = await oidc.validate_id_token(id_token, nonce=flow.nonce)
    except IdpUnavailableError:
        return _failure(503, "idp_unavailable")
    except IdpError as exc:
        log.warning("auth.rejected", reason="idp_error", method=flow.method, idp_error=exc.code[:64])
        return _failure(502, "idp_error")
    except InvalidTokenError as exc:
        log.info("auth.rejected", reason="invalid_token", method=flow.method, detail=exc.reason)
        return _failure(400, "invalid_token")
    if not method_proven(flow.method, claims):
        reason = "passkey_required" if flow.method == "passkey" else "invalid_token"
        log.info("auth.rejected", reason=reason, method=flow.method, detail="method_claim")
        return _failure(400, reason)

    session_token = new_token()
    async with factory_of(request)() as db, db.begin():
        result = await store.login(
            db,
            issuer=oidc.issuer,
            subject=str(claims["sub"]),
            email=str(claims["email"]).strip().lower(),
            email_verified=True,
            display_name=str(claims.get("name") or ""),
            admin_email=settings.admin_email,
        )
        if result.disabled:
            await store.revoke_user_sessions(db, result.user_id)
        else:
            await store.create_session(
                db,
                id_hash=token_hash(secret, session_token),
                user_id=result.user_id,
                org_id=result.org_id,
                sign_in_method=flow.method,
                idp_sid=str(claims["sid"]) if claims.get("sid") else None,
                id_token=id_token,
                ip_address=_client_ip(request),
                user_agent=(request.headers.get("user-agent") or "")[:USER_AGENT_MAX] or None,
                absolute=settings.session_absolute,
                idle=settings.session_idle,
            )
    if result.disabled:
        log.info("auth.rejected", reason="user_disabled", user_id=str(result.user_id), method=flow.method)
        return _failure(403, "account_disabled")
    log.info(
        "auth.login",
        user_id=str(result.user_id),
        method=flow.method,
        org_id=str(result.org_id) if result.org_id else None,
        role=result.role,
    )
    response = RedirectResponse(flow.next_path, status_code=302, headers={"Cache-Control": "no-store"})
    _clear_cookie(response, LOGIN_COOKIE)
    _set_cookie(response, SESSION_COOKIE, session_token, int(settings.session_absolute.total_seconds()))
    return response


async def _describe(request: Request, user_id: uuid.UUID, org_id: uuid.UUID) -> tuple[User, Org, str] | None:
    """The user, the org and their role in it, read in the org's tenant context."""
    async with for_org(factory_of(request), org_id)() as db, db.begin():
        row = (
            await db.execute(
                select(User, Org, OrgMembership.role)
                .join(OrgMembership, OrgMembership.user_id == User.id)
                .join(Org, Org.id == OrgMembership.org_id)
                .where(User.id == user_id, Org.id == org_id)
            )
        ).one_or_none()
    return None if row is None else (row[0], row[1], row[2])


async def _email_of(request: Request, user_id: uuid.UUID) -> str | None:
    async with factory_of(request)() as db, db.begin():
        email: str | None = await db.scalar(select(User.email).where(User.id == user_id))
    return email


@router.get("/me", response_model=MeOut)
async def me(request: Request) -> Any:
    """The signed-in user, their org and role, the sign-in method and passkey freshness.

    401 without a session; 403 `no_access` (with the email, for the "No access yet" page) for a
    user without a membership.
    """
    settings = settings_of(request)
    session: CurrentSession | None = None
    org_id: uuid.UUID | None
    if settings.resolved_auth_mode == "dev":
        user_id, org_id, method = BOOTSTRAP_USER_ID, DEFAULT_ORG_ID, "dev"
    else:
        session = await require_session(request)
        user_id, org_id, method = session.user_id, session.org_id, session.sign_in_method
    described = await _describe(request, user_id, org_id) if org_id is not None else None
    if described is None:
        return JSONResponse(
            status_code=403, content={"detail": "no_access", "email": await _email_of(request, user_id)}
        )
    user, org, role = described
    return MeOut(
        user=UserOut(id=str(user.id), email=user.email, display_name=user.display_name),
        org=OrgOut(id=str(org.id), name=org.name),
        role=role,
        sign_in_method=method,
        passkey_fresh=passkey_is_fresh(settings, session),
    )


@router.get("/sessions", response_model=list[DeviceOut])
async def list_devices(request: Request) -> list[DeviceOut]:
    """The user's signed-in browsers; empty in dev mode."""
    settings = settings_of(request)
    if settings.resolved_auth_mode == "dev":
        return []
    current = await require_session(request)
    async with factory_of(request)() as db, db.begin():
        rows = await store.list_sessions(db, current.user_id, idle=settings.session_idle)
    return [
        DeviceOut(
            id=str(row.id),
            current=row.id == current.id,
            sign_in_method=row.sign_in_method,
            created_at=row.created_at,
            last_seen_at=row.last_seen_at,
            user_agent=row.user_agent,
            ip_address=str(row.ip_address) if row.ip_address is not None else None,
        )
        for row in rows
    ]


@router.delete("/sessions/{session_id}", status_code=204)
async def revoke_device(request: Request, session_id: uuid.UUID) -> Response:
    """Sign out one of the user's browsers; the current one clears its cookie too."""
    current = await require_session(request)
    async with factory_of(request)() as db, db.begin():
        revoked = await store.revoke(db, session_id, user_id=current.user_id)
    if not revoked:
        raise HTTPException(status_code=404, detail="not found")
    log.info(
        "auth.session_revoked",
        user_id=str(current.user_id),
        reason="device",
        current=session_id == current.id,
    )
    response = Response(status_code=204)
    if session_id == current.id:
        _clear_cookie(response, SESSION_COOKIE)
    return response


@router.post("/sessions/revoke-others", status_code=204)
async def revoke_other_devices(request: Request) -> Response:
    """Sign out every other browser of the user."""
    current = await require_session(request)
    async with factory_of(request)() as db, db.begin():
        count = await store.revoke_user_sessions(db, current.user_id, keep=current.id)
    log.info("auth.session_revoked", user_id=str(current.user_id), reason="others", count=count)
    return Response(status_code=204)


@router.post("/logout", response_model=LogoutOut)
async def logout(request: Request) -> Any:
    """Revoke the session, clear the cookie and return the IdP's end-session URL."""
    oidc: OidcClient | None = request.app.state.oidc
    session = await current_session(request)
    if session is not None:
        async with factory_of(request)() as db, db.begin():
            await store.revoke(db, session.id, user_id=session.user_id)
        log.info("auth.logout", user_id=str(session.user_id), method=session.sign_in_method)
    logout_url = "/"
    if oidc is not None:
        try:
            logout_url = (
                await oidc.end_session_url(
                    id_token_hint=session.id_token if session else None,
                    post_logout_redirect_uri=_public_url(settings_of(request)) + "/",
                )
                or "/"
            )
        except IdpUnavailableError:
            # Signed out here anyway; the IdP session ends at its own idle timeout.
            logout_url = "/"
    response = JSONResponse({"logout_url": logout_url}, headers={"Cache-Control": "no-store"})
    _clear_cookie(response, SESSION_COOKIE)
    return response


@router.post("/backchannel-logout")
async def backchannel_logout(request: Request, logout_token: Annotated[str, Form()]) -> Response:
    """OIDC back-channel logout from the IdP: revoke the sessions of its `sid` (or `sub`)."""
    oidc = _oidc(request)
    headers = {"Cache-Control": "no-store"}
    try:
        claims = await oidc.validate_logout_token(logout_token)
    except InvalidTokenError as exc:
        log.info("auth.rejected", reason="invalid_logout_token", detail=exc.reason)
        return JSONResponse(status_code=400, content={"detail": "invalid_token"}, headers=headers)
    except IdpUnavailableError:
        return JSONResponse(status_code=503, content={"detail": "idp_unavailable"}, headers=headers)
    async with factory_of(request)() as db, db.begin():
        count = await store.revoke_by_idp(
            db,
            issuer=oidc.issuer,
            sid=str(claims["sid"]) if claims.get("sid") else None,
            subject=str(claims["sub"]) if claims.get("sub") else None,
        )
    log.info("auth.session_revoked", reason="backchannel", count=count)
    return Response(status_code=200, headers=headers)


@router.get("/passkeys")
async def manage_passkeys(request: Request) -> Response:
    """Send the user to Keycloak's account console, where passkeys are added and removed."""
    oidc = _oidc(request)
    return RedirectResponse(oidc.issuer + ACCOUNT_SIGNING_IN, status_code=302)
