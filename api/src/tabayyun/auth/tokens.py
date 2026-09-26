"""Random tokens, their stored HMAC, PKCE and the `next` parameter (spec 013).

Cookies carry a random token; the database holds only HMAC-SHA256(session secret, token), so a
leaked table cannot be replayed as cookies.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from urllib.parse import urlsplit

TOKEN_BYTES = 32


def new_token() -> str:
    """A random 32-byte token, base64url without padding."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def token_hash(secret: str, token: str) -> bytes:
    """HMAC-SHA256 of `token` keyed by the session secret: what the database stores."""
    return hmac.new(secret.encode(), token.encode(), hashlib.sha256).digest()


def pkce_challenge(verifier: str) -> str:
    """The S256 code challenge of a PKCE verifier (RFC 7636)."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def safe_next(value: str | None) -> str:
    """`value` when it is a path on this site, else `/` (no open redirect).

    Accepted: starts with a single `/`, has no scheme or host, and no backslash or control
    character (browsers read `/\\evil.example` as `//evil.example`).
    """
    if not value or not value.startswith("/") or value.startswith("//"):
        return "/"
    if "\\" in value or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        return "/"
    parts = urlsplit(value)
    if parts.scheme or parts.netloc:
        return "/"
    return value
