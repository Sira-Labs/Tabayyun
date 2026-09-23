"""Coverage gaps (spec 006): which parts of a window the cache does not hold."""

from tabayyun.services.coverage import missing_ranges


def test_nothing_covered_is_one_gap():
    assert missing_ranges([], 0, 100) == [(0, 100)]


def test_fully_covered():
    assert missing_ranges([(0, 100)], 10, 90) == []
    assert missing_ranges([(0, 50), (50, 100)], 0, 100) == [], "touching ranges coalesce"


def test_gaps_at_both_ends_and_between():
    assert missing_ranges([(20, 40), (60, 80)], 0, 100) == [(0, 20), (40, 60), (80, 100)]


def test_overlapping_and_unsorted_ranges_coalesce():
    assert missing_ranges([(50, 70), (10, 30), (25, 55)], 0, 100) == [(0, 10), (70, 100)]


def test_ranges_outside_the_window_are_ignored():
    assert missing_ranges([(-50, -10), (200, 300)], 0, 100) == [(0, 100)]
    assert missing_ranges([(-50, 10), (90, 300)], 0, 100) == [(10, 90)]


def test_empty_or_inverted_window():
    assert missing_ranges([(0, 10)], 50, 50) == []
    assert missing_ranges([], 60, 50) == []
    assert missing_ranges([(5, 5)], 0, 10) == [(0, 10)], "empty ranges cover nothing"
