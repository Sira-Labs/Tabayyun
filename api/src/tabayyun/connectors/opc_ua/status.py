"""OPC UA values and status codes to Tabayyun rows (spec 023). Pure functions.

A status code's top two bits are its severity (Good, Uncertain, Bad); its low 16 bits are info
bits (overflow, limits) that do not change the quality.
"""

from __future__ import annotations

import datetime as dt
import math
from typing import Any

NAN = math.nan
CODE_MASK = 0xFFFF0000
GOOD_LOCAL_OVERRIDE = 0x00960000
GOOD_CLAMPED = 0x00300000
UNCERTAIN_SUBSTITUTE_VALUE = 0x40910000
OVERRIDDEN = frozenset({GOOD_LOCAL_OVERRIDE, GOOD_CLAMPED})
EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)


def severity(status: int) -> str:
    """`good`, `uncertain` or `bad` from a status code's severity bits."""
    top = (status >> 30) & 0b11
    return "good" if top == 0 else "uncertain" if top == 1 else "bad"


def quality(status: int) -> str:
    """Tabayyun's quality of a status code (spec 023's table)."""
    code = status & CODE_MASK
    level = severity(status)
    if level == "bad":
        return "bad"
    if level == "good":
        return "uncertain" if code in OVERRIDDEN else "good"
    return "estimated" if code == UNCERTAIN_SUBSTITUTE_VALUE else "uncertain"


def number(value: Any) -> float | None:
    """The float a variant value stands for: numbers and booleans; None otherwise."""
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, int | float):
        out = float(value)
        return out if math.isfinite(out) else None
    return None


def convert(value: Any, status: int) -> tuple[float, str]:
    """(value, quality) of one data value; Bad and non-numeric values are NaN and `bad`."""
    q = quality(status)
    if q == "bad":
        return NAN, "bad"
    out = number(value)
    if out is None:
        return NAN, "bad"
    return out, q


def to_ns(stamp: dt.datetime) -> int:
    """A timestamp as ns since the epoch; naive datetimes are UTC (asyncua has µs resolution)."""
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.UTC)
    delta = stamp - EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1_000


def from_ns(ns: int, *, ceil: bool = False) -> dt.datetime:
    """ns since the epoch as an aware UTC datetime, rounded down (or up) to a microsecond."""
    micros = -(-ns // 1_000) if ceil else ns // 1_000
    return EPOCH + dt.timedelta(microseconds=micros)
