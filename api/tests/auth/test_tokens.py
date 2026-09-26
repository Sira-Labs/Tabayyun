"""Tokens, their HMAC, PKCE and the `next` parameter (spec 013)."""

import pytest

from tabayyun.auth.tokens import new_token, pkce_challenge, safe_next, token_hash


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("/runs", "/runs"),
        ("/runs/abc?tab=findings#x", "/runs/abc?tab=findings#x"),
        (None, "/"),
        ("", "/"),
        ("runs", "/"),
        ("https://evil.example", "/"),
        ("//evil.example", "/"),
        ("/\\evil.example", "/"),
        ("/%0d%0aSet-Cookie:x", "/%0d%0aSet-Cookie:x"),
        ("/\r\nSet-Cookie: x", "/"),
        ("javascript:alert(1)", "/"),
    ],
)
def test_next_param_is_relative_only(value, expected):
    """Only a path on this site survives; anything that could leave it becomes `/`."""
    assert safe_next(value) == expected


def test_session_hmac_lookup():
    """The stored hash depends on the token and the secret, and never equals the token."""
    token = new_token()
    assert len(token) >= 43 and token != new_token()
    digest = token_hash("secret-a", token)
    assert digest == token_hash("secret-a", token)
    assert digest != token_hash("secret-b", token)
    assert digest != token_hash("secret-a", new_token())
    assert token.encode() not in digest and len(digest) == 32


def test_pkce_challenge_matches_rfc_7636():
    """The S256 example of RFC 7636, appendix B."""
    assert pkce_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == (
        "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    )
