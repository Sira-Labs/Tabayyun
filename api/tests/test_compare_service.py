"""Pure helpers of the compare service (specs 018 and 033)."""

import math
import uuid

import pyarrow as pa
import pytest

from tabayyun.db.models import Series
from tabayyun.services import compare


def test_non_finite_numbers_never_reach_the_response():
    """NaN and both infinities become null; statistics with one of them are dropped whole."""
    assert compare._nullable([1.0, math.nan, math.inf, -math.inf, 0.0]) == [1.0, None, None, None, 0.0]
    stats = {"min": 0.0, "max": 2.0, "mean": 1.0, "sd": 1.0, "first_ts": 5, "first_value": 0.0}
    assert compare.finite_stats(stats) == stats
    assert compare.finite_stats({**stats, "sd": math.inf}) is None
    assert compare.finite_stats({**stats, "mean": math.nan}) is None
    assert compare.finite_stats(None) is None


MIN = 60 * 10**9


def _batch(values: list[float]) -> pa.RecordBatch:
    ts = pa.array([i * MIN for i in range(len(values))], pa.timestamp("ns", tz="UTC"))
    quality = pa.array(["good"] * len(values), pa.string())
    return pa.record_batch({"ts": ts, "value": pa.array(values, pa.float64()), "quality": quality})


def _series(interval_ns: int | None = None) -> Series:
    return Series(id=uuid.uuid4(), external_id="s", expected_interval_ns=interval_ns)


async def test_scatter_points():
    """The points come back in the order of the request's axes, on the series' grid."""
    x, y = _series(), _series(5 * MIN)
    batches = {str(x.id): _batch([float(i) for i in range(20)]), str(y.id): _batch([9.0] * 10 + [3.0] * 10)}
    out = await compare.pair_scatter(x, y, batches, 0, 20 * MIN)
    # The coarser expected interval is the grid: four bins of five minutes.
    assert (out.grid_ns, out.n_bins, out.missing) == (5 * MIN, 4, [])
    assert (out.ts, out.xs, out.ys) == (
        [0, 5 * MIN, 10 * MIN, 15 * MIN],
        [2.0, 7.0, 12.0, 17.0],
        [9.0] * 2 + [3.0] * 2,
    )
    with pytest.raises(compare.ScatterError, match="grid"):
        await compare.pair_scatter(x, y, batches, 0, 20 * MIN, grid="soon")


async def test_scatter_missing_and_not_finite():
    """A series without rows is missing; a bin whose mean overflowed is not a bin with a value."""
    x, y = _series(), _series()
    only_x = {str(x.id): _batch([1.0, 2.0, 3.0])}
    out = await compare.pair_scatter(x, y, only_x, 0, 3 * MIN)
    assert out.missing == [str(y.id)] and out.ts == [] and out.r is None
    none = await compare.pair_scatter(x, y, {}, 0, 1)
    assert none.missing == [str(x.id), str(y.id)] and none.grid_ns == 0
    # A grid that cannot be read is refused without rows too.
    with pytest.raises(compare.ScatterError, match="grid"):
        await compare.pair_scatter(x, y, {}, 0, 1, grid="soon")
    huge = {str(x.id): _batch([1e308, 1e308, 1.0, 2.0, 3.0, 4.0]), str(y.id): _batch([1.0] * 6)}
    out = await compare.pair_scatter(x, y, huge, 0, 6 * MIN, grid="2m")
    assert (out.n_bins, out.ts, out.xs, out.r) == (2, [2 * MIN, 4 * MIN], [1.5, 3.5], None)
