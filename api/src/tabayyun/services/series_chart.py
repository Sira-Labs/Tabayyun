"""Chart points and on-the-fly profiles of one series from the Parquet cache (spec 025).

The chart is M4 of the raw layer on window bins (the core's `chart`); the profile is the
core's baseline profile of a window without bad-quality samples. Both read the cache through
`RunCache` in a thread, and size their windows from the `coverage` table, which is the only
record of what the cache holds (spec 006).
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import tabayyun_core
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tabayyun.db.models import Coverage, Series
from tabayyun.services.cache import RAW_LAYER, RunCache
from tabayyun.services.timeconv import datetime_to_ns

# A window over this many rows (by coverage) opens whole; a larger extent opens on its last week.
DEFAULT_WHOLE_ROWS = 2_000_000
DEFAULT_RECENT_NS = 7 * 86_400 * 10**9
# The profile's default window: the catalogue's baseline window, trailing 28 days.
PROFILE_WINDOW_NS = 28 * 86_400 * 10**9
# `tby.operational_range`'s default `k`: the band is [p0.1 − k·MAD, p99.9 + k·MAD].
BAND_K = 1.0
# Quality codes of the cache's `quality` column (the core's `Quality::as_u8`).
QUALITY_CODES = {"good": 0, "uncertain": 1, "bad": 2, "estimated": 3}


@dataclass(frozen=True)
class CoverageRange:
    """One recorded write of a series: `[start_ns, end_ns)` with its row count."""

    start_ns: int
    end_ns: int
    rows: int
    written_at: datetime


@dataclass(frozen=True)
class Extent:
    """What the cache holds of a series, from its coverage rows."""

    start_ns: int
    end_ns: int
    rows: int
    ranges: tuple[CoverageRange, ...]

    @property
    def version(self) -> str:
        """Changes whenever a write lands, for ETags."""
        latest = max(r.written_at for r in self.ranges)
        return f"{latest.isoformat()}:{self.rows}:{len(self.ranges)}"


@dataclass(frozen=True)
class ChartPoints:
    """Downsampled points of one window."""

    ts: list[int]
    values: list[float | None]
    quality: list[tuple[int, int, str]]
    n_raw: int


class WindowTooLargeError(Exception):
    """The window holds more rows than a chart may read; `estimate` is the coverage estimate."""

    def __init__(self, estimate: int, limit: int) -> None:
        super().__init__(f"the window holds about {estimate} rows; the limit is {limit}")
        self.estimate = estimate
        self.limit = limit


async def extent(session: AsyncSession, series_id: uuid.UUID, layer: str = RAW_LAYER) -> Extent | None:
    """The series' covered extent, or None when the cache holds nothing of it."""
    rows = await session.execute(
        select(Coverage.range_start, Coverage.range_end, Coverage.rows, Coverage.written_at)
        .where(Coverage.series_id == series_id, Coverage.layer == layer)
        .order_by(Coverage.range_start)
    )
    ranges = tuple(
        CoverageRange(datetime_to_ns(s), datetime_to_ns(e), int(n), w) for s, e, n, w in rows.all()
    )
    if not ranges:
        return None
    return Extent(
        start_ns=min(r.start_ns for r in ranges),
        end_ns=max(r.end_ns for r in ranges),
        rows=sum(r.rows for r in ranges),
        ranges=ranges,
    )


def estimate_rows(ext: Extent, start_ns: int, end_ns: int) -> int:
    """Rows of `[start_ns, end_ns)`: each range's rows scaled by its share inside the window.

    Overlapping writes count twice, so the estimate errs high, which suits a cap.
    """
    total = 0.0
    for r in ext.ranges:
        span = r.end_ns - r.start_ns
        overlap = min(r.end_ns, end_ns) - max(r.start_ns, start_ns)
        if overlap <= 0 or span <= 0:
            continue
        total += r.rows * overlap / span
    return math.ceil(total)


