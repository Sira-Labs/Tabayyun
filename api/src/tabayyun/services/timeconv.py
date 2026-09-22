"""Conversions between the core's nanosecond timestamps and `timestamptz` values.

The core speaks `i64` nanoseconds since the epoch (UTC); PostgreSQL stores microseconds.
Window starts round down and window ends round up, so a stored window always contains the
core's half-open window `[start, end)`.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_EPOCH_NS = re.compile(r"^-?\d+$")


def ns_to_datetime(ns: int) -> datetime:
    """Nanoseconds since the epoch to an aware datetime, rounded down to the microsecond."""
    return EPOCH + timedelta(microseconds=ns // 1000)


def ns_to_datetime_ceil(ns: int) -> datetime:
    """Nanoseconds since the epoch to an aware datetime, rounded up to the microsecond."""
    return EPOCH + timedelta(microseconds=-(-ns // 1000))


def datetime_to_ns(value: datetime) -> int:
    """Aware datetime to nanoseconds since the epoch."""
    return int((value - EPOCH) // timedelta(microseconds=1)) * 1000


def parse_time(value: str) -> datetime:
    """RFC 3339 text or epoch nanoseconds to an aware datetime; naive text is taken as UTC.

    Raises ValueError with a message fit for a 422 response.
    """
    text = value.strip()
    if _EPOCH_NS.match(text):
        return ns_to_datetime(int(text))
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"not RFC 3339 or epoch ns: {value!r}") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
