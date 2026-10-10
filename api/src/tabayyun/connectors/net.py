"""Where connectors may connect (spec 021): an address policy against SSRF.

Every target is resolved once and every address it resolves to is checked:

- loopback, link-local (cloud metadata), unspecified, multicast, reserved and broadcast
  addresses are always refused, including their IPv4-mapped and NAT64 forms;
- other non-global addresses (RFC 1918, ULA, CGNAT, documentation ranges) only inside
  `TABAYYUN_CONNECTOR_ALLOWED_NETWORKS`;
- public addresses unless `TABAYYUN_CONNECTOR_ALLOW_PUBLIC=false`.

HTTP connectors get a client pinned to the checked address (the original name stays in the
Host header and TLS SNI, so certificates are still verified against it), without redirects:
a second DNS answer or a 30x cannot send it elsewhere. A connector may pass an `ssl.SSLContext`
that trusts a private certificate authority (spec 022); verification is never turned off.
Connectors over plain TCP (OPC UA, spec 023) get the checked address from `tcp_target` and
connect to it themselves.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import ssl
from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import httpx

from tabayyun.connectors.errors import TargetRefusedError

if TYPE_CHECKING:
    from tabayyun.settings import Settings

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
Resolver = Callable[[str, int], Awaitable[list[str]]]

HTTP_TIMEOUT_S = 30.0
NAT64 = ipaddress.ip_network("64:ff9b::/96")
BROADCAST = ipaddress.ip_address("255.255.255.255")


async def system_resolver(host: str, port: int) -> list[str]:
    """Addresses `host` resolves to, through the system resolver."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def _embedded(addr: IPAddress) -> IPAddress:
    """The IPv4 address an IPv4-mapped or NAT64 IPv6 address stands for, else itself."""
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            return addr.ipv4_mapped
        if addr in NAT64:
            return ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF)
    return addr


def always_refused(addr: IPAddress) -> bool:
    """Loopback, link-local, unspecified, multicast, reserved or broadcast."""
    return (
        addr.is_loopback
        or addr.is_link_local
        or addr.is_unspecified
        or addr.is_multicast
        or addr.is_reserved
        or addr == BROADCAST
    )


class NetPolicy:
    """The address policy of one install."""

    def __init__(
        self,
        allowed: Iterable[IPNetwork] = (),
        *,
        allow_public: bool = True,
        resolver: Resolver = system_resolver,
        transport: httpx.AsyncBaseTransport | None = None,
        redirect: Mapping[str, str] | None = None,
    ) -> None:
        """`transport` replaces the network under the pinned HTTP transport, and `redirect` maps
        a checked address to another for TCP connectors; both for tests only: the address checks
        still run in front of them."""
        self.allowed = tuple(allowed)
        self.allow_public = allow_public
        self._resolve = resolver
        self._transport = transport
        self._redirect = dict(redirect or {})

    @classmethod
    def from_settings(cls, settings: Settings) -> NetPolicy:
        """The policy the settings configure."""
        return cls(settings.allowed_networks, allow_public=settings.connector_allow_public)

    def allows(self, address: str) -> bool:
        """Whether a connector may connect to `address` (an IP literal)."""
        addr = _embedded(ipaddress.ip_address(address))
        if always_refused(addr):
            return False
        if any(addr in net for net in self.allowed if net.version == addr.version):
            return True
        return addr.is_global and self.allow_public

    async def resolve(self, host: str, port: int) -> list[IPAddress]:
        """Every address `host` resolves to, all allowed; the first is the one to connect to.

        Raises:
            TargetRefusedError: it does not resolve, or any address is refused (one bad answer
                among good ones could be picked by a later connection).
        """
        try:
            literal = ipaddress.ip_address(host.strip("[]"))
            answers = [str(literal)]
        except ValueError:
            try:
                answers = await self._resolve(host, port)
            except OSError:
                raise TargetRefusedError(f"target does not resolve: {host}") from None
        if not answers:
            raise TargetRefusedError(f"target does not resolve: {host}")
        if not all(self.allows(a) for a in answers):
            raise TargetRefusedError(f"target not allowed: {host}")
        return [ipaddress.ip_address(a) for a in dict.fromkeys(answers)]

    async def tcp_target(self, host: str, port: int) -> str:
        """The checked address a TCP connector connects to for `host` (spec 023).

        Raises:
            TargetRefusedError: it does not resolve, or an address it resolves to is refused.
        """
        address = str((await self.resolve(host, port))[0])
        return self._redirect.get(address, address)

    async def http_client(
        self, base_url: str, *, verify: ssl.SSLContext | None = None, **kwargs: object
    ) -> httpx.AsyncClient:
        """A client for `base_url` pinned to its checked address, without redirects.

        `verify` is the TLS context to verify the server with (default: the system's trust).

        Raises:
            TargetRefusedError: the URL is not http(s), or its host is refused.
        """
        url = urlsplit(base_url)
        if url.scheme not in ("http", "https") or not url.hostname:
            raise TargetRefusedError(f"not an http(s) URL: {base_url}")
        port = url.port or (443 if url.scheme == "https" else 80)
        address = (await self.resolve(url.hostname, port))[0]
        inner = self._transport or httpx.AsyncHTTPTransport(verify=verify if verify is not None else True)
        transport = PinnedTransport(inner, url.hostname, str(address))
        return httpx.AsyncClient(
            base_url=base_url,
            transport=transport,
            follow_redirects=False,
            timeout=HTTP_TIMEOUT_S,
            **kwargs,  # type: ignore[arg-type]
        )


class PinnedTransport(httpx.AsyncBaseTransport):
    """Sends requests for `hostname` to `address`, keeping Host and SNI; refuses other hosts."""

    def __init__(self, inner: httpx.AsyncBaseTransport, hostname: str, address: str) -> None:
        self._inner = inner
        self._hostname = hostname
        self._address = address

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Rewrite the connection target; the Host header and SNI keep the name."""
        if request.url.host != self._hostname:
            raise TargetRefusedError(f"client pinned to {self._hostname}, not {request.url.host}")
        request.url = request.url.copy_with(host=self._address)
        request.extensions = {**request.extensions, "sni_hostname": self._hostname}
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        """Close the wrapped transport."""
        await self._inner.aclose()
