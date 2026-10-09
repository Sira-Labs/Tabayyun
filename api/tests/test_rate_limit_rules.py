"""Fixed windows and the bucket table of spec 015 (no database)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from tabayyun.services.rate_limits import HOUR, MINUTE, RULES, seconds_left, window_start

NOW = datetime(2026, 10, 5, 9, 41, 17, 250_000, tzinfo=UTC)


def test_windows_align_to_the_minute_and_hour():
    assert window_start(NOW, MINUTE) == datetime(2026, 10, 5, 9, 41, tzinfo=UTC)
    assert window_start(NOW, HOUR) == datetime(2026, 10, 5, 9, 0, tzinfo=UTC)


def test_windows_do_not_depend_on_the_caller_time_zone():
    riyadh = NOW.astimezone(timezone(timedelta(hours=3)))
    assert window_start(riyadh, MINUTE) == window_start(NOW, MINUTE)


@pytest.mark.parametrize(
    ("now", "window", "expected"),
    [
        (NOW, MINUTE, 43),  # 42.75 s left, rounded up
        (datetime(2026, 10, 5, 9, 41, 59, 999_000, tzinfo=UTC), MINUTE, 1),
        (datetime(2026, 10, 5, 9, 42, tzinfo=UTC), MINUTE, 60),
        (NOW, HOUR, 1123),
    ],
)
def test_retry_after_counts_the_rest_of_the_window(now, window, expected):
    assert seconds_left(now, window) == expected


def test_the_bucket_table_matches_the_spec():
    assert {b: (r.limit, r.window) for b, r in RULES.items()} == {
        "auth.login": (20, MINUTE),
        "auth.callback": (20, MINUTE),
        "auth.backchannel": (60, MINUTE),
        "auth.sessions": (30, MINUTE),
        "admin.write": (60, MINUTE),
        "admin.invite": (50, HOUR),
        "source.write": (60, MINUTE),
        "source.fetch": (30, MINUTE),
    }


def test_source_jobs_count_against_source_fetch():
    """Checks, fetches, searches and metadata imports share the `source.fetch` bucket (specs 021, 022)."""
    from fastapi.routing import APIRoute

    import tabayyun.main  # noqa: F401  (imports the jobs package before the router, as the app does)
    from tabayyun.routers import sources

    bucket = sources.SOURCE_FETCH_LIMIT.dependency
    limited = {
        (method, route.path)
        for route in sources.router.routes
        if isinstance(route, APIRoute) and any(d.dependency is bucket for d in route.dependencies)
        for method in route.methods
    }
    assert limited == {
        ("POST", "/api/sources/{source_id}/check"),
        ("POST", "/api/sources/{source_id}/fetches"),
        ("POST", "/api/sources/{source_id}/search"),
        ("POST", "/api/sources/{source_id}/metadata"),
    }
