"""Conversions between the core's nanosecond timestamps and `timestamptz` values.

The core speaks `i64` nanoseconds since the epoch (UTC); PostgreSQL stores microseconds.
Window starts round down and window ends round up, so a stored window always contains the
core's half-open window `[start, end)`.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
# The core's timestamps are `i64` ns (the database's `BIGINT` too). CORE_NS_MAX is the core's
# END_OF_TIME: as a range end it has no end, so it includes `i64::MAX` itself (issue #42).
CORE_NS_MIN = -(2**63)
CORE_NS_MAX = 2**63 - 1
_EPOCH_NS = re.compile(r"^-?\d+$")
_FRACTION = re.compile(r"[.,](\d+)")  # ISO 8601 allows either decimal mark; `fromisoformat` too


def ns_to_datetime(ns: int) -> datetime:
    """Nanoseconds since the epoch to an aware datetime, rounded down to the microsecond."""
    return EPOCH + timedelta(microseconds=ns // 1000)


def ns_to_datetime_ceil(ns: int) -> datetime:
    """Nanoseconds since the epoch to an aware datetime, rounded up to the microsecond."""
    return EPOCH + timedelta(microseconds=-(-ns // 1000))


def to_core_ns(ns: int) -> int:
    """Nanoseconds clamped to the core's `i64` range; later instants become its end of time."""
    return min(max(ns, CORE_NS_MIN), CORE_NS_MAX)


def datetime_to_ns(value: datetime) -> int:
    """Aware datetime to nanoseconds since the epoch."""
    return int((value - EPOCH) // timedelta(microseconds=1)) * 1000


def parse_time(value: str) -> datetime:
    """RFC 3339 text or epoch nanoseconds to an aware datetime; naive text is taken as UTC.

    Raises ValueError with a message fit for a 422 response.
    """
    text = value.strip()
    try:
        if _EPOCH_NS.match(text):
            return ns_to_datetime(int(text))
        parsed = datetime.fromisoformat(text)
    except (ValueError, OverflowError) as exc:
        # OverflowError: an epoch value outside the datetime range (years 1–9999).
        raise ValueError(f"not RFC 3339 or epoch ns: {value!r}") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def parse_time_ns(value: str) -> int:
    """Like `parse_time`, but exact to the nanosecond: a datetime keeps only microseconds.

    Validity checks that must see the requested instant (issue #42) use this.
    """
    text = value.strip()
    if _EPOCH_NS.match(text):
        return int(text)
    ns = datetime_to_ns(parse_time(text))
    fraction = _FRACTION.search(text)
    if fraction and len(fraction.group(1)) > 6:
        # Digits 7-9 of the fraction; the datetime already carries the first six.
        ns += int(fraction.group(1)[6:9].ljust(3, "0"))
    return ns