def default_chart_window(ext: Extent) -> tuple[int, int]:
    """The whole extent when it is small enough, else its latest 7 days."""
    if ext.rows <= DEFAULT_WHOLE_ROWS:
        return ext.start_ns, ext.end_ns
    return max(ext.start_ns, ext.end_ns - DEFAULT_RECENT_NS), ext.end_ns


def default_profile_window(ext: Extent) -> tuple[int, int]:
    """The trailing 28 days of the extent."""
    return max(ext.start_ns, ext.end_ns - PROFILE_WINDOW_NS), ext.end_ns


def check_size(ext: Extent | None, start_ns: int, end_ns: int, limit: int) -> None:
    """Refuse a window that would read more than `limit` rows.

    Raises:
        WindowTooLargeError: the coverage estimate of the window exceeds `limit`.
    """
    if ext is None:
        return
    estimate = estimate_rows(ext, start_ns, end_ns)
    if estimate > limit:
        raise WindowTooLargeError(estimate, limit)


def etag(*parts: object) -> str:
    """A strong ETag over the parts that decide a response."""
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:32]
    return f'"{digest}"'


async def read_window(cache: RunCache, series: Series, start_ns: int, end_ns: int) -> pa.RecordBatch | None:
    """Raw rows of one series over `[start_ns, end_ns)`, or None when there are none.

    Raises:
        CacheError: the store cannot be opened or read.
    """
    batches = await asyncio.to_thread(
        cache.read_series,
        source_id=str(series.source_id),
        series_ids=[str(series.id)],
        start_ns=start_ns,
        end_ns=end_ns,
    )
    return batches.get(str(series.id))


async def chart_points(
    batch: pa.RecordBatch | None, start_ns: int, end_ns: int, buckets: int, expected_interval_ns: int | None
) -> ChartPoints:
    """M4 points, gap breaks and quality runs of a window; NaN becomes None."""
    if batch is None:
        return ChartPoints(ts=[], values=[], quality=[], n_raw=0)
    out = await asyncio.to_thread(
        tabayyun_core.chart, batch, start_ns, end_ns, buckets, expected_interval_ns=expected_interval_ns
    )
    values = [None if v != v else float(v) for v in out["values"]]  # NaN is the only v != v
    quality = [(int(s), int(e), str(q)) for s, e, q in out["quality"]]
    ts = [int(t) for t in out["ts"]]
    return ChartPoints(ts=ts, values=values, quality=quality, n_raw=int(out["n_raw"]))


def quality_counts(batch: pa.RecordBatch | None) -> dict[str, int]:
    """Samples of each quality class in the batch."""
    counts = dict.fromkeys(QUALITY_CODES, 0)
    if batch is None:
        return counts
    by_code = {code: name for name, code in QUALITY_CODES.items()}
    for item in pc.value_counts(batch.column("quality")).to_pylist():
        name = by_code.get(int(item["values"]))
        if name is not None:
            counts[name] += int(item["counts"])
    return counts


async def window_profile(batch: pa.RecordBatch | None, series_id: str) -> dict[str, Any] | None:
    """The core profile of the batch without bad-quality samples, or None when none is left."""
    if batch is None:
        return None
    usable = batch.filter(pc.not_equal(batch.column("quality"), QUALITY_CODES["bad"]))
    if usable.num_rows == 0:
        return None
    return await asyncio.to_thread(tabayyun_core.profile, usable, {"id": series_id}, quality_col="quality")


def band(series: Series, profile: dict[str, Any] | None) -> dict[str, Any] | None:
    """The operating band: the series' operational range, else learned from the profile."""
    if series.operational_min is not None and series.operational_max is not None:
        return {"lo": series.operational_min, "hi": series.operational_max, "source": "metadata"}
    if profile is None:
        return None
    lo, hi, mad = profile.get("p001"), profile.get("p999"), profile.get("mad")
    if lo is None or hi is None or mad is None:
        return None
    return {"lo": lo - BAND_K * mad, "hi": hi + BAND_K * mad, "source": "profile"}
