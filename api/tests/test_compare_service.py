"""Pure helpers of the compare service (spec 018)."""

import math

from tabayyun.services import compare


def test_non_finite_numbers_never_reach_the_response():
    """NaN and both infinities become null; statistics with one of them are dropped whole."""
    assert compare._nullable([1.0, math.nan, math.inf, -math.inf, 0.0]) == [1.0, None, None, None, 0.0]
    stats = {"min": 0.0, "max": 2.0, "mean": 1.0, "sd": 1.0, "first_ts": 5, "first_value": 0.0}
    assert compare.finite_stats(stats) == stats
    assert compare.finite_stats({**stats, "sd": math.inf}) is None
    assert compare.finite_stats({**stats, "mean": math.nan}) is None
    assert compare.finite_stats(None) is None
