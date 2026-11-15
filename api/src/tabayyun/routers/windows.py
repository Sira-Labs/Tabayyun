"""Request helpers shared by the endpoints that read a window of raw data from the cache: the
series chart and profile (spec 025), the compare chart and the group residual (spec 018)."""

from __future__ import annotations

import pyarrow as pa
import structlog
from fastapi import HTTPException

from tabayyun.db.models import Series
from tabayyun.services import series_chart
from tabayyun.services.cache import CacheError, RunCache
from tabayyun.services.timeconv import parse_time_ns, to_core_ns

log = structlog.get_logger()


def window_or_422(from_: str | None, to: str | None) -> tuple[int, int] | None:
    """The requested `[from, to)` in ns, None when neither is given; 422 when malformed."""
    if from_ is None and to is None:
        return None
    if from_ is None or to is None:
        raise HTTPException(status_code=422, detail="give both from and to, or neither")
    try:
        start, end = to_core_ns(parse_time_ns(from_)), to_core_ns(parse_time_ns(to))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"from/to: {exc}") from exc
    if end <= start:
        raise HTTPException(status_code=422, detail="to must be later than from")
    return start, end


async def read_or_503(
    cache: RunCache, series: Series, window: tuple[int, int] | None
) -> pa.RecordBatch | None:
    """Rows of the window, None when there is no window; 503 when the store cannot be read."""
    if window is None:
        return None
    try:
        return await series_chart.read_window(cache, series, *window)
    except CacheError as exc:
        # The store's own message goes to the log only: it names hosts and buckets.
        log.warning("series.cache_read_failed", series_id=str(series.id), error=str(exc))
        raise HTTPException(status_code=503, detail="cache_unavailable") from exc
