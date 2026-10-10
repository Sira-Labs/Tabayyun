"""An in-process asyncua server for the `opc_ua` connector tests (spec 023).

`running_server()` starts it on a free loopback port with Basic256Sha256 (Sign and
SignAndEncrypt) and an unsecured endpoint, anonymous and username logins, and this address
space under Objects:

- `Plant/Area1/FIC101.PV` (Double, history): a day of minute values from `DAY0`, a daily sine
  with noise, 02:00–03:00 flat; minute 100 `GoodLocalOverride`, 200 `GoodClamped`, 300
  `UncertainSubstituteValue`, 400 `UncertainLastUsableValue`, 500 `BadSensorFailure`.
  Properties: `EngineeringUnits` m³/h, `EURange` 10–90, `InstrumentRange` 0–120.
- `Plant/Area1/LIC201.PV` (Double, history): a sine; `EngineeringUnits` m.
- `Plant/Area1/TI102.PV` (Double): current value only, no history access.
- `Plant/Area1/PumpRunning` (Boolean, history): true at 00:00 and 12:00, false at 06:00 and 18:00.
- `Plant/Area2/Line/FIC201.PV` (Double, history): one value, deeper in the tree.

The connector reaches it as `opcua.example.com`, resolved to a public test address that the
network policy's test redirect maps to 127.0.0.1, so the address checks still run.
"""

from __future__ import annotations

import datetime as dt
import functools
import math
import socket
import tempfile
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from asyncua import Server, ua
from asyncua.crypto import cert_gen
from asyncua.crypto.permission_rules import User, UserRole
from asyncua.server.user_managers import UserManager
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.x509.oid import ExtendedKeyUsageOID

from tabayyun.connectors import NetPolicy
from tabayyun.connectors.opc_ua.config import generate_identity

HOST = "opcua.example.com"
PUBLIC_IP = "93.184.215.14"
DAY0 = dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
MINUTES = 1440
USERS = {"svc-tabayyun": "fixture-only"}
SOURCE_ID = uuid.UUID("00000000-0000-0000-0000-00000000c0a1")


@functools.cache
def server_identity() -> tuple[bytes, bytes, str]:
    """The server's certificate and key (PEM) and the certificate's SHA-256 thumbprint."""
    key = cert_gen.generate_private_key()
    cert = cert_gen.generate_self_signed_app_certificate(
        key,
        "Fixture OPC UA server",
        {},
        [x509.UniformResourceIdentifier("urn:tabayyun:test-server"), x509.DNSName("localhost")],
        [ExtendedKeyUsageOID.SERVER_AUTH],
    )
    return (
        cert.public_bytes(serialization.Encoding.PEM),
        cert_gen.dump_private_key_as_pem(key),
        cert.fingerprint(hashes.SHA256()).hex(),
    )


@functools.cache
def client_identity() -> tuple[str, str]:
    """A client certificate and key as the connector generates them (PEM)."""
    return generate_identity(SOURCE_ID)


class Users(UserManager):
    """Anonymous (when allowed) and the fixture's username logins."""

    def __init__(self, allow_anonymous: bool) -> None:
        self.allow_anonymous = allow_anonymous

    def get_user(
        self, iserver: Any, username: str | None = None, password: str | None = None, certificate: Any = None
    ) -> User | None:  # noqa: E501
        if username is None:
            return User(role=UserRole.User) if self.allow_anonymous else None
        return User(role=UserRole.User) if USERS.get(username) == password else None


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class Fixture:
    """A running server: its URL as the connector sees it, its thumbprint and its nodes."""

    server: Server
    port: int
    thumbprint: str
    ns: int
    nodes: dict[str, Any]

    @property
    def url(self) -> str:
        return f"opc.tcp://{HOST}:{self.port}/tabayyun"

    def node_id(self, name: str) -> str:
        return f"ns={self.ns};s={name}"

    def config(self, **overrides: Any) -> dict[str, Any]:
        return {"endpoint_url": self.url, "server_certificate_sha256": self.thumbprint, **overrides}


def net_policy() -> NetPolicy:
    """Resolves the fixture's host to a public test address and redirects it to loopback."""

    async def resolve(host: str, port: int) -> list[str]:
        if host != HOST:
            raise OSError("no such host")
        return [PUBLIC_IP]

    return NetPolicy(resolver=resolve, redirect={PUBLIC_IP: "127.0.0.1"})


def _value(minute: int) -> float:
    in_day = minute % MINUTES
    if 120 <= in_day < 180:
        return 42.0
    noise = (minute * 2654435761 % 1000) / 1000 - 0.5
    return round(50 + 10 * math.sin(2 * math.pi * in_day / MINUTES) + noise, 4)


STATUSES = {
    100: ua.StatusCodes.GoodLocalOverride,
    200: ua.StatusCodes.GoodClamped,
    300: ua.StatusCodes.UncertainSubstituteValue,
    400: ua.StatusCodes.UncertainLastUsableValue,
    500: ua.StatusCodes.BadSensorFailure,
}


