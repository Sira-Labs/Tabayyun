"""Rate-limit dependencies for routes (spec 015).

`limit(bucket, key)` counts the request in its bucket before the route runs and raises
`RateLimitedError` over the limit; `create_app` answers 429 with `Retry-After`, as a short HTML
page for browser navigations (sign-in) and as `{"detail": "rate_limited"}` otherwise.
Keys: the client IP, the signed-in user, the user's org, or the user when signed in, else IP.
"""

from __future__ import annotations

import html as html_lib
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Annotated, Literal

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from tabayyun.auth.deps import client_ip, current_session, factory_of, settings_of
from tabayyun.authz import Principal, get_principal
from tabayyun.services import rate_limits

KeyKind = Literal["ip", "user", "org", "user_or_ip"]
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class RateLimitedError(Exception):
    """A request over its bucket's limit."""

    def __init__(self, bucket: str, retry_after: int, *, html: bool) -> None:
        super().__init__(bucket)
        self.bucket = bucket
        self.retry_after = retry_after
        self.html = html


def response_for(exc: RateLimitedError) -> Response:
    """The 429 answer: a page for a browser navigation, JSON for the web app's calls."""
    headers = {"Retry-After": str(exc.retry_after), "Cache-Control": "no-store"}
    if exc.html:
        body = (
            "<!doctype html><meta charset='utf-8'><title>Too many attempts</title>"
            f"<p>Too many sign-in attempts. Try again in {html_lib.escape(str(exc.retry_after))} seconds.</p>"
            "<p><a href='/login'>Back to sign-in</a></p>"
        )
        return HTMLResponse(body, status_code=429, headers=headers)
    return JSONResponse(status_code=429, content={"detail": "rate_limited"}, headers=headers)


async def _anonymous_key(request: Request, kind: KeyKind) -> str:
    if kind == "ip":
        return client_ip(request) or "unknown"
    session = await current_session(request)
    return f"user:{session.user_id}" if session else f"ip:{client_ip(request) or 'unknown'}"


async def _count(request: Request, rule: rate_limits.Rule, key: str, *, html: bool) -> None:
    clock: Callable[[], datetime] = getattr(request.app.state, "clock", rate_limits.utc_now)
    decision = await rate_limits.hit(factory_of(request), rule, key, clock())
    if not decision.allowed:
        raise RateLimitedError(rule.bucket, decision.retry_after, html=html)


def limit(
    bucket: str, key: KeyKind, *, html: bool = False, methods: frozenset[str] | None = None
) -> Callable[..., Awaitable[None]]:
    """A dependency counting the request in `bucket`; `methods` limits it to those methods.

    `user` and `org` keys take the request's principal (so the session check comes first);
    `ip` and `user_or_ip` work before any sign-in.
    """
    rule = rate_limits.RULES[bucket]

    def applies(request: Request) -> bool:
        return settings_of(request).rate_limits and (methods is None or request.method in methods)

    if key in ("user", "org"):

        async def by_principal(
            request: Request, principal: Annotated[Principal, Depends(get_principal)]
        ) -> None:
            if applies(request):
                who = principal.user_id if key == "user" else principal.org_id
                await _count(request, rule, str(who), html=html)

        return by_principal

    async def by_client(request: Request) -> None:
        if applies(request):
            await _count(request, rule, await _anonymous_key(request, key), html=html)

    return by_client
