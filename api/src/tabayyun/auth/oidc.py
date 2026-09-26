"""OIDC relying party for the backend-for-frontend login (spec 013).

Talks to one issuer (the Keycloak realm): discovery and JWKS, fetched lazily and cached for an
hour; the authorization URL per sign-in method; the code exchange with the client secret and
the PKCE verifier; ID and back-channel logout token validation; the end-session URL.

JOSE is joserfc, Authlib's JOSE library: Authlib 1.8 deprecates its own `authlib.jose` and its
httpx client in favour of it, so the HTTP side is plain httpx (spec 013, implementation notes).
The httpx client is injected, which lets tests serve a fake identity provider in process.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
import structlog
from joserfc import jwt
from joserfc.errors import InvalidKeyIdError, JoseError
from joserfc.jwk import KeySet
from joserfc.jwt import JWTClaimsRegistry

from tabayyun.auth.tokens import pkce_challenge

log = structlog.get_logger()

CACHE_TTL_S = 3600.0
# A token naming an unknown key triggers at most one JWKS refetch per this many seconds, so
# forged key ids cannot make the api hammer the IdP.
JWKS_REFRESH_MIN_S = 60.0
LEEWAY_S = 60
ALGORITHMS = ("RS256", "ES256")
SCOPE = "openid email profile"
BACKCHANNEL_EVENT = "http://schemas.openid.net/event/backchannel-logout"
HTTP_TIMEOUT_S = 10.0


class IdpUnavailableError(Exception):
    """The issuer's discovery document or JWKS could not be fetched."""


class IdpError(Exception):
    """The token endpoint refused the code; `code` is the IdP's `error` value."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class InvalidTokenError(Exception):
    """An ID or logout token failed validation; `reason` is for the log only."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ProviderMetadata:
    """The endpoints of the issuer's discovery document the api uses."""

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    end_session_endpoint: str | None


@dataclass
class _Cache:
    metadata: ProviderMetadata
    keys: KeySet
    fetched_at: float
    keys_fetched_at: float


