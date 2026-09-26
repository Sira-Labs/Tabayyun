"""The OIDC client against the fake IdP: URLs, code exchange and token validation (spec 013)."""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from joserfc.jwk import RSAKey

from fake_idp import CLIENT_ID, ISSUER, FakeIdp, unsigned
from tabayyun.auth.deps import passkey_is_fresh
from tabayyun.auth.oidc import IdpError, IdpUnavailableError, InvalidTokenError, OidcClient
from tabayyun.auth.store import CurrentSession
from tabayyun.auth.tokens import pkce_challenge
from tabayyun.routers.auth import method_proven
from tabayyun.settings import Settings

NONCE = "n-123"
OTHER_KEY = RSAKey.generate_key(2048, parameters={"kid": "k1"}, private=True)


@pytest.fixture
def idp() -> FakeIdp:
    return FakeIdp()


def _claims(idp: FakeIdp, **extra):
    """ID token claims for Bo with `extra` overriding any claim."""
    return {**idp.id_token_claims(email="Bo@Example.org", sub="sub-bo", nonce=NONCE), **extra}


async def test_valid_id_token(idp):
    """A token signed by the realm for this client and nonce yields its claims."""
    claims = await idp.client().validate_id_token(idp.sign(_claims(idp)), nonce=NONCE)
    assert claims["sub"] == "sub-bo" and claims["email_verified"] is True


@pytest.mark.parametrize(
    "case",
    [
        "wrong_nonce",
        "wrong_audience",
        "wrong_issuer",
        "expired",
        "bad_signature",
        "alg_none",
        "email_unverified",
        "no_email",
        "foreign_azp",
        "garbage",
    ],
)
async def test_id_token_validation(idp, case):
    """Every failure of spec 013 behaviour 3 is an InvalidTokenError."""
    now = int(time.time())
    token = {
        "wrong_nonce": lambda: idp.sign(_claims(idp, nonce="other")),
        "wrong_audience": lambda: idp.sign(_claims(idp, aud="other-client", azp="other-client")),
        "wrong_issuer": lambda: idp.sign(_claims(idp, iss="https://evil.example/realms/tabayyun")),
        "expired": lambda: idp.sign(_claims(idp, iat=now - 900, exp=now - 120)),
        "bad_signature": lambda: idp.sign(_claims(idp), key=OTHER_KEY),
        "alg_none": lambda: unsigned(_claims(idp)),
        "email_unverified": lambda: idp.sign(_claims(idp, email_verified=False)),
        "no_email": lambda: idp.sign(_claims(idp, email="")),
        "foreign_azp": lambda: idp.sign(_claims(idp, aud=[CLIENT_ID, "other"], azp="other")),
        "garbage": lambda: "not-a-jwt",
    }[case]()
    with pytest.raises(InvalidTokenError):
        await idp.client().validate_id_token(token, nonce=NONCE)


async def test_expiry_has_leeway(idp):
    """A token that expired less than 60 s ago still passes (clock skew)."""
    now = int(time.time())
    await idp.client().validate_id_token(idp.sign(_claims(idp, exp=now - 30)), nonce=NONCE)


async def test_key_rotation_refetches_the_jwks_once(idp):
    """A token with an unknown key id refetches the JWKS, at most once a minute."""
    clock = [1000.0]
    client = OidcClient(
        ISSUER,
        CLIENT_ID,
        "s",
        httpx.AsyncClient(transport=httpx.ASGITransport(app=idp.app())),
        clock=lambda: clock[0],
    )
    await client.validate_id_token(idp.sign(_claims(idp)), nonce=NONCE)
    idp.key = RSAKey.generate_key(2048, parameters={"kid": "k2", "use": "sig"}, private=True)
    rotated = idp.sign(_claims(idp), kid="k2")
    with pytest.raises(InvalidTokenError):  # within the minute: no refetch
        await client.validate_id_token(rotated, nonce=NONCE)
    clock[0] += 61
    assert (await client.validate_id_token(rotated, nonce=NONCE))["sub"] == "sub-bo"


async def test_discovery_must_name_its_issuer(idp):
    """A discovery document for another issuer, or none at all, makes the IdP unavailable."""
    wrong = OidcClient(
        "https://idp.test/realms/other",
        CLIENT_ID,
        "s",
        httpx.AsyncClient(transport=httpx.ASGITransport(app=idp.app())),
    )
    with pytest.raises(IdpUnavailableError):
        await wrong.metadata()

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    down = OidcClient(ISSUER, CLIENT_ID, "s", httpx.AsyncClient(transport=httpx.MockTransport(refuse)))
    with pytest.raises(IdpUnavailableError):
        await down.metadata()


