"""Config and credentials of the `pi_web_api` connector (spec 022)."""

from __future__ import annotations

import datetime as dt

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from pydantic import ValidationError

from tabayyun import connectors
from tabayyun.connectors.pi_web_api import PiWebApiConfig, PiWebApiCredentials

BASE = {"base_url": "https://pi.example.com/piwebapi/", "data_server": "\\\\PISRV01"}


def ca_pem() -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Plant CA")])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def test_defaults_and_normalised_url():
    config = PiWebApiConfig.model_validate(BASE)
    assert config.base_url == "https://pi.example.com/piwebapi"
    assert (config.max_count, config.requests_per_second, config.max_points) == (10_000, 20.0, 100)
    assert config.asset_database is None and config.ssl_context() is None
    assert connectors.get("pi_web_api").config_model is PiWebApiConfig


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"base_url": "http://pi.example.com/piwebapi"}, "base_url"),
        ({"base_url": "https://pi.example.com/piwebapi?x=1"}, "base_url"),
        ({"base_url": "https://user:pw@pi.example.com/piwebapi"}, "base_url"),
        ({"base_url": "https:///piwebapi"}, "base_url"),
        ({"data_server": "PISRV01"}, "data_server"),
        ({"data_server": "\\\\PISRV01\\tag"}, "data_server"),
        ({"asset_database": "\\\\AFSRV01"}, "asset_database"),
        ({"asset_database": "\\\\AFSRV01\\Plant|x"}, "asset_database"),
        ({"ca_pem": "not a certificate"}, "ca_pem"),
        ({"max_count": 999}, "max_count"),
        ({"max_count": 150_001}, "max_count"),
        ({"requests_per_second": 51}, "requests_per_second"),
        ({"verify_tls": False}, "verify_tls"),
    ],
)
def test_invalid_config(change, field):
    with pytest.raises(ValidationError) as exc:
        PiWebApiConfig.model_validate({**BASE, **change})
    assert {e["loc"][0] for e in exc.value.errors()} == {field}


def test_asset_database_and_ca_pem():
    pem = ca_pem()
    config = PiWebApiConfig.model_validate({**BASE, "asset_database": "\\\\AFSRV01\\Plant", "ca_pem": pem})
    context = config.ssl_context()
    assert context is not None and context.get_ca_certs()


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "basic", "username": "PLANT\\svc", "password": "s3cret"},
        {"kind": "bearer", "token": "eyJhbGciOi.payload.sig-_~+/="},
    ],
)
def test_valid_credentials(payload):
    assert PiWebApiCredentials.model_validate(payload).kind == payload["kind"]


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "basic", "username": "svc"},
        {"kind": "basic", "username": "svc", "password": "x", "token": "t"},
        {"kind": "basic", "username": "a:b", "password": "x"},
        {"kind": "bearer"},
        {"kind": "bearer", "token": "t", "username": "svc"},
        {"kind": "bearer", "token": "bad token\r\nX-Injected: 1"},
        {"kind": "kerberos"},
        {"kind": "basic", "username": "svc", "password": "x", "domain": "PLANT"},
    ],
)
def test_invalid_credentials(payload):
    with pytest.raises(ValidationError):
        PiWebApiCredentials.model_validate(payload)
