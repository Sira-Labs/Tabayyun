"""A fake OIDC identity provider served in process (spec 013 tests).

It serves discovery, the JWKS and the token endpoint through httpx's ASGI transport; the
authorization step is `authorize()`, which reads the api's redirect like a browser would and
returns the callback URL with a code for the given claims. The token endpoint checks the client
secret, the redirect URI and the PKCE verifier, as Keycloak does.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse
from joserfc import jwt
from joserfc.jwk import RSAKey

from tabayyun.auth.oidc import BACKCHANNEL_EVENT, OidcClient

ISSUER = "https://idp.test/realms/tabayyun"
CLIENT_ID = "tabayyun-api"
CLIENT_SECRET = "fake-idp-client-secret-for-tests"  # not a secret: a test fixture
_KEY = RSAKey.generate_key(2048, parameters={"kid": "k1", "use": "sig", "alg": "RS256"}, private=True)


def _b64(data: bytes) -> str:
    """base64url without padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


@dataclass
class _Grant:
    claims: dict[str, Any]
    challenge: str
    redirect_uri: str
    signer: Callable[[dict[str, Any]], str] | None


@dataclass
class FakeIdp:
    """One realm with one RSA key; `grants` maps codes to what the token endpoint returns."""

    key: RSAKey = field(default_factory=lambda: _KEY)
    grants: dict[str, _Grant] = field(default_factory=dict)

    def sign(self, claims: dict[str, Any], *, key: RSAKey | None = None, kid: str = "k1") -> str:
        """An RS256 JWT with `claims`, signed with the realm key or `key`."""
        return jwt.encode({"alg": "RS256", "kid": kid, "typ": "JWT"}, claims, key or self.key)

    def id_token_claims(self, *, email: str, sub: str, nonce: str, **extra: Any) -> dict[str, Any]:
        """Claims Keycloak puts in an ID token for this client; `extra` overrides any."""
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "aud": CLIENT_ID,
            "azp": CLIENT_ID,
            "sub": sub,
            "iat": now,
            "exp": now + 300,
            "nonce": nonce,
            "email": email,
            "email_verified": True,
            "name": email.split("@")[0].title(),
            "sid": f"sid-{sub}",
        }
        claims.update(extra)
        return {k: v for k, v in claims.items() if v is not None}

    def logout_token(self, **claims: Any) -> str:
        """A back-channel logout token; `claims` adds or overrides (None removes)."""
        body = {
            "iss": ISSUER,
            "aud": CLIENT_ID,
            "iat": int(time.time()),
            "jti": secrets.token_hex(8),
            "events": {BACKCHANNEL_EVENT: {}},
        }
        body.update(claims)
        return self.sign({k: v for k, v in body.items() if v is not None})

    def authorize(
        self,
        location: str,
        *,
        email: str,
        sub: str,
        signer: Callable[[dict[str, Any]], str] | None = None,
        **extra: Any,
    ) -> tuple[str, dict[str, list[str]]]:
        """Play the browser at the IdP: returns the callback path with a new code and the
        authorization request's parameters. `signer` turns the claims into the ID token in
        place of the realm key; `extra` overrides claims (e.g. `nonce`, `amr`,
        `identity_provider`; None removes one)."""
        query = parse_qs(urlsplit(location).query)
        claims = self.id_token_claims(email=email, sub=sub, nonce=query["nonce"][0])
        claims.update(extra)
        code = secrets.token_urlsafe(16)
        self.grants[code] = _Grant(
            claims={k: v for k, v in claims.items() if v is not None},
            challenge=query["code_challenge"][0],
            redirect_uri=query["redirect_uri"][0],
            signer=signer,
        )
        callback = urlsplit(query["redirect_uri"][0]).path
        return f"{callback}?{urlencode({'code': code, 'state': query['state'][0]})}", query

    def app(self) -> FastAPI:
        """Discovery, JWKS and token endpoint."""
        app = FastAPI()
        path = urlsplit(ISSUER).path

        @app.get(f"{path}/.well-known/openid-configuration")
        async def discovery() -> dict[str, Any]:
            """The endpoints Keycloak publishes, under the fake issuer."""
            base = f"{ISSUER}/protocol/openid-connect"
            return {
                "issuer": ISSUER,
                "authorization_endpoint": f"{base}/auth",
                "token_endpoint": f"{base}/token",
                "jwks_uri": f"{base}/certs",
                "end_session_endpoint": f"{base}/logout",
            }

        @app.get(f"{path}/protocol/openid-connect/certs")
        async def certs() -> dict[str, Any]:
            """The public half of the realm key."""
            return {"keys": [self.key.as_dict(private=False)]}

        @app.post(f"{path}/protocol/openid-connect/token")
        async def token(
            request: Request,
            grant_type: str = Form(),
            code: str = Form(),
            redirect_uri: str = Form(),
            code_verifier: str = Form(),
        ) -> JSONResponse:
            """Code exchange: client secret, redirect URI and PKCE verifier must match."""
            expected = "Basic " + base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
            if request.headers.get("authorization") != expected:
                return JSONResponse({"error": "unauthorized_client"}, status_code=401)
            grant = self.grants.pop(code, None)
            if grant_type != "authorization_code" or grant is None or grant.redirect_uri != redirect_uri:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            if _b64(hashlib.sha256(code_verifier.encode()).digest()) != grant.challenge:
                return JSONResponse({"error": "invalid_grant", "error_description": "PKCE"}, status_code=400)
            id_token = (grant.signer or self.sign)(grant.claims)
            return JSONResponse({"access_token": "at", "token_type": "Bearer", "id_token": id_token})

        return app

    def client(self) -> OidcClient:
        """An OIDC client for the api that reaches this IdP in process."""
        http = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app()))
        return OidcClient(ISSUER, CLIENT_ID, CLIENT_SECRET, http)


def unsigned(claims: dict[str, Any]) -> str:
    """A JWT with `alg: none` (must be refused)."""
    header = _b64(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    return f"{header}.{_b64(json.dumps(claims).encode())}."
