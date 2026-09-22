"""Opaque keyset cursors over `(timestamp, id)` pairs, shared by the list endpoints."""

from __future__ import annotations

import base64
import binascii
import uuid
from datetime import datetime


def encode_keyset(ts: datetime, row_id: uuid.UUID) -> str:
    """Cursor naming the last row of a page."""
    return base64.urlsafe_b64encode(f"{ts.isoformat()}|{row_id}".encode()).decode()


def decode_keyset(cursor: str) -> tuple[datetime, uuid.UUID]:
    """Inverse of `encode_keyset`; raises ValueError("invalid cursor") on anything else."""
    try:
        ts, row_id = base64.urlsafe_b64decode(cursor.encode()).decode().split("|", 1)
        return datetime.fromisoformat(ts), uuid.UUID(row_id)
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise ValueError("invalid cursor") from exc
