"""Config, credentials and the client certificate of an `opc_ua` source (spec 023).

The source's client identity is an RSA key and a self-signed application certificate that
Tabayyun generates when credentials are first set (or that an admin provides). It is stored
encrypted with the credentials; only the certificate is ever shown, for the server's trust list.
The server is trusted by pinning its certificate's SHA-256 thumbprint.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from typing import Any, Literal
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.exceptions import InvalidKey
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tabayyun.connectors.base import ConnectorConfig

THUMBPRINT = re.compile(r"^[0-9a-f]{64}$")
NODE_ID = re.compile(r"^(?:ns=\d{1,5};)?[isgb]=.{1,4000}$", re.DOTALL)
POLICIES = ("Basic256Sha256", "Aes128_Sha256_RsaOaep", "Aes256_Sha256_RsaPss", "None")
MIN_RSA_BITS = 2048
CERT_DAYS = 5 * 365 + 1


def normalise_thumbprint(value: str) -> str:
    """A thumbprint as 64 lower-case hex digits (colons, spaces and case ignored)."""
    return re.sub(r"[\s:]", "", value).lower()


class OpcUaConfig(ConnectorConfig):
    """Where the server is, how the channel is secured, and how far a search goes."""

    endpoint_url: str = Field(max_length=500)
    security_policy: Literal["Basic256Sha256", "Aes128_Sha256_RsaOaep", "Aes256_Sha256_RsaPss", "None"] = (
        "Basic256Sha256"
    )
    security_mode: Literal["Sign", "SignAndEncrypt"] = "SignAndEncrypt"
    allow_insecure: bool = False
    server_certificate_sha256: str | None = Field(default=None, max_length=100)
    browse_root: str = Field(default="i=85", max_length=500)
    browse_limit: int = Field(default=5000, ge=100, le=50_000)
    browse_depth: int = Field(default=10, ge=1, le=30)
    max_values: int = Field(default=10_000, ge=100, le=100_000)
    timeout_s: float = Field(default=30.0, ge=1, le=300)
    requests_per_second: float = Field(default=20.0, gt=0, le=50)

    @field_validator("endpoint_url")
    @classmethod
    def _endpoint(cls, value: str) -> str:
        url = urlsplit(value.strip())
        if url.scheme != "opc.tcp" or not url.hostname:
            raise ValueError("must be an opc.tcp URL")
        if url.query or url.fragment or "@" in url.netloc:
            raise ValueError("must have no query, fragment or user info")
        try:
            url.port  # noqa: B018  (validates the port)
        except ValueError:
            raise ValueError("has an invalid port") from None
        return value.strip()

    @field_validator("server_certificate_sha256")
    @classmethod
    def _thumbprint(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalised = normalise_thumbprint(value)
        if not THUMBPRINT.match(normalised):
            raise ValueError("must be a SHA-256 thumbprint: 64 hex digits")
        return normalised

    @field_validator("browse_root")
    @classmethod
    def _root(cls, value: str) -> str:
        if not NODE_ID.match(value.strip()):
            raise ValueError("must be a node id like i=85 or ns=2;s=Plant")
        return value.strip()

    @model_validator(mode="after")
    def _insecure(self) -> OpcUaConfig:
        if self.security_policy == "None" and not self.allow_insecure:
            raise ValueError("security_policy None needs allow_insecure: true")
        return self

    @property
    def secure(self) -> bool:
        """Whether the channel is signed (and maybe encrypted)."""
        return self.security_policy != "None"


class OpcUaCredentials(BaseModel):
    """The user identity, and the client certificate and key (both or neither)."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["anonymous", "username"]
    username: str | None = Field(default=None, min_length=1, max_length=256)
    password: str | None = Field(default=None, min_length=1, max_length=1024)
    client_certificate_pem: str | None = Field(default=None, max_length=16_384)
    client_private_key_pem: str | None = Field(default=None, max_length=16_384)

    @model_validator(mode="after")
    def _shape(self) -> OpcUaCredentials:
        if self.kind == "username" and (self.username is None or self.password is None):
            raise ValueError("username credentials need username and password")
        if self.kind == "anonymous" and (self.username is not None or self.password is not None):
            raise ValueError("anonymous credentials take no username or password")
        if (self.client_certificate_pem is None) != (self.client_private_key_pem is None):
            raise ValueError("client_certificate_pem and client_private_key_pem come together")
        if self.client_certificate_pem is not None:
            assert self.client_private_key_pem is not None
            check_pair(self.client_certificate_pem, self.client_private_key_pem)
        return self

    @property
    def has_identity(self) -> bool:
        """Whether a client certificate and key are set."""
        return self.client_certificate_pem is not None


def check_pair(certificate_pem: str, key_pem: str) -> None:
    """A PEM certificate and an unencrypted PEM RSA key of at least 2048 bits that belong together.

    Raises:
        ValueError: they do not load, are not RSA, are too short, or do not match.
    """
    try:
        cert = x509.load_pem_x509_certificate(certificate_pem.encode())
        key = serialization.load_pem_private_key(key_pem.encode(), password=None)
    except (ValueError, TypeError, InvalidKey):
        raise ValueError("client certificate or key is not PEM (or the key is encrypted)") from None
    if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < MIN_RSA_BITS:
        raise ValueError(f"client key must be RSA of at least {MIN_RSA_BITS} bits")
    public = cert.public_key()
    if (
        not isinstance(public, rsa.RSAPublicKey)
        or public.public_numbers() != key.public_key().public_numbers()
    ):
        raise ValueError("client key does not match the certificate")


def generate_identity(source_id: uuid.UUID, now: dt.datetime | None = None) -> tuple[str, str]:
    """A new client key and self-signed OPC UA application certificate, as PEM (cert, key)."""
    now = now or dt.datetime.now(dt.UTC)
    key = rsa.generate_private_key(public_exponent=65537, key_size=MIN_RSA_BITS)
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, f"Tabayyun {source_id}"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Tabayyun"),
        ]
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=CERT_DAYS))
        .add_extension(
            x509.SubjectAlternativeName([x509.UniformResourceIdentifier(application_uri(source_id))]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=True,
                key_encipherment=True,
                data_encipherment=True,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode(), key_pem.decode()


def application_uri(source_id: uuid.UUID) -> str:
    """The OPC UA application URI of a source's generated client certificate."""
    return f"urn:tabayyun:source:{source_id}"


def certificate_uri(certificate_pem: str) -> str | None:
    """The first URI in a certificate's subject alternative names (its application URI)."""
    cert = x509.load_pem_x509_certificate(certificate_pem.encode())
    try:
        names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return None
    uris = names.get_values_for_type(x509.UniformResourceIdentifier)
    return uris[0] if uris else None


def certificate_info(certificate_pem: str) -> dict[str, Any]:
    """The public facts of a certificate an admin needs to trust it."""
    cert = x509.load_pem_x509_certificate(certificate_pem.encode())
    return {
        "certificate_pem": certificate_pem,
        "sha1": cert.fingerprint(hashes.SHA1()).hex(),  # noqa: S303  (OPC UA servers show SHA-1 thumbprints)
        "sha256": cert.fingerprint(hashes.SHA256()).hex(),
        "application_uri": certificate_uri(certificate_pem),
        "not_after": cert.not_valid_after_utc.isoformat(),
    }
