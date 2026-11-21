"""The overview's pure parts (spec 032): tiles, the worst series, days with zeros, windows."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from tabayyun.services import overview

AT = datetime(2026, 11, 21, 6, 0, tzinfo=UTC)


def _latest(n: int, overall: float, **dimensions: float | None) -> overview.Latest:
    values = {m: None for m in overview.METRICS} | {"overall": overall} | dimensions
    return overview.Latest(uuid.UUID(int=n), values, AT)


def test_window_of_days():
    """A window of n days ends today and starts n - 1 days before."""
    assert overview.window_of(30, date(2026, 11, 21)) == (date(2026, 10, 23), date(2026, 11, 21))
    assert overview.window_of(7, date(2026, 3, 2)) == (date(2026, 2, 24), date(2026, 3, 2))


def test_summarise_tiles():
    """Eight tiles in order; mean, minimum and count are over the series that have the metric."""
    latest = [
        _latest(1, 90.0, completeness=100.0, validity=80.0),
        _latest(2, 70.0, completeness=50.0),
    ]
    trend = {"overall": [{"day": "2026-11-20", "mean": 80.0, "n": 2}]}
    tiles = overview.summarise_tiles(latest, trend)
    assert [t["metric"] for t in tiles] == [
        "overall",
        "completeness",
        "timeliness",
        "validity",
        "accuracy",
        "consistency",
        "plausibility",
        "integrity",
    ]
    by = {t["metric"]: t for t in tiles}
    assert (by["overall"]["mean"], by["overall"]["min"], by["overall"]["n_series"]) == (80.0, 70.0, 2)
    assert (by["completeness"]["mean"], by["completeness"]["min"]) == (75.0, 50.0)
    assert (by["validity"]["mean"], by["validity"]["n_series"]) == (80.0, 1)
    assert (by["timeliness"]["mean"], by["timeliness"]["min"], by["timeliness"]["n_series"]) == (
        None,
        None,
        0,
    )
    assert by["overall"]["trend"] == trend["overall"] and by["validity"]["trend"] == []
    assert all(t["mean"] is None and t["n_series"] == 0 for t in overview.summarise_tiles([], {}))


def test_rank_worst_by_lowest_value():
    """The rank is by a series' lowest value, not by its overall score; 5 % rounded up, and at
    most fifty are listed."""
    good = _latest(1, 99.0, completeness=99.0)
    hidden = _latest(2, 95.0, completeness=40.0)  # a fine overall score over one bad dimension
    poor = _latest(3, 60.0, completeness=70.0)
    n, worst = overview.rank_worst([good, hidden, poor])
    assert n == 1 and [w.series_id for w in worst] == [hidden.series_id]
    assert hidden.lowest() == ("completeness", 40.0) and poor.lowest() == ("overall", 60.0)
    # A tie on the lowest value goes to the lower overall score.
    tied = [_latest(4, 80.0, validity=50.0), _latest(5, 70.0, validity=50.0)] + [
        _latest(10 + i, 99.0) for i in range(38)
    ]
    n, worst = overview.rank_worst(tied)
    assert n == 2 and [w.series_id.int for w in worst] == [5, 4]
    many = [_latest(i, float(i % 90)) for i in range(2001)]
    n, worst = overview.rank_worst(many)
    assert n == 101 and len(worst) == overview.WORST_LISTED
    assert overview.rank_worst([]) == (0, [])


def test_fill_days_with_zeros():
    """Every day of the window has an entry with all four severities."""
    days = overview.fill_days(
        date(2026, 11, 19),
        date(2026, 11, 21),
        [
            (date(2026, 11, 19), "high", 2),
            (date(2026, 11, 21), "critical", 1),
            (date(2026, 11, 21), "low", 4),
        ],
    )
    assert days == [
        {"day": "2026-11-19", "critical": 0, "high": 2, "medium": 0, "low": 0},
        {"day": "2026-11-20", "critical": 0, "high": 0, "medium": 0, "low": 0},
        {"day": "2026-11-21", "critical": 1, "high": 0, "medium": 0, "low": 4},
    ]