@pytest.mark.parametrize("method", ["google", "github", "passkey"])
async def test_authorization_url_per_method(idp, method):
    """Code flow with PKCE S256; Google and GitHub carry the broker hint, passkey forces a
    fresh authentication."""
    url = await idp.client().authorization_url(
        method=method, state="st", nonce=NONCE, code_verifier="v" * 43, redirect_uri="https://t.example/cb"
    )
    query = parse_qs(urlsplit(url).query)
    assert url.startswith(f"{ISSUER}/protocol/openid-connect/auth?")
    assert query["response_type"] == ["code"] and query["scope"] == ["openid email profile"]
    assert query["code_challenge"] == [pkce_challenge("v" * 43)] and query["code_challenge_method"] == [
        "S256"
    ]
    assert query["state"] == ["st"] and query["nonce"] == [NONCE] and query["client_id"] == [CLIENT_ID]
    if method == "passkey":
        assert query["prompt"] == ["login"] and "kc_idp_hint" not in query
    else:
        assert query["kc_idp_hint"] == [method] and "prompt" not in query


async def test_code_exchange_checks_the_verifier(idp):
    """The token endpoint gets the verifier; a wrong one is the IdP's `invalid_grant`."""
    client = idp.client()
    location = await client.authorization_url(
        method="google", state="st", nonce=NONCE, code_verifier="v" * 43, redirect_uri="https://t.example/cb"
    )
    callback, _ = idp.authorize(location, email="bo@example.org", sub="sub-bo")
    code = parse_qs(urlsplit(callback).query)["code"][0]
    with pytest.raises(IdpError) as err:
        await client.exchange_code(code=code, code_verifier="w" * 43, redirect_uri="https://t.example/cb")
    assert err.value.code == "invalid_grant"


async def test_logout_token_validation(idp):
    """Back-channel logout tokens need the event claim, no nonce, and a sid or sub."""
    client = idp.client()
    assert (await client.validate_logout_token(idp.logout_token(sid="s1")))["sid"] == "s1"
    assert (await client.validate_logout_token(idp.logout_token(sub="u1")))["sub"] == "u1"
    for bad in (
        idp.logout_token(sid="s1", nonce="n"),
        idp.logout_token(sid="s1", events=None),
        idp.logout_token(sid="s1", events={"other": {}}),
        idp.logout_token(),
        idp.logout_token(sid="s1", aud="other"),
        idp.logout_token(sid="s1", iat=None),
    ):
        with pytest.raises(InvalidTokenError):
            await client.validate_logout_token(bad)


async def test_end_session_url(idp):
    """The logout URL carries the hint and the post-logout redirect."""
    url = await idp.client().end_session_url(
        id_token_hint="tok", post_logout_redirect_uri="https://t.example/"
    )
    query = parse_qs(urlsplit(url).query)
    assert query == {
        "client_id": [CLIENT_ID],
        "post_logout_redirect_uri": ["https://t.example/"],
        "id_token_hint": ["tok"],
    }


@pytest.mark.parametrize(
    ("method", "claims", "proven"),
    [
        ("passkey", {"amr": ["passkey"]}, True),
        ("passkey", {"amr": ["passkey"], "identity_provider": "google"}, True),  # stale IdP claim
        ("passkey", {"amr": ["pwd"]}, False),
        ("passkey", {"identity_provider": "google"}, False),
        ("google", {"identity_provider": "google", "amr": ["passkey"]}, True),  # stale amr
        ("google", {"identity_provider": "github"}, False),
        ("github", {"identity_provider": "github"}, True),
        ("github", {}, False),
    ],
)
def test_sign_in_method_from_claims(method, claims, proven):
    """Only the claim of the method the flow asked for counts."""
    assert method_proven(method, claims) is proven


def _session(method: str, age: timedelta) -> CurrentSession:
    now = datetime.now(UTC)
    return CurrentSession(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        org_id=None,
        sign_in_method=method,
        created_at=now - age,
        last_seen_at=now,
        id_token=None,
        user_disabled=False,
    )


def test_require_recent_passkey():
    """A passkey session younger than PASSKEY_FRESH passes; older ones and Google or GitHub
    sessions do not; dev mode always passes."""
    oidc = Settings(env="test", auth_mode="oidc")
    assert passkey_is_fresh(oidc, _session("passkey", timedelta(hours=11)))
    assert not passkey_is_fresh(oidc, _session("passkey", timedelta(hours=13)))
    assert not passkey_is_fresh(oidc, _session("google", timedelta(minutes=1)))
    assert not passkey_is_fresh(oidc, None)
    assert passkey_is_fresh(
        Settings(env="test", auth_mode="oidc", passkey_fresh="1h"), _session("passkey", timedelta(minutes=5))
    )
    assert passkey_is_fresh(Settings(env="test", auth_mode="dev"), None)
