"""The connector network policy (spec 021): refused ranges, the allow-list, public off, every
resolved address checked, and the pinned HTTP client without redirects."""

from __future__ import annotations

import asyncio
import ipaddress
import ssl

import httpx
import pytest

from tabayyun.connectors import NetPolicy, TargetRefusedError
from tabayyun.connectors.net import PinnedTransport
from tabayyun.settings import Settings

PUBLIC = "93.184.215.14"


def resolver(answers: dict[str, list[str]]):
    async def resolve(host: str, port: int) -> list[str]:
        if host not in answers:
            raise OSError("no such host")
        return answers[host]

    return resolve


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "::1",
        "169.254.169.254",  # cloud metadata
        "fe80::1",
        "0.0.0.0",
        "::",
        "224.0.0.1",
        "ff02::1",
        "255.255.255.255",
        "240.0.0.1",
        "::ffff:127.0.0.1",  # IPv4-mapped loopback
        "::ffff:169.254.169.254",
        "64:ff9b::a9fe:a9fe",  # NAT64 of the metadata address
    ],
)
def test_always_refused_even_when_allow_listed(address):
    everything = [ipaddress.ip_network("0.0.0.0/0"), ipaddress.ip_network("::/0")]
    assert not NetPolicy(everything).allows(address)


@pytest.mark.parametrize(
    "address", ["10.1.2.3", "172.16.5.4", "192.168.1.10", "100.64.0.1", "fd00::1", "192.0.2.7"]
)
def test_private_only_inside_the_allow_list(address):
    assert not NetPolicy().allows(address)
    net = ipaddress.ip_network(address).supernet(new_prefix=8 if ":" not in address else 16)
    assert NetPolicy([net]).allows(address)


def test_public_on_and_off():
    assert NetPolicy().allows(PUBLIC) and NetPolicy().allows("2606:4700::1111")
    assert not NetPolicy(allow_public=False).allows(PUBLIC)


def test_from_settings():
    policy = NetPolicy.from_settings(
        Settings(connector_allowed_networks="10.0.0.0/8", connector_allow_public=False)
    )
    assert policy.allows("10.9.9.9") and not policy.allows(PUBLIC)


def test_resolve_checks_every_answer():
    policy = NetPolicy(
        resolver=resolver({"pi.example.com": [PUBLIC], "mixed.example.com": [PUBLIC, "169.254.169.254"]})
    )
    assert asyncio.run(policy.resolve("pi.example.com", 443)) == [ipaddress.ip_address(PUBLIC)]
    with pytest.raises(TargetRefusedError, match="not allowed: mixed.example.com") as err:
        asyncio.run(policy.resolve("mixed.example.com", 443))
    assert not err.value.retryable
    with pytest.raises(TargetRefusedError, match="does not resolve"):
        asyncio.run(policy.resolve("missing.example.com", 443))
    with pytest.raises(TargetRefusedError):
        asyncio.run(policy.resolve("[::1]", 443))


@pytest.mark.parametrize("url", ["ftp://pi.example.com/", "file:///etc/passwd", "https:///nohost"])
def test_http_client_needs_an_http_url(url):
    with pytest.raises(TargetRefusedError):
        asyncio.run(NetPolicy(resolver=resolver({})).http_client(url))


def test_http_client_is_pinned_and_does_not_follow_redirects():
    async def go():
        client = await NetPolicy(resolver=resolver({"pi.example.com": [PUBLIC]})).http_client(
            "https://pi.example.com/piwebapi"
        )
        async with client:
            assert client.follow_redirects is False
            assert isinstance(client._transport, PinnedTransport)  # noqa: SLF001

    asyncio.run(go())


def test_pinned_transport_rewrites_the_target_and_keeps_host_and_sni():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(302, headers={"location": "http://169.254.169.254/"})

    async def go():
        transport = PinnedTransport(httpx.MockTransport(handler), "pi.example.com", PUBLIC)
        async with httpx.AsyncClient(transport=transport, follow_redirects=False) as client:
            response = await client.get("https://pi.example.com/piwebapi/points")
            assert response.status_code == 302  # returned, not followed
            with pytest.raises(TargetRefusedError, match="pinned to pi.example.com"):
                await client.get("http://169.254.169.254/latest/meta-data")

    asyncio.run(go())
    [request] = seen
    assert request.url.host == PUBLIC and request.url.path == "/piwebapi/points"
    assert request.headers["host"] == "pi.example.com"
    assert request.extensions["sni_hostname"] == "pi.example.com"


def test_injected_transport_still_gets_checked_and_pinned_requests():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    policy = NetPolicy(
        resolver=resolver({"pi.example.com": [PUBLIC]}), transport=httpx.MockTransport(handler)
    )

    async def go():
        async with await policy.http_client("https://pi.example.com/piwebapi") as client:
            await client.get("points")
        with pytest.raises(TargetRefusedError):
            await NetPolicy(
                resolver=resolver({"pi.example.com": ["127.0.0.1"]}), transport=httpx.MockTransport(handler)
            ).http_client("https://pi.example.com/piwebapi")

    asyncio.run(go())
    [request] = seen
    assert request.url.host == PUBLIC and request.url.path == "/piwebapi/points"
    assert request.headers["host"] == "pi.example.com"


def test_verify_context_reaches_the_transport(monkeypatch):
    created: list[dict] = []
    real = httpx.AsyncHTTPTransport

    def spy(**kwargs):
        created.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", spy)
    context = ssl.create_default_context()

    async def go():
        policy = NetPolicy(resolver=resolver({"pi.example.com": [PUBLIC]}))
        async with await policy.http_client("https://pi.example.com/piwebapi", verify=context):
            pass
        async with await policy.http_client("https://pi.example.com/piwebapi"):
            pass

    asyncio.run(go())
    assert created == [{"verify": context}, {"verify": True}]
