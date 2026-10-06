"""Connector credentials at rest (spec 021): AES-256-GCM under `TABAYYUN_MASTER_KEY`.

Each source's credentials are one JSON object, encrypted with a fresh 96-bit nonce and bound to
the source by the associated data `"{org_id}:{source_id}"`, so a ciphertext copied to another
source (or org) does not decrypt. The key id (8 hex of the key's SHA-256) is stored with the
row: after a rotation `TABAYYUN_MASTER_KEY_PREVIOUS` still decrypts old rows until
`python -m tabayyun.secrets rotate` re-encrypts them.

Nothing here logs or returns a credential value except `load`, whose caller is the fetch
engine; errors name the source, never the content.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import SecretStr
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from tabayyun.db.models import SourceCredentials
from tabayyun.settings import Settings

NONCE_BYTES = 12


class CredentialsUnavailableError(Exception):
    """No master key is configured, so credentials can be neither stored nor read."""


class CredentialsError(Exception):
    """Stored credentials do not decrypt (wrong key, tampered row, or copied to another source)."""


@dataclass(frozen=True)
class Sealed:
    """One encrypted credentials object, as stored."""

    key_id: str
    nonce: bytes
    ciphertext: bytes


def key_id_of(key: bytes) -> str:
    """The id stored with each row: 8 hex of the key's SHA-256 (not secret, not reversible)."""
    return hashlib.sha256(key).hexdigest()[:8]


class Keyring:
    """The current master key and, during a rotation, the previous one."""

    def __init__(self, current: bytes, previous: bytes | None = None) -> None:
        self.current_id = key_id_of(current)
        self._keys = {self.current_id: AESGCM(current)}
        if previous is not None:
            self._keys.setdefault(key_id_of(previous), AESGCM(previous))

    @classmethod
    def from_settings(cls, settings: Settings) -> Keyring | None:
        """The keyring the settings configure; None without a master key."""
        current = _decode(settings.master_key)
        if current is None:
            return None
        return cls(current, _decode(settings.master_key_previous))

    def seal(self, org_id: uuid.UUID, source_id: uuid.UUID, payload: dict[str, Any]) -> Sealed:
        """Encrypt `payload` under the current key for this source."""
        nonce = os.urandom(NONCE_BYTES)
        plaintext = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        ciphertext = self._keys[self.current_id].encrypt(nonce, plaintext, _aad(org_id, source_id))
        return Sealed(key_id=self.current_id, nonce=nonce, ciphertext=ciphertext)

    def open(self, org_id: uuid.UUID, source_id: uuid.UUID, sealed: Sealed) -> dict[str, Any]:
        """Decrypt a row of this source.

        Raises:
            CredentialsError: the key is unknown, or the row does not authenticate.
        """
        aead = self._keys.get(sealed.key_id)
        if aead is None:
            raise CredentialsError(f"credentials of source {source_id} use an unknown key {sealed.key_id}")
        try:
            plaintext = aead.decrypt(sealed.nonce, sealed.ciphertext, _aad(org_id, source_id))
        except InvalidTag:
            raise CredentialsError(f"credentials of source {source_id} do not decrypt") from None
        value = json.loads(plaintext)
        if not isinstance(value, dict):
            raise CredentialsError(f"credentials of source {source_id} are not an object")
        return value


def _decode(secret: SecretStr | None) -> bytes | None:
    """The raw key (settings validated it as 32 bytes of base64)."""
    return None if secret is None else base64.b64decode(secret.get_secret_value().strip())


def _aad(org_id: uuid.UUID, source_id: uuid.UUID) -> bytes:
    return f"{org_id}:{source_id}".encode()


def require(keyring: Keyring | None) -> Keyring:
    """The keyring, or CredentialsUnavailableError when no master key is set."""
    if keyring is None:
        raise CredentialsUnavailableError("TABAYYUN_MASTER_KEY is not set")
    return keyring


async def store(
    session: AsyncSession,
    keyring: Keyring | None,
    *,
    org_id: uuid.UUID,
    source_id: uuid.UUID,
    payload: dict[str, Any],
    user_id: uuid.UUID | None,
) -> None:
    """Encrypt and upsert a source's credentials (in the caller's tenant transaction)."""
    sealed = require(keyring).seal(org_id, source_id, payload)
    values = {
        "source_id": source_id,
        "org_id": org_id,
        "key_id": sealed.key_id,
        "nonce": sealed.nonce,
        "ciphertext": sealed.ciphertext,
        "updated_by": user_id,
    }
    stmt = insert(SourceCredentials).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[SourceCredentials.source_id],
        set_={k: stmt.excluded[k] for k in ("key_id", "nonce", "ciphertext", "updated_by")}
        | {"updated_at": func.now()},
    )
    await session.execute(stmt)


async def clear(session: AsyncSession, source_id: uuid.UUID) -> bool:
    """Delete a source's credentials; whether there were any."""
    result = await session.execute(delete(SourceCredentials).where(SourceCredentials.source_id == source_id))
    return bool(result.rowcount)  # type: ignore[attr-defined]


async def status(session: AsyncSession, source_id: uuid.UUID) -> datetime | None:
    """When the source's credentials were last set; None when it has none."""
    updated_at: datetime | None = await session.scalar(
        select(SourceCredentials.updated_at).where(SourceCredentials.source_id == source_id)
    )
    return updated_at


async def load(
    session: AsyncSession, keyring: Keyring | None, *, org_id: uuid.UUID, source_id: uuid.UUID
) -> dict[str, Any] | None:
    """A source's decrypted credentials; None when it has none.

    Raises:
        CredentialsUnavailableError: it has some but no master key is set.
        CredentialsError: they do not decrypt.
    """
    row = await session.get(SourceCredentials, source_id)
    if row is None:
        return None
    return require(keyring).open(org_id, source_id, Sealed(row.key_id, row.nonce, row.ciphertext))


async def rotate(session: AsyncSession, keyring: Keyring) -> int:
    """Re-encrypt every row not under the current key; the number of rows re-encrypted.

    Runs as the table owner (no RLS), so it sees every org's rows.
    """
    rows = (
        await session.scalars(
            select(SourceCredentials).where(SourceCredentials.key_id != keyring.current_id).with_for_update()
        )
    ).all()
    for row in rows:
        payload = keyring.open(row.org_id, row.source_id, Sealed(row.key_id, row.nonce, row.ciphertext))
        sealed = keyring.seal(row.org_id, row.source_id, payload)
        row.key_id, row.nonce, row.ciphertext = sealed.key_id, sealed.nonce, sealed.ciphertext
    return len(rows)
