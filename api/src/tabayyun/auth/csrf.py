"""CSRF guard for unsafe requests (spec 013, behaviour 5).

Every POST, PUT, PATCH and DELETE must carry `X-Tabayyun-Request: 1`, which a cross-site form
cannot set, and an `Origin` header, when present, must be the public URL's origin. Stricter
than SameSite alone because the api takes multipart uploads. The back-channel logout is
exempt: the IdP posts it server to server, authenticated by the token's signature.

A pure ASGI middleware, so request bodies (uploads) stream through untouched.
"""

from __future__ import annotations

from urllib.parse import urlsplit

import structlog
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

log = structlog.get_logger()

CSRF_HEADER = "x-tabayyun-request"
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def origin_of(url: str | None) -> str | None:
    """`scheme://host[:port]` of a URL, or None when it has none."""
    if not url:
        return None
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}".lower()


class CsrfMiddleware:
    """Refuses unsafe requests without the CSRF header or from a foreign origin with 403 `csrf`."""

    def __init__(self, app: ASGIApp, *, public_url: str | None, exempt_paths: frozenset[str]) -> None:
        self.app = app
        self.allowed_origin = origin_of(public_url)
        self.exempt_paths = exempt_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Pass safe and exempt requests through; answer 403 for unsafe ones that fail."""
        if (
            scope["type"] == "http"
            and scope["method"] in UNSAFE_METHODS
            and scope["path"] not in self.exempt_paths
        ):
            headers = Headers(scope=scope)
            origin = headers.get("origin")
            reason = None
            if headers.get(CSRF_HEADER) != "1":
                reason = "header"
            elif (
                origin is not None
                and self.allowed_origin is not None
                and origin.lower() != self.allowed_origin
            ):
                reason = "origin"
            if reason is not None:
                log.info("auth.rejected", reason=f"csrf_{reason}", method=scope["method"], path=scope["path"])
                await JSONResponse(status_code=403, content={"detail": "csrf"})(scope, receive, send)
                return
        await self.app(scope, receive, send)
