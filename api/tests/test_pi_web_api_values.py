"""PI Web API values (spec 022): timestamps, the quality table, and paging past `maxCount`."""

from __future__ import annotations

import math

import pytest

from tabayyun.connectors.pi_web_api.values import (
    Cursor,
    TimestampError,
    convert,
    format_time,
    parse_time,
    read_page,
)

S = 1_000_000_000
T0 = 1_788_220_800 * S  # 2026-09-01T00:00:00Z


@pytest.mark.parametrize(
    ("text", "ns"),
    [
        ("2026-09-01T00:00:00Z", T0),
        ("2026-09-01T00:00:00.5Z", T0 + S // 2),
        ("2026-09-01T00:00:00.2988321Z", T0 + 298_832_100),
        ("2026-09-01T00:00:00.123456789Z", T0 + 123_456_789),
        ("2026-09-01T03:00:00+03:00", T0),
        ("2026-08-31T19:30:00-04:30", T0),
        ("1969-12-31T23:59:59.9Z", -S // 10),
    ],
)
def test_parse_time(text, ns):
    assert parse_time(text) == ns


@pytest.mark.parametrize(
    "text", ["2026-09-01 00:00:00Z", "2026-09-01T00:00:00", "*-1d", "", "2026-09-01T00:00:00.Z"]
)
def test_parse_time_refuses(text):
    with pytest.raises(TimestampError):
        parse_time(text)


def test_format_time_rounds_to_pi_ticks():
    assert format_time(T0) == "2026-09-01T00:00:00.0000000Z"
    assert format_time(T0 + 150) == "2026-09-01T00:00:00.0000001Z"
    assert format_time(T0 + 150, ceil=True) == "2026-09-01T00:00:00.0000002Z"
    assert format_time(T0 + 100, ceil=True) == "2026-09-01T00:00:00.0000001Z"
    assert format_time(-S // 10) == "1969-12-31T23:59:59.9000000Z"
    assert parse_time(format_time(T0 + 298_832_100)) == T0 + 298_832_100


def item(value, **flags):
    return {"Value": value, "Good": True, "Questionable": False, "Substituted": False, **flags}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (item(12.5), (12.5, "good")),
        (item(7), (7.0, "good")),
        (item(True), (1.0, "good")),
        (item({"Name": "Running", "Value": 1, "IsSystem": False}), (1.0, "good")),
        (item({"Name": "I/O Timeout", "Value": 246, "IsSystem": True}, Good=False), (math.nan, "bad")),
        (item({"Name": "Shutdown", "Value": 254, "IsSystem": True}), (math.nan, "bad")),
        (item(None, Errors=[{"FieldName": "Value", "Message": ["x"]}]), (math.nan, "bad")),
        (item(None), (math.nan, "bad")),
        (item("running"), (math.nan, "bad")),
        (item(3.0, Good=False), (3.0, "bad")),
        (item(3.0, Questionable=True), (3.0, "uncertain")),
        (item(3.0, Questionable=True, Substituted=True), (3.0, "uncertain")),
        (item(3.0, Substituted=True), (3.0, "estimated")),
        ({"Value": 4.0}, (4.0, "good")),
    ],
)
def test_quality_table(raw, expected):
    value, quality = convert(raw)
    assert quality == expected[1]
    assert (math.isnan(value) and math.isnan(expected[0])) or value == expected[0]


def page(*stamps_values):
    return [{"Timestamp": format_time(ts), "Value": v, "Good": True} for ts, v in stamps_values]


def test_short_page_is_the_last_and_the_window_is_half_open():
    items = page((T0 - S, 0.0), (T0, 1.0), (T0 + S, 2.0), (T0 + 2 * S, 3.0))
    rows, cursor = read_page(items, Cursor(T0 - S), T0, T0 + 2 * S, max_count=10)
    assert cursor is None and rows.ts == [T0, T0 + S] and rows.value == [1.0, 2.0]


def test_paging_skips_values_already_read_at_the_boundary():
    # Three values at T0+S: the first page ends after two of them.
    first = page((T0, 1.0), (T0 + S, 2.0), (T0 + S, 2.1))
    rows, cursor = read_page(first, Cursor(T0), T0, T0 + 10 * S, max_count=3)
    assert rows.value == [1.0, 2.0, 2.1] and cursor == Cursor(T0 + S, 2)
    second = page((T0 + S, 2.0), (T0 + S, 2.1), (T0 + S, 2.2), (T0 + 2 * S, 3.0))
    rows, cursor = read_page(second, cursor, T0, T0 + 10 * S, max_count=4)
    assert rows.value == [2.2, 3.0] and cursor == Cursor(T0 + 2 * S, 1)
    rows, cursor = read_page(page((T0 + 2 * S, 3.0)), cursor, T0, T0 + 10 * S, max_count=4)
    assert rows.value == [] and cursor is None


def test_a_full_page_at_one_timestamp_cannot_advance():
    stuck = page((T0, 1.0), (T0, 1.1))
    with pytest.raises(TimestampError, match="more than 2 values at one timestamp"):
        read_page(stuck, Cursor(T0), T0, T0 + S, max_count=2)


def test_a_full_page_reaching_the_end_stops():
    items = page((T0, 1.0), (T0 + S, 2.0))
    rows, cursor = read_page(items, Cursor(T0), T0, T0 + S, max_count=2)
    assert rows.value == [1.0] and cursor is None
