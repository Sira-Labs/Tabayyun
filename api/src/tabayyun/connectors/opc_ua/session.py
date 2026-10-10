"""One OPC UA session of a source (spec 023): connect to the checked address, pin the server
certificate, secure the channel, log on, pace requests, and map errors to connector errors.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from asyncua import Client, ua
from asyncua.crypto import security_policies, uacrypto
from asyncua.crypto.uacrypto import CertProperties
from asyncua.ua.uaerrors import UaError, UaStatusCodeError
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.x509.oid import NameOID

from tabayyun.connectors.errors import AuthError, ConnectorError
from tabayyun.connectors.net import NetPolicy
from tabayyun.connectors.opc_ua.config import OpcUaConfig, OpcUaCredentials, certificate_uri
from tabayyun.connectors.plan import Pacer

DEFAULT_PORT = 4840
FALLBACK_URI = "urn:tabayyun:client"
POLICY_CLASSES: dict[str, type[security_policies.SecurityPolicy]] = {
    "Basic256Sha256": security_policies.SecurityPolicyBasic256Sha256,
    "Aes128_Sha256_RsaOaep": security_policies.SecurityPolicyAes128Sha256RsaOaep,
    "Aes256_Sha256_RsaPss": security_policies.SecurityPolicyAes256Sha256RsaPss,
}
AUTH_CODES = frozenset(
    {
        "BadUserAccessDenied",
        "BadIdentityTokenInvalid",
        "BadIdentityTokenRejected",
        "BadSecurityChecksFailed",
        "BadSecurityPolicyRejected",
        "BadSecurityModeRejected",
        "BadApplicationSignatureInvalid",
        "BadUserSignatureInvalid",
    }
)
RETRY_CODES = frozenset(
    {
        "BadTimeout",
        "BadServerHalted",
        "BadTooManySessions",
        "BadCommunicationError",
        "BadConnectionClosed",
        "BadSecureChannelClosed",
        "BadServerNotConnected",
        "BadNotConnected",
        "BadShutdown",
        "BadResourceUnavailable",
        "BadServerTooBusy",
        "BadTcpServerTooBusy",
    }
)


def code_name(status: int) -> str:
    """The name of a status code, e.g. `BadNodeIdUnknown` (the hex value when unknown)."""
    name, _ = ua.status_codes.get_name_and_doc(status & 0xFFFF0000)
    return name if name and not name.startswith("Unknown") else f"0x{status:08X}"


def error_for(exc: Exception) -> ConnectorError:
    """The connector error an exception from a session stands for (spec 023's table)."""
    if isinstance(exc, ConnectorError):
        return exc
    if isinstance(exc, UaStatusCodeError):
        name = code_name(int(exc.code))
        if name in AUTH_CODES or name.startswith("BadCertificate"):
            return AuthError(f"OPC UA server refused the connection: {name}")
        return ConnectorError(f"OPC UA server answered {name}", retryable=name in RETRY_CODES)
    if isinstance(exc, TimeoutError | asyncio.TimeoutError):
        return ConnectorError("OPC UA server timed out", retryable=True)
    if isinstance(exc, OSError):
        return ConnectorError(f"cannot reach the OPC UA server ({type(exc).__name__})", retryable=True)
    if isinstance(exc, UaError):
        return ConnectorError(f"OPC UA error ({type(exc).__name__})", retryable=False)
    raise exc


@dataclass
class Session:
    """A connected client and the pacer its requests wait on."""

    client: Client
    pacer: Pacer

    async def call(self, method: str, params: Any) -> Any:
        """One paced service call on the session (`read`, `browse`, `history_read`, …)."""
        await self.pacer.wait()
        return await getattr(self.client.uaclient, method)(params)


def _target(config: OpcUaConfig) -> tuple[str, int, str]:
    url = urlsplit(config.endpoint_url)
    assert url.hostname is not None
    return url.hostname, url.port or DEFAULT_PORT, url.path


def _server_certificate(endpoints: list[ua.EndpointDescription], config: OpcUaConfig) -> tuple[bytes, Any]:
    """The DER certificate and endpoint of the configured policy and mode."""
    uri = POLICY_CLASSES[config.security_policy].URI
    mode = ua.MessageSecurityMode[config.security_mode]
    for endpoint in endpoints:
        if endpoint.SecurityPolicyUri == uri and endpoint.SecurityMode == mode and endpoint.ServerCertificate:
            cert = uacrypto.x509_from_der(endpoint.ServerCertificate)  # type: ignore[no-untyped-call]
            return cert.public_bytes(serialization.Encoding.DER), cert
    raise ConnectorError(
        f"server offers no {config.security_policy} {config.security_mode} endpoint", retryable=False
    )


def _pin(cert: Any, config: OpcUaConfig) -> None:
    """Refuse a server certificate that is not the pinned one."""
    thumbprint = cert.fingerprint(hashes.SHA256()).hex()
    subject = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    cn = str(subject[0].value) if subject else "?"
    if config.server_certificate_sha256 is None:
        raise ConnectorError(
            f"server certificate not pinned: SHA-256 {thumbprint}, subject {cn}; "
            "set server_certificate_sha256 after comparing it with the server",
            retryable=False,
        )
    if thumbprint != config.server_certificate_sha256:
        raise ConnectorError(
            f"server certificate SHA-256 {thumbprint} does not match the pinned "
            f"{config.server_certificate_sha256}",
            retryable=False,
        )


@asynccontextmanager
async def open_session(
    config: OpcUaConfig, credentials: OpcUaCredentials | None, net: NetPolicy
) -> AsyncIterator[Session]:
    """A connected, secured session that disconnects on exit; errors inside become connector errors.

    Raises:
        ConnectorError: as `error_for` maps them; `AuthError` without the client identity a
            secure policy needs; `TargetRefusedError` for a refused host.
    """
    host, port, path = _target(config)
    address = await net.tcp_target(host, port)
    shown = f"[{address}]" if ":" in address else address
    client = Client(f"opc.tcp://{shown}:{port}{path}", timeout=config.timeout_s)
    if credentials is not None and credentials.has_identity:
        assert credentials.client_certificate_pem is not None
        client.application_uri = certificate_uri(credentials.client_certificate_pem) or FALLBACK_URI
    else:
        client.application_uri = FALLBACK_URI
    try:
        if config.secure:
            if credentials is None or not credentials.has_identity:
                raise AuthError("no client certificate: set the source's credentials first")
            assert credentials.client_certificate_pem and credentials.client_private_key_pem
            server_der, server_cert = _server_certificate(
                await client.connect_and_get_server_endpoints(), config
            )
            _pin(server_cert, config)
            await client.set_security(
                POLICY_CLASSES[config.security_policy],
                CertProperties(credentials.client_certificate_pem.encode(), extension="pem"),
                CertProperties(credentials.client_private_key_pem.encode(), extension="pem"),
                server_certificate=CertProperties(server_der, extension="der"),
                mode=ua.MessageSecurityMode[config.security_mode],
            )
        if credentials is not None and credentials.kind == "username":
            assert credentials.username is not None and credentials.password is not None
            client.set_user(credentials.username)
            client.set_password(credentials.password)
        await client.connect()
    except Exception as exc:  # noqa: BLE001  (mapped to a connector error, or re-raised)
        raise error_for(exc) from None
    try:
        yield Session(client, Pacer(config.requests_per_second))
    except ConnectorError:
        raise
    except Exception as exc:  # noqa: BLE001  (mapped to a connector error, or re-raised)
        raise error_for(exc) from None
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001, S110  (a failed goodbye changes nothing)
            pass
