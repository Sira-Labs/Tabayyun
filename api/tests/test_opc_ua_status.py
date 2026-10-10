"""OPC UA status codes and values (spec 023): the quality table, info bits, conversion, time."""

from __future__ import annotations

import datetime as dt
import math

import pytest

from tabayyun.connectors.opc_ua.status import convert, from_ns, quality, to_ns

GOOD = 0x00000000
GOOD_LOCAL_OVERRIDE = 0x00960000
GOOD_CLAMPED = 0x00300000
UNCERTAIN = 0x40000000
UNCERTAIN_SUBSTITUTE = 0x40910000
UNCERTAIN_LAST_USABLE = 0x40900000
BAD_SENSOR = 0x808B0000
LIMIT_HIGH = 0x00000200  # an info bit: "high limited"


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (GOOD, "good"),
        (GOOD | LIMIT_HIGH, "good"),
        (GOOD_LOCAL_OVERRIDE, "uncertain"),
        (GOOD_CLAMPED | LIMIT_HIGH, "uncertain"),
        (UNCERTAIN, "uncertain"),
        (UNCERTAIN_LAST_USABLE, "uncertain"),
        (UNCERTAIN_SUBSTITUTE, "estimated"),
        (UNCERTAIN_SUBSTITUTE | LIMIT_HIGH, "estimated"),
        (BAD_SENSOR, "bad"),
        (0xC0000000, "bad"),
    ],
)
def test_quality(status, expected):
    assert quality(status) == expected


@pytest.mark.parametrize(
    ("value", "status", "expected"),
    [
        (12.5, GOOD, (12.5, "good")),
        (7, GOOD, (7.0, "good")),
        (True, GOOD, (1.0, "good")),
        (3.0, GOOD_LOCAL_OVERRIDE, (3.0, "uncertain")),
        (3.0, BAD_SENSOR, (math.nan, "bad")),
        ("running", GOOD, (math.nan, "bad")),
        (None, GOOD, (math.nan, "bad")),
        (float("inf"), GOOD, (math.nan, "bad")),
    ],
)
def test_convert(value, status, expected):
    got = convert(value, status)
    assert got[1] == expected[1]
    assert (math.isnan(got[0]) and math.isnan(expected[0])) or got[0] == expected[0]


def test_time_round_trip():
    stamp = dt.datetime(2026, 9, 1, 0, 0, 0, 123456, tzinfo=dt.UTC)
    ns = to_ns(stamp)
    assert ns == 1_788_220_800_123_456_000 and from_ns(ns) == stamp
    assert to_ns(stamp.replace(tzinfo=None)) == ns
    assert from_ns(ns + 1) == stamp and from_ns(ns + 1, ceil=True) == stamp + dt.timedelta(microseconds=1)
    assert to_ns(dt.datetime(1969, 12, 31, 23, 59, 59, tzinfo=dt.UTC)) == -1_000_000_000
