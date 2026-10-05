"""Security headers on every API response (spec 015).

The web edge (Caddy) sets the page headers; these cover the API itself, including a direct call
that bypasses the edge: no MIME sniffing, no shared caching of tenant data, no referrer. HTML
answers (the sign-in failure pages) also get a CSP that allows nothing but their inline style
and no framing. A header the route already set is left alone.

A pure ASGI middleware, so streamed bodies pass through untouched.
"""

from __future__ import annotations

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

API_HEADERS = {
    "x-content-type-options": "nosniff",
    "cache-control": "no-store",
    "referrer-policy": "no-referrer",
}
HTML_HEADERS = {
    "content-security-policy": (
        "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'none'"
    ),
    "x-frame-options": "DENY",
}
# The interactive API docs (dev only) load their own scripts and styles.
DOCS_PATHS = ("/api/docs", "/api/redoc")


class SecurityHeadersMiddleware:
    """Adds `API_HEADERS` to every response and `HTML_HEADERS` to HTML ones."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        docs = str(scope.get("path", "")).startswith(DOCS_PATHS)

        async def with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in API_HEADERS.items():
                    headers.setdefault(name, value)
                if headers.get("content-type", "").startswith("text/html") and not docs:
                    for name, value in HTML_HEADERS.items():
                        headers.setdefault(name, value)
            await send(message)

        await self.app(scope, receive, with_headers)