class OidcClient:
    """One issuer and one confidential client; safe to share across requests."""

    def __init__(
        self,
        issuer: str,
        client_id: str,
        client_secret: str,
        http: httpx.AsyncClient | None = None,
        *,
        clock: Any = time.monotonic,
    ) -> None:
        self.issuer = issuer.rstrip("/")
        self.client_id = client_id
        self._client_secret = client_secret
        self._http = http or httpx.AsyncClient(timeout=HTTP_TIMEOUT_S)
        self._clock = clock
        self._cache: _Cache | None = None
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        """Close the HTTP client."""
        await self._http.aclose()

    async def metadata(self) -> ProviderMetadata:
        """Discovery document, cached for an hour; raises IdpUnavailableError."""
        return (await self._cached()).metadata

    async def _cached(self) -> _Cache:
        """Discovery document and JWKS, fetched again once they are an hour old."""
        async with self._lock:
            now = self._clock()
            if self._cache is None or now - self._cache.fetched_at > CACHE_TTL_S:
                metadata = await self._discover()
                keys = await self._fetch_keys(metadata.jwks_uri)
                self._cache = _Cache(metadata, keys, now, now)
            return self._cache

    async def _get_json(self, url: str) -> dict[str, Any]:
        """A JSON object from the IdP; IdpUnavailableError on any transport or format error."""
        try:
            response = await self._http.get(url)
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("auth.idp_unavailable", url=url, error=type(exc).__name__)
            raise IdpUnavailableError(url) from exc
        if not isinstance(body, dict):
            raise IdpUnavailableError(url)
        return body

    async def _discover(self) -> ProviderMetadata:
        """Fetch and check the discovery document."""
        doc = await self._get_json(f"{self.issuer}/.well-known/openid-configuration")
        try:
            metadata = ProviderMetadata(
                issuer=str(doc["issuer"]),
                authorization_endpoint=str(doc["authorization_endpoint"]),
                token_endpoint=str(doc["token_endpoint"]),
                jwks_uri=str(doc["jwks_uri"]),
                end_session_endpoint=doc.get("end_session_endpoint"),
            )
        except KeyError as exc:
            raise IdpUnavailableError(f"discovery document lacks {exc}") from exc
        if metadata.issuer.rstrip("/") != self.issuer:
            # OIDC Discovery 4.3: the document must name the issuer it was fetched for.
            raise IdpUnavailableError(f"discovery issuer {metadata.issuer!r} is not {self.issuer!r}")
        return metadata

    async def _fetch_keys(self, jwks_uri: str) -> KeySet:
        """The IdP's RSA and EC signing keys."""
        body = await self._get_json(jwks_uri)
        # Encryption keys and algorithms outside ALGORITHMS are of no use for verification.
        keys = [
            k for k in body.get("keys", []) if k.get("use", "sig") == "sig" and k.get("kty") in ("RSA", "EC")
        ]
        try:
            return KeySet.import_key_set({"keys": keys})
        except (JoseError, ValueError) as exc:
            raise IdpUnavailableError(f"unusable JWKS: {type(exc).__name__}") from exc

    async def _refresh_keys(self) -> bool:
        """Refetch the JWKS after a key-id miss, rate-limited; True when it was refetched."""
        async with self._lock:
            cache = self._cache
            if cache is None or self._clock() - cache.keys_fetched_at < JWKS_REFRESH_MIN_S:
                return False
            cache.keys = await self._fetch_keys(cache.metadata.jwks_uri)
            cache.keys_fetched_at = self._clock()
            return True

    async def authorization_url(
        self, *, method: str, state: str, nonce: str, code_verifier: str, redirect_uri: str
    ) -> str:
        """Where the browser goes to sign in with `method` (google, github or passkey).

        `kc_idp_hint` sends Google and GitHub straight to the broker; `prompt=login` makes the
        passkey method authenticate afresh even inside an SSO session, so `amr` is current.
        """
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "scope": SCOPE,
            "state": state,
            "nonce": nonce,
            "code_challenge": pkce_challenge(code_verifier),
            "code_challenge_method": "S256",
        }
        if method == "passkey":
            params["prompt"] = "login"
        else:
            params["kc_idp_hint"] = method
        metadata = await self.metadata()
        return f"{metadata.authorization_endpoint}?{urlencode(params)}"

    async def exchange_code(self, *, code: str, code_verifier: str, redirect_uri: str) -> dict[str, Any]:
        """Token response for an authorization code; raises IdpError or IdpUnavailableError."""
        metadata = await self.metadata()
        try:
            response = await self._http.post(
                metadata.token_endpoint,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "code_verifier": code_verifier,
                },
                auth=(self.client_id, self._client_secret),
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            log.warning("auth.idp_unavailable", url=metadata.token_endpoint, error=type(exc).__name__)
            raise IdpUnavailableError(metadata.token_endpoint) from exc
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code != 200 or not isinstance(body, dict) or "id_token" not in body:
            error = body.get("error") if isinstance(body, dict) else None
            raise IdpError(str(error or f"http_{response.status_code}"))
        return body

    async def _decode(self, token: str) -> dict[str, Any]:
        """Signature check against the JWKS, refetched once on an unknown key id."""
        keys = (await self._cached()).keys
        try:
            try:
                decoded = jwt.decode(token, keys, algorithms=list(ALGORITHMS))
            except InvalidKeyIdError:
                if not await self._refresh_keys():
                    raise
                decoded = jwt.decode(token, (await self._cached()).keys, algorithms=list(ALGORITHMS))
        except (JoseError, ValueError) as exc:
            raise InvalidTokenError(f"signature: {type(exc).__name__}") from exc
        return dict(decoded.claims)

    def _validate(self, claims: dict[str, Any], **options: Any) -> None:
        """Check `iss`, `aud` (and `azp` with several audiences) plus `options`, with leeway."""
        registry = JWTClaimsRegistry(
            leeway=LEEWAY_S,
            iss={"essential": True, "value": self.issuer},
            aud={"essential": True, "value": self.client_id},
            **options,
        )
        try:
            registry.validate(claims)
        except JoseError as exc:
            raise InvalidTokenError(f"claims: {type(exc).__name__}") from exc
        aud = claims.get("aud")
        if isinstance(aud, list) and len(aud) > 1 and claims.get("azp") != self.client_id:
            raise InvalidTokenError("claims: azp")

    async def validate_id_token(self, id_token: str, *, nonce: str) -> dict[str, Any]:
        """Claims of a valid ID token for this client and `nonce` with a verified email."""
        claims = await self._decode(id_token)
        self._validate(
            claims,
            exp={"essential": True},
            iat={"essential": True},
            sub={"essential": True},
            nonce={"essential": True, "value": nonce},
        )
        if claims.get("email_verified") is not True or not str(claims.get("email") or "").strip():
            raise InvalidTokenError("email_verified")
        return claims

    async def validate_logout_token(self, logout_token: str) -> dict[str, Any]:
        """Claims of a valid back-channel logout token (OIDC Back-Channel Logout 2.6)."""
        claims = await self._decode(logout_token)
        self._validate(claims, iat={"essential": True})
        events = claims.get("events")
        if not isinstance(events, dict) or BACKCHANNEL_EVENT not in events:
            raise InvalidTokenError("events")
        if "nonce" in claims:
            raise InvalidTokenError("nonce")
        if not claims.get("sid") and not claims.get("sub"):
            raise InvalidTokenError("sid_or_sub")
        return claims

    async def end_session_url(
        self, *, id_token_hint: str | None, post_logout_redirect_uri: str
    ) -> str | None:
        """The IdP's logout URL for the browser, or None when it has no end-session endpoint."""
        endpoint = (await self.metadata()).end_session_endpoint
        if not endpoint:
            return None
        params = {"client_id": self.client_id, "post_logout_redirect_uri": post_logout_redirect_uri}
        if id_token_hint:
            params["id_token_hint"] = id_token_hint
        return f"{endpoint}?{urlencode(params)}"
