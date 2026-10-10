"""PI Web API values to Tabayyun rows (spec 022): timestamps, quality, recorded-values paging.

Pure functions, tested without a server.
"""

from __future__ import annotations

import calendar
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

NAN = math.nan
TICK_NS = 100  # PI's time resolution
TICKS_PER_SECOND = 10_000_000
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
TIMESTAMP = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})$"
)


class TimestampError(ValueError):
    """A timestamp PI Web API should not send."""


def parse_time(value: str) -> int:
    """An ISO 8601 timestamp with 0 to 9 fractional digits and an offset, as ns since the epoch.

    Raises:
        TimestampError: it is not one.
    """
    match = TIMESTAMP.match(value)
    if match is None:
        raise TimestampError(f"not an ISO 8601 timestamp: {value[:40]}")
    year, month, day, hour, minute, second, fraction, offset = match.groups()
    seconds = calendar.timegm((int(year), int(month), int(day), int(hour), int(minute), int(second)))
    if offset != "Z":
        sign = 1 if offset[0] == "+" else -1
        seconds -= sign * (int(offset[1:3]) * 3600 + int(offset[4:6]) * 60)
    nanos = int((fraction or "").ljust(9, "0"))
    return seconds * 1_000_000_000 + nanos


def format_time(ns: int, *, ceil: bool = False) -> str:
    """`ns` as ISO 8601 UTC with PI's 7 fractional digits, rounded down (or up) to 100 ns."""
    ticks = -(-ns // TICK_NS) if ceil else ns // TICK_NS
    seconds, fraction = divmod(ticks, TICKS_PER_SECOND)
    stamp = EPOCH + timedelta(seconds=seconds)
    return f"{stamp:%Y-%m-%dT%H:%M:%S}.{fraction:07d}Z"


def _number(value: Any) -> float | None:
    """The number a PI value stands for: numbers, booleans, digital states by code."""
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, Mapping):
        if value.get("IsSystem"):
            return None  # I/O Timeout, Shutdown, Bad Input, …
        code = value.get("Value")
        if isinstance(code, int | float) and not isinstance(code, bool):
            return float(code)
    return None


def convert(item: Mapping[str, Any]) -> tuple[float, str]:
    """One PI value item as (value, quality) by the table of spec 022; NaN is null."""
    if item.get("Errors"):
        return NAN, "bad"
    number = _number(item.get("Value"))
    if number is None or not math.isfinite(number):
        return NAN, "bad"
    if item.get("Good") is False:
        return number, "bad"
    if item.get("Questionable"):
        return number, "uncertain"
    if item.get("Substituted"):
        return number, "estimated"
    return number, "good"


@dataclass
class Rows:
    """Rows of one page, already within the window."""

    ts: list[int]
    value: list[float]
    quality: list[str]


@dataclass(frozen=True)
class Cursor:
    """Where the next page starts, and how many values at that timestamp were already read."""

    at_ns: int
    seen_at: int = 0


def read_page(
    items: Sequence[Mapping[str, Any]], cursor: Cursor, start_ns: int, end_ns: int, max_count: int
) -> tuple[Rows, Cursor | None]:
    """The rows of one recorded-values page and the next cursor (None when it was the last page).

    PI answers the earliest `max_count` values and does not say it cut the answer short, so a
    full page is followed by another from its last timestamp. PI may hold several values at one
    timestamp; the next page repeats those already read at its first timestamp, which are
    skipped by count.

    Raises:
        TimestampError: a timestamp is malformed, or a full page holds one timestamp only (the
            next page could not advance).
    """
    rows = Rows([], [], [])
    skip = cursor.seen_at
    stamps = [parse_time(str(item.get("Timestamp", ""))) for item in items]
    for ts, item in zip(stamps, items, strict=True):
        if skip and ts == cursor.at_ns:
            skip -= 1
            continue
        if ts < start_ns or ts >= end_ns:
            continue
        value, quality = convert(item)
        rows.ts.append(ts)
        rows.value.append(value)
        rows.quality.append(quality)
    if len(items) < max_count:
        return rows, None
    last = stamps[-1]
    if stamps[0] == last:
        # The next request would start at this timestamp and get the same page again.
        raise TimestampError(f"more than {max_count} values at one timestamp")
    if last >= end_ns:
        return rows, None
    return rows, Cursor(last, sum(1 for ts in stamps if ts == last))
