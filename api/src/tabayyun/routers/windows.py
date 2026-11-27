"""Request helpers shared by the endpoints that read a window of a series from the cache: the
series chart and profile (spec 025), the compare chart, the scatter and the group residual
(specs 018, 033). They read the raw layer unless the request names another one (spec 036)."""

from __future__ import annotations

from collections.abc import Sequence

import pyarrow as pa
import structlog
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from tabayyun.db.models import Series
from tabayyun.services import corrections, layers, series_chart
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


def layer_or_422(text: str | None, *, versions: bool = True) -> layers.LayerRef:
    """The layer a request names (raw without one); 422 naming the field for any other name.
    `versions=False` refuses `corrected@v{n}`, for the reads of several series."""
    try:
        return layers.parse_layer(text, versions=versions)
    except layers.LayerError as exc:
        raise HTTPException(status_code=422, detail=[{"field": "layer", "message": str(exc)}]) from exc


async def version_or_422(
    session: AsyncSession, series: Series, ref: layers.LayerRef
) -> tuple[int | None, str]:
    """The version of the series' corrected layer that `ref` reads (None reads raw) and the
    mark of its state for ETags; 422 `unknown_layer_version` for a version never approved."""
    try:
        return await layers.resolve(session, series.id, ref)
    except layers.UnknownLayerVersionError as exc:
        raise HTTPException(status_code=422, detail=layers.UNKNOWN_LAYER_VERSION) from exc


async def overlays_for(
    session: AsyncSession, series: Series, window: tuple[int, int] | None, version: int | None
) -> list[corrections.Layered] | None:
    """The approved corrections a read of the layer at `version` lays over the window's raw
    rows; None for a raw read. Separate from the read, so that reads of several series can
    run side by side while the session is used by one at a time."""
    if window is None or version is None:
        return None
    return await corrections.approved_layers(session, series.id, version, *window)


async def read_overlaid_or_503(
    cache: RunCache,
    series: Series,
    window: tuple[int, int] | None,
    overlays: Sequence[corrections.Layered] | None,
) -> pa.RecordBatch | None:
    """Rows of the window: raw for `overlays` None, else raw with those corrections laid over
    (spec 035, behaviour 3). None when there is no window; 503 when the store cannot be read."""
    if overlays is None:
        return await read_or_503(cache, series, window)
    if window is None:
        return None
    try:
        return await corrections.read_composed(cache, series, overlays, *window)
    except CacheError as exc:
        # As for raw: the store's own message goes to the log only.
        log.warning("series.cache_read_failed", series_id=str(series.id), error=str(exc))
        raise HTTPException(status_code=503, detail="cache_unavailable") from exc


async def read_layer_or_503(
    cache: RunCache,
    session: AsyncSession,
    series: Series,
    window: tuple[int, int] | None,
    version: int | None,
) -> pa.RecordBatch | None:
    """Rows of the window in the series' corrected layer at `version`, raw for None; None when
    there is no window; 503 when the store cannot be read."""
    overlays = await overlays_for(session, series, window, version)
    return await read_overlaid_or_503(cache, series, window, overlays)
