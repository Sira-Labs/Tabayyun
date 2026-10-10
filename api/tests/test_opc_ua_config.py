"""Config, credentials and the client identity of the `opc_ua` connector (spec 023)."""

from __future__ import annotations

import uuid

import pytest
from cryptography import x509
from cryptography.x509.oid import ExtendedKeyUsageOID
from pydantic import ValidationError

from opcua_server import client_identity
from tabayyun import connectors
from tabayyun.connectors.opc_ua import OpcUaConfig, OpcUaConnector, OpcUaCredentials
from tabayyun.connectors.opc_ua.config import application_uri, certificate_uri, generate_identity

BASE = {"endpoint_url": "opc.tcp://scada.plant.example:4840/UA/Server"}
PIN = "AB:" * 31 + "CD"


def test_defaults():
    config = OpcUaConfig.model_validate(BASE)
    assert (config.security_policy, config.security_mode, config.allow_insecure) == (
        "Basic256Sha256",
        "SignAndEncrypt",
        False,
    )
    assert (config.browse_root, config.browse_limit, config.max_values, config.requests_per_second) == (
        "i=85",
        5000,
        10_000,
        20.0,
    )
    assert config.secure and connectors.get("opc_ua") is OpcUaConnector


def test_thumbprint_is_normalised():
    config = OpcUaConfig.model_validate({**BASE, "server_certificate_sha256": PIN})
    assert config.server_certificate_sha256 == "ab" * 31 + "cd"


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"endpoint_url": "https://scada.plant.example"}, "endpoint_url"),
        ({"endpoint_url": "opc.tcp://user:pw@scada:4840"}, "endpoint_url"),
        ({"endpoint_url": "opc.tcp://scada:99999"}, "endpoint_url"),
        ({"security_policy": "Basic128Rsa15"}, "security_policy"),
        ({"security_mode": "None"}, "security_mode"),
        ({"server_certificate_sha256": "abc"}, "server_certificate_sha256"),
        ({"browse_root": "Objects"}, "browse_root"),
        ({"browse_limit": 50}, "browse_limit"),
        ({"browse_depth": 31}, "browse_depth"),
        ({"max_values": 10}, "max_values"),
        ({"trust_all": True}, "trust_all"),
    ],
)
def test_invalid_config(change, field):
    with pytest.raises(ValidationError) as exc:
        OpcUaConfig.model_validate({**BASE, **change})
    assert {e["loc"][0] for e in exc.value.errors()} == {field}


def test_no_security_needs_an_explicit_opt_in():
    with pytest.raises(ValidationError, match="allow_insecure"):
        OpcUaConfig.model_validate({**BASE, "security_policy": "None"})
    config = OpcUaConfig.model_validate({**BASE, "security_policy": "None", "allow_insecure": True})
    assert not config.secure


def test_generated_identity():
    source = uuid.uuid4()
    cert_pem, key_pem = generate_identity(source)
    OpcUaCredentials.model_validate(
        {"kind": "anonymous", "client_certificate_pem": cert_pem, "client_private_key_pem": key_pem}
    )
    cert = x509.load_pem_x509_certificate(cert_pem.encode())
    assert certificate_uri(cert_pem) == application_uri(source) == f"urn:tabayyun:source:{source}"
    usage = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert list(usage) == [ExtendedKeyUsageOID.CLIENT_AUTH]
    assert cert.public_key().key_size == 2048
    assert "BEGIN PRIVATE KEY" in key_pem


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "anonymous", "username": "svc"},
        {"kind": "username", "username": "svc"},
        {"kind": "anonymous", "client_certificate_pem": "x"},
        {"kind": "kerberos"},
    ],
)
def test_invalid_credentials(payload):
    with pytest.raises(ValidationError):
        OpcUaCredentials.model_validate(payload)


def test_a_mismatched_pair_is_refused():
    cert_pem, _ = client_identity()
    _, other_key = generate_identity(uuid.uuid4())
    with pytest.raises(ValidationError, match="does not match"):
        OpcUaCredentials.model_validate(
            {"kind": "anonymous", "client_certificate_pem": cert_pem, "client_private_key_pem": other_key}
        )


def test_prepare_credentials_generates_keeps_or_takes_the_identity():
    source = uuid.uuid4()
    made, details = OpcUaConnector.prepare_credentials(
        OpcUaCredentials(kind="anonymous"), None, source_id=source
    )
    assert details == {"client_certificate": "generated"} and made.has_identity
    assert certificate_uri(made.client_certificate_pem) == application_uri(source)
    user = OpcUaCredentials(kind="username", username="svc", password="pw")
    kept, details = OpcUaConnector.prepare_credentials(user, made, source_id=source)
    assert details == {"client_certificate": "kept"} and kept.username == "svc"
    assert kept.client_certificate_pem == made.client_certificate_pem
    cert_pem, key_pem = client_identity()
    own = OpcUaCredentials(kind="anonymous", client_certificate_pem=cert_pem, client_private_key_pem=key_pem)
    taken, details = OpcUaConnector.prepare_credentials(own, made, source_id=source)
    assert details == {"client_certificate": "provided"} and taken.client_certificate_pem == cert_pem


def test_client_certificate_shows_no_key():
    cert_pem, key_pem = client_identity()
    info = OpcUaConnector.client_certificate(
        OpcUaCredentials(kind="anonymous", client_certificate_pem=cert_pem, client_private_key_pem=key_pem)
    )
    assert set(info) == {"certificate_pem", "sha1", "sha256", "application_uri", "not_after"}
    assert "PRIVATE KEY" not in str(info) and len(info["sha256"]) == 64 and len(info["sha1"]) == 40
    assert OpcUaConnector.client_certificate(OpcUaCredentials(kind="anonymous")) is None
