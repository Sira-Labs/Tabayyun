"""Nanosecond ↔ timestamptz conversions and query-time parsing (no database)."""

from datetime import UTC, datetime

import pytest

from tabayyun.services.timeconv import datetime_to_ns, ns_to_datetime, ns_to_datetime_ceil, parse_time

NS = 1_700_000_000_123_456_789


def test_start_rounds_down_and_end_rounds_up():
    """A stored window always contains the core's half-open window."""
    assert datetime_to_ns(ns_to_datetime(NS)) == NS - 789
    assert datetime_to_ns(ns_to_datetime_ceil(NS)) == NS - 789 + 1000
    assert datetime_to_ns(ns_to_datetime_ceil(NS - 789)) == NS - 789


def test_parse_time_accepts_epoch_ns_and_rfc3339():
    """Epoch ns, offset-aware text and naive text (taken as UTC)."""
    assert parse_time(str(NS)) == ns_to_datetime(NS)
    assert parse_time("2026-01-01T00:00:00Z") == datetime(2026, 1, 1, tzinfo=UTC)
    assert parse_time("2026-01-01T00:00:00") == datetime(2026, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize("value", ["yesterday", "9" * 30, "-" + "9" * 30, ""])
def test_parse_time_rejects_garbage_and_out_of_range(value):
    """Unparsable and out-of-range values raise ValueError, never OverflowError."""
    with pytest.raises(ValueError, match="not RFC 3339 or epoch ns"):
        parse_time(value)
