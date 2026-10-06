"""Connector credentials encryption (spec 021): round trip, binding to the source, keys, rotation
of the keyring, and the master-key setting."""

from __future__ import annotations

import base64
import os
import uuid
from dataclasses import replace

import pytest
from pydantic import ValidationError

from tabayyun.secrets import (
    CredentialsError,
    CredentialsUnavailableError,
    Keyring,
    key_id_of,
    require,
)
from tabayyun.settings import Settings

ORG, SOURCE, OTHER = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
PAYLOAD = {"username": "svc-pi", "password": "s3cret-value"}


def key() -> bytes:
    return os.urandom(32)


def test_round_trip_and_no_plaintext():
    ring = Keyring(key())
    sealed = ring.seal(ORG, SOURCE, PAYLOAD)
    assert b"s3cret-value" not in sealed.ciphertext and len(sealed.nonce) == 12
    assert ring.open(ORG, SOURCE, sealed) == PAYLOAD


def test_nonces_differ_per_seal():
    ring = Keyring(key())
    assert ring.seal(ORG, SOURCE, PAYLOAD).nonce != ring.seal(ORG, SOURCE, PAYLOAD).nonce


@pytest.mark.parametrize(("org", "source"), [(ORG, OTHER), (OTHER, SOURCE)])
def test_bound_to_org_and_source(org, source):
    ring = Keyring(key())
    sealed = ring.seal(ORG, SOURCE, PAYLOAD)
    with pytest.raises(CredentialsError, match="do not decrypt") as err:
        ring.open(org, source, sealed)
    assert "s3cret" not in str(err.value)


def test_tampered_and_wrong_key():
    ring = Keyring(key())
    sealed = ring.seal(ORG, SOURCE, PAYLOAD)
    flipped = bytes([sealed.ciphertext[0] ^ 1]) + sealed.ciphertext[1:]
    with pytest.raises(CredentialsError):
        ring.open(ORG, SOURCE, replace(sealed, ciphertext=flipped))
    with pytest.raises(CredentialsError, match="unknown key"):
        Keyring(key()).open(ORG, SOURCE, sealed)


def test_previous_key_still_opens_and_new_rows_use_the_current():
    old, new = key(), key()
    sealed = Keyring(old).seal(ORG, SOURCE, PAYLOAD)
    ring = Keyring(new, previous=old)
    assert ring.open(ORG, SOURCE, sealed) == PAYLOAD
    assert ring.seal(ORG, SOURCE, PAYLOAD).key_id == key_id_of(new) != sealed.key_id


def test_from_settings_and_require():
    raw = key()
    settings = Settings(master_key=base64.b64encode(raw).decode())
    ring = Keyring.from_settings(settings)
    assert ring is not None and ring.current_id == key_id_of(raw)
    assert Keyring.from_settings(Settings()) is None
    with pytest.raises(CredentialsUnavailableError):
        require(None)


@pytest.mark.parametrize("value", ["change-me", "dGVzdA==", base64.b64encode(os.urandom(16)).decode(), "!!"])
def test_a_bad_master_key_is_refused(value):
    with pytest.raises(ValidationError, match="master_key"):
        Settings(master_key=value)


def test_an_empty_master_key_means_none():
    assert Settings(master_key="  ").master_key is None
