"""Corrections without a database (spec 035): the overlay of corrected versions on raw rows,
what an operation reads around its window, and `*_ns` parameters."""

from __future__ import annotations

import pyarrow as pa
import pytest

from tabayyun.services import corrections

HOUR = 3600 * 10**9
DAY = 24 * HOUR


def batch(rows: list[tuple[int, float, int]], *, ingest: bool = False) -> pa.RecordBatch:
    """Rows `(hour, value, quality code)` in the cache's schema."""
    columns = {
        "ts": pa.array([h * HOUR for h, _, _ in rows], pa.timestamp("ns", tz="UTC")),
        "value": pa.array([v for _, v, _ in rows], pa.float64()),
        "quality": pa.array([q for _, _, q in rows], pa.uint8()),
    }
    if ingest:
        columns["ingest_ts"] = pa.array([0] * len(rows), pa.timestamp("ns", tz="UTC"))
    return pa.RecordBatch.from_pydict(columns)


def rows_of(found: pa.RecordBatch | None) -> list[tuple[int, float, int]]:
    assert found is not None
    assert found.schema.names == ["ts", "value", "quality"]
    ts = found.column("ts").cast(pa.int64()).to_pylist()
    return list(
        zip(
            [t // HOUR for t in ts],
            found.column("value").to_pylist(),
            found.column("quality").to_pylist(),
            strict=True,
        )
    )


def test_overlay_replaces_window_rows():
    """Each layer replaces the rows of its window, in order; rows outside stay as they were."""
    raw = batch([(h, float(h), 0) for h in range(6)], ingest=True)
    assert rows_of(corrections.overlay(raw, [])) == [(h, float(h), 0) for h in range(6)]

    # v1 fills [2, 4) with estimates and adds a sample; the window's raw rows are gone.
    v1 = batch([(2, 20.0, 3), (3, 30.0, 3)])
    assert rows_of(corrections.overlay(raw, [(2 * HOUR, 4 * HOUR, v1)])) == [
        (0, 0.0, 0),
        (1, 1.0, 0),
        (2, 20.0, 3),
        (3, 30.0, 3),
        (4, 4.0, 0),
        (5, 5.0, 0),
    ]
    # v2 over [3, 5) lies on top of v1: the later layer wins where they overlap.
    v2 = batch([(3, 300.0, 3), (4, 400.0, 3)])
    both = corrections.overlay(raw, [(2 * HOUR, 4 * HOUR, v1), (3 * HOUR, 5 * HOUR, v2)])
    assert [v for _, v, _ in rows_of(both)] == [0.0, 1.0, 20.0, 300.0, 400.0, 5.0]

    # A layer without rows removes its window's rows; one over no raw rows adds its own.
    assert [h for h, _, _ in rows_of(corrections.overlay(raw, [(1 * HOUR, 5 * HOUR, None)]))] == [0, 5]
    assert rows_of(corrections.overlay(None, [(2 * HOUR, 4 * HOUR, v1)])) == [(2, 20.0, 3), (3, 30.0, 3)]
    assert corrections.overlay(None, []) is None
    assert corrections.overlay(raw, [(0, 6 * HOUR, None)]) is None

    # Samples that share a timestamp keep their order, and the window's end is open.
    doubled = batch([(0, 1.0, 0), (1, 2.0, 0), (1, 3.0, 0), (2, 4.0, 0)])
    kept = corrections.overlay(doubled, [(0, 1 * HOUR, batch([(0, 9.0, 3)]))])
    assert rows_of(kept) == [(0, 9.0, 3), (1, 2.0, 0), (1, 3.0, 0), (2, 4.0, 0)]


def test_context_margin_per_operation():
    """`impute.linear` and `impute.like_day` read around the window; the others do not."""
    margin = corrections.context_margin_ns
    assert margin("impute.linear", {"max_gap_ns": 6 * HOUR}, HOUR) == 6 * HOUR
    assert margin("impute.linear", {}, HOUR) == DAY
    assert margin("impute.linear", {"max_gap_ns": None}, None) == DAY
    assert margin("impute.like_day", {}, HOUR) == 4 * DAY + HOUR
    assert margin("impute.like_day", {"days": 2}, 900 * 10**9) == 2 * DAY + 900 * 10**9
    assert margin("impute.like_day", {"days": 3, "same_weekday": True}, HOUR) == 21 * DAY + HOUR
    assert margin("impute.like_day", {"days": 1}, None) == DAY
    for method in ("mask", "clamp", "dedupe", "align.resample", "transform.affine"):
        assert margin(method, {"grid_ns": HOUR, "min": 0}, HOUR) == 0


def test_params_ns_as_strings():
    """`*_ns` parameters come in as integers or decimal strings, are stored as integers and go
    out as strings; other parameters pass as they are."""
    stored = corrections.params_in(
        {"max_gap_ns": "3600000000000", "grid_ns": 60, "scale": 2.5, "keep": "last"}
    )
    assert stored == {"max_gap_ns": 3_600_000_000_000, "grid_ns": 60, "scale": 2.5, "keep": "last"}
    assert corrections.params_out(stored) == {
        "max_gap_ns": "3600000000000",
        "grid_ns": "60",
        "scale": 2.5,
        "keep": "last",
    }
    assert corrections.operation("impute.linear", {"max_gap_ns": 5}) == {
        "method": "impute.linear",
        "max_gap_ns": 5,
    }
    assert corrections.params_in({"max_gap_ns": None}) == {"max_gap_ns": None}
    for bad in (
        {"max_gap_ns": "1h"},
        {"max_gap_ns": 1.5},
        {"grid_ns": True},
        {"grid_ns": str(2**63)},
        {"method": "mask"},
    ):
        with pytest.raises(corrections.InvalidCorrectionError):
            corrections.params_in(bad)
    assert corrections.decode_cursor(corrections.encode_cursor(7)) == 7
    with pytest.raises(ValueError, match="invalid cursor"):
        corrections.decode_cursor("bm9wZQ==")
