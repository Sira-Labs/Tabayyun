"""Nanosecond ↔ timestamptz conversions and query-time parsing (no database)."""

from datetime import UTC, datetime

import pytest

from tabayyun.services.coverage import missing_ranges
from tabayyun.services.timeconv import (
    CORE_NS_MAX,
    CORE_NS_MIN,
    datetime_to_ns,
    ns_to_datetime,
    ns_to_datetime_ceil,
    parse_time,
    to_core_ns,
)

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


def test_to_core_ns_clamps_to_the_core_range():
    """The core takes `i64` ns; later instants clamp to its end of time, which has no end."""
    assert to_core_ns(NS) == NS
    assert (CORE_NS_MIN, CORE_NS_MAX) == (-(2**63), 2**63 - 1)
    far = datetime_to_ns(datetime(2300, 1, 1, tzinfo=UTC))
    assert to_core_ns(far) == CORE_NS_MAX
    assert to_core_ns(-far) == CORE_NS_MIN


def test_coverage_of_a_write_ending_at_the_core_limit_holds_it():
    """A write whose last sample is `i64::MAX` reports the end `CORE_NS_MAX`; its stored
    coverage end rounds up past it, so a window reaching the end of time has no gap there."""
    start = CORE_NS_MAX - 5_000
    stored = (datetime_to_ns(ns_to_datetime(start)), datetime_to_ns(ns_to_datetime_ceil(CORE_NS_MAX)))
    assert stored[1] > CORE_NS_MAX
    assert missing_ranges([stored], start, CORE_NS_MAX) == []
