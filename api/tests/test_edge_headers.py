"""The web edge's security headers (spec 015): `deploy/caddy/Caddyfile` must keep them."""

from __future__ import annotations

import re
from pathlib import Path

CADDYFILE = Path(__file__).resolve().parents[2] / "deploy" / "caddy" / "Caddyfile"

REQUIRED = {
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}
CSP_DIRECTIVES = {
    "default-src": "'self'",
    "script-src": "'self'",
    "style-src": "'self'",
    "object-src": "'none'",
    "base-uri": "'none'",
    "frame-ancestors": "'none'",
}


def headers() -> dict[str, str]:
    """`Name "value"` lines of the Caddyfile's header block."""
    text = CADDYFILE.read_text()
    return dict(re.findall(r'^\s+([A-Z][A-Za-z-]+) "([^"]*)"\s*$', text, flags=re.MULTILINE))


def test_required_headers_are_set():
    found = headers()
    for name, value in REQUIRED.items():
        assert found.get(name) == value, name


def test_csp_is_strict():
    csp = dict(
        (part.split(" ", 1) + [""])[:2]
        for part in (p.strip() for p in headers()["Content-Security-Policy"].split(";"))
    )
    for directive, value in CSP_DIRECTIVES.items():
        assert csp.get(directive) == value, directive
    assert all("'unsafe-inline'" not in v and "'unsafe-eval'" not in v for v in csp.values())


def test_permissions_policy_denies_device_access():
    policy = headers()["Permissions-Policy"]
    for feature in ("camera", "microphone", "geolocation", "payment", "usb"):
        assert f"{feature}=()" in policy


def test_server_header_is_removed():
    assert re.search(r"^\s+-Server\s*$", CADDYFILE.read_text(), flags=re.MULTILINE)


def test_the_edge_sets_the_client_ip():
    """Per-IP rate limits key on `X-Real-IP`: the edge must overwrite a client's own value, and
    trust `X-Forwarded-For` only from the proxies named, reading it from the right."""
    text = CADDYFILE.read_text()
    trust = r"^\s+trusted_proxies static \{\$TABAYYUN_TRUSTED_PROXIES:private_ranges\}\s*$"
    assert re.search(r"^\s+header_up X-Real-IP \{client_ip\}\s*$", text, flags=re.MULTILINE)
    assert re.search(trust, text, flags=re.MULTILINE)
    assert re.search(r"^\s+trusted_proxies_strict\s*$", text, flags=re.MULTILINE)


def test_compose_trusts_no_proxy_by_default():
    """In the compose bundle Caddy faces the clients itself: a private-range client must not be able
    to name another address."""
    compose = (CADDYFILE.parents[1] / "compose.yaml").read_text()
    assert "TABAYYUN_TRUSTED_PROXIES: ${TABAYYUN_TRUSTED_PROXIES:-127.0.0.1/32}" in compose