def _dv(
    value: Any, variant: ua.VariantType, stamp: dt.datetime, status: int = ua.StatusCodes.Good
) -> ua.DataValue:
    return ua.DataValue(
        ua.Variant(value, variant), ua.StatusCode(status), SourceTimestamp=stamp, ServerTimestamp=stamp
    )


async def _variable(
    parent: Any, ns: int, name: str, value: Any, *, history: bool, unit: str | None = None
) -> Any:
    node = await parent.add_variable(ua.NodeId(name, ns), name, value)
    if history:
        await node.set_attr_bit(ua.AttributeIds.AccessLevel, ua.AccessLevel.HistoryRead)
        await node.set_attr_bit(ua.AttributeIds.UserAccessLevel, ua.AccessLevel.HistoryRead)
    if unit is not None:
        await node.add_property(
            ns,
            "EngineeringUnits",
            ua.EUInformation(
                NamespaceUri="http://www.opcfoundation.org/UA/units/un/cefact",
                UnitId=0,
                DisplayName=ua.LocalizedText(unit),
                Description=ua.LocalizedText(unit),
            ),
        )
    return node


@asynccontextmanager
async def running_server(*, allow_anonymous: bool = True, page: int = 10_000) -> AsyncIterator[Fixture]:
    """The fixture server, stopped on exit; `page` is the server's history page size."""
    cert_pem, key_pem, thumbprint = server_identity()
    port = free_port()
    server = Server(user_manager=Users(allow_anonymous))
    await server.init()
    server.set_endpoint(f"opc.tcp://127.0.0.1:{port}/tabayyun")
    server.set_server_name("Tabayyun fixture")
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "server.pem").write_bytes(cert_pem)
        (Path(tmp) / "server.key.pem").write_bytes(key_pem)
        await server.load_certificate(str(Path(tmp) / "server.pem"))
        await server.load_private_key(str(Path(tmp) / "server.key.pem"), format="pem")
    await server.set_application_uri("urn:tabayyun:test-server")
    server.set_security_policy(
        [
            ua.SecurityPolicyType.Basic256Sha256_SignAndEncrypt,
            ua.SecurityPolicyType.Basic256Sha256_Sign,
            ua.SecurityPolicyType.NoSecurity,
        ]
    )
    server.set_security_IDs(["Anonymous", "Username"])
    ns = await server.register_namespace("urn:tabayyun:plant")
    plant = await server.nodes.objects.add_object(ua.NodeId("Plant", ns), "Plant")
    area1 = await plant.add_object(ua.NodeId("Area1", ns), "Area1")
    line = await (await plant.add_object(ua.NodeId("Area2", ns), "Area2")).add_object(
        ua.NodeId("Line", ns), "Line"
    )
    flow = await _variable(area1, ns, "FIC101.PV", 0.0, history=True, unit="m³/h")
    await flow.add_property(ns, "EURange", ua.Range(Low=10.0, High=90.0))
    await flow.add_property(ns, "InstrumentRange", ua.Range(Low=0.0, High=120.0))
    await flow.write_attribute(
        ua.AttributeIds.Description, ua.DataValue(ua.Variant(ua.LocalizedText("Inlet flow")))
    )
    nodes = {
        "FIC101.PV": flow,
        "LIC201.PV": await _variable(area1, ns, "LIC201.PV", 0.0, history=True, unit="m"),
        "TI102.PV": await _variable(area1, ns, "TI102.PV", 20.0, history=False),
        "PumpRunning": await _variable(area1, ns, "PumpRunning", False, history=True),
        "FIC201.PV": await _variable(line, ns, "FIC201.PV", 0.0, history=True),
    }
    async with server:
        storage = server.iserver.history_manager.storage
        storage.max_history_data_response_size = page
        for name in ("FIC101.PV", "LIC201.PV", "PumpRunning", "FIC201.PV"):
            await storage.new_historized_node(nodes[name].nodeid, None, 0)
        start = int(DAY0.timestamp()) // 60
        for i in range(MINUTES):
            stamp = DAY0 + dt.timedelta(minutes=i)
            flow_status = STATUSES.get(i, ua.StatusCodes.Good)
            await storage.save_node_value(
                flow.nodeid, _dv(_value(start + i), ua.VariantType.Double, stamp, flow_status)
            )
            level = round(5 + math.sin(2 * math.pi * i / MINUTES), 4)
            await storage.save_node_value(nodes["LIC201.PV"].nodeid, _dv(level, ua.VariantType.Double, stamp))
            if i % 360 == 0:
                await storage.save_node_value(
                    nodes["PumpRunning"].nodeid, _dv(i % 720 == 0, ua.VariantType.Boolean, stamp)
                )
        await storage.save_node_value(nodes["FIC201.PV"].nodeid, _dv(7.0, ua.VariantType.Double, DAY0))
        yield Fixture(server, port, thumbprint, ns, nodes)
