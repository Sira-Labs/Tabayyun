"""Compare endpoints: 2 to 8 series on the same chart bins (spec 018) and the scatter of one
series against another (spec 033)."""

from __future__ import annotations

import asyncio
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from tabayyun.authz import ReadScope, get_session
from tabayyun.db.models import Series
from tabayyun.routers.windows import read_or_503, window_or_422
from tabayyun.services import compare, series_chart

router = APIRouter(prefix="/api/compare", tags=["compare"])


class BinsOut(BaseModel):
    """The window every series is binned on: `[from, to)` in ns strings, cut into `width_px`
    equal bins. `from` and `to` are null when no window was given and no series has data."""

    from_: str | None = Field(alias="from")
    to: str | None
    width_px: int


class FirstOut(BaseModel):
    """The earliest usable raw sample of the window."""

    ts: str
    value: float


class StatsOut(BaseModel):
    """Statistics of the window's usable, finite raw samples, before downsampling."""

    min: float
    max: float
    mean: float
    sd: float = Field(description="population standard deviation")
    first: FirstOut


class CompareSeriesOut(BaseModel):
    """One series: its M4 points (`values` is null where the line breaks) and its statistics,
    which are null when the window holds no usable sample."""

    id: str
    name: str
    unit: str | None
    physical_min: float | None
    physical_max: float | None
    ts: list[str]
    values: list[float | None]
    n_raw: int
    stats: StatsOut | None


class CompareChartOut(BaseModel):
    """`GET /api/compare/chart`."""

    bins: BinsOut
    series: list[CompareSeriesOut]


def _ids_or_422(raw: list[str]) -> list[uuid.UUID]:
    """2 to 8 distinct series ids; 422 otherwise."""
    if not compare.MIN_SERIES <= len(raw) <= compare.MAX_SERIES:
        raise HTTPException(
            status_code=422,
            detail=f"give {compare.MIN_SERIES} to {compare.MAX_SERIES} series, got {len(raw)}",
        )
    try:
        ids = [uuid.UUID(value) for value in raw]
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="series: not a series id") from exc
    if len(set(ids)) != len(ids):
        raise HTTPException(status_code=422, detail="series: the same id is given twice")
    return ids


def _stats_out(stats: dict[str, float] | None) -> StatsOut | None:
    if stats is None:
        return None
    return StatsOut(
        min=stats["min"],
        max=stats["max"],
        mean=stats["mean"],
        sd=stats["sd"],
        first=FirstOut(ts=str(int(stats["first_ts"])), value=stats["first_value"]),
    )


@router.get("/chart", response_model=CompareChartOut)
async def get_compare_chart(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    scope: ReadScope,
    series: Annotated[list[str], Query()],
    from_: Annotated[str | None, Query(alias="from")] = None,
    to: str | None = None,
    width_px: Annotated[int, Query(ge=100, le=4000)] = 1200,
) -> CompareChartOut:
    """M4 points of 2 to 8 series on identical bins (default window: the last 7 days of their
    data), each with the statistics of its raw samples in the window.

    422 for fewer than 2 or more than 8 series and for `window_too_large`; 404 names the ids
    the workspace does not have.
    """
    ids = _ids_or_422(series)
    members, unknown = await compare.series_by_id(session, scope, ids)
    if unknown:
        detail = {"message": "series not found", "unknown": [str(i) for i in unknown]}
        raise HTTPException(status_code=404, detail=detail)
    found = await compare.extents(session, members)
    window = window_or_422(from_, to) or compare.default_window(found)
    if window is None:
        points = [await compare.series_points(s, None, 0, 1, width_px) for s in members]
    else:
        try:
            compare.check_size(found, *window, request.app.state.settings.chart_max_rows)
        except series_chart.WindowTooLargeError as exc:
            raise HTTPException(status_code=422, detail="window_too_large") from exc
        cache = request.app.state.run_cache

        async def one(s: Series) -> compare.SeriesPoints:
            return await compare.series_points(s, await read_or_503(cache, s, window), *window, width_px)

        # The reads and the downsampling run in threads, so the series do not wait for each other.
        points = list(await asyncio.gather(*(one(s) for s in members)))
    return CompareChartOut(
        bins=BinsOut.model_validate(
            {
                "from": str(window[0]) if window else None,
                "to": str(window[1]) if window else None,
                "width_px": width_px,
            }
        ),
        series=[
            CompareSeriesOut(
                id=str(p.series.id),
                name=p.series.name or p.series.external_id,
                unit=p.series.unit,
                physical_min=p.series.physical_min,
                physical_max=p.series.physical_max,
                ts=[str(t) for t in p.ts],
                values=p.values,
                n_raw=p.n_raw,
                stats=_stats_out(p.stats),
            )
            for p in points
        ],
    )


class ScatterAxisOut(BaseModel):
    """The series on one axis of the scatter."""

    id: str
    name: str
    unit: str | None


class ScatterOut(BaseModel):
    """`GET /api/compare/scatter`: one point per grid bin in which both series have a value."""

    window: dict[str, str] | None
    grid_ns: str
    x: ScatterAxisOut
    y: ScatterAxisOut
    n_bins: int = Field(description="grid bins with a value of both series, before the cap")
    r: float | None = Field(description="Pearson correlation over all those bins")
    ts: list[str]
    xs: list[float]
    ys: list[float]
    missing: list[str]


def _axis(series: Series) -> ScatterAxisOut:
    return ScatterAxisOut(id=str(series.id), name=series.name or series.external_id, unit=series.unit)


def _pair_or_422(x: str, y: str) -> list[uuid.UUID]:
    """The two different series ids of a scatter; 422 otherwise."""
    try:
        ids = [uuid.UUID(x), uuid.UUID(y)]
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="x, y: not a series id") from exc
    if ids[0] == ids[1]:
        raise HTTPException(status_code=422, detail="x and y are the same series")
    return ids


@router.get("/scatter", response_model=ScatterOut)
async def get_compare_scatter(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    scope: ReadScope,
    x: str,
    y: str,
    from_: Annotated[str | None, Query(alias="from")] = None,
    to: str | None = None,
    grid: Annotated[str, Query(max_length=20)] = "auto",
    max_points: Annotated[int, Query(ge=100, le=5000)] = 2000,
) -> ScatterOut:
    """Series `y` against series `x` on the grid the cross-series checks use (default window:
    the last 7 days of their data), at most `max_points` points in time order.

    422 for the same series twice, a `grid` that is neither `auto` nor a duration, and
    `window_too_large`; 404 names the ids the workspace does not have.
    """
    members, unknown = await compare.series_by_id(session, scope, _pair_or_422(x, y))
    if unknown:
        detail = {"message": "series not found", "unknown": [str(i) for i in unknown]}
        raise HTTPException(status_code=404, detail=detail)
    found = await compare.extents(session, members)
    window = window_or_422(from_, to) or compare.default_window(found)
    batches = {}
    if window is not None:
        try:
            compare.check_size(found, *window, request.app.state.settings.chart_max_rows)
        except series_chart.WindowTooLargeError as exc:
            raise HTTPException(status_code=422, detail="window_too_large") from exc
        cache = request.app.state.run_cache
        rows = await asyncio.gather(*(read_or_503(cache, s, window) for s in members))
        batches = {str(s.id): batch for s, batch in zip(members, rows, strict=True) if batch is not None}
    try:
        start_ns, end_ns = window or (0, 1)
        out = await compare.pair_scatter(
            members[0], members[1], batches, start_ns, end_ns, grid=grid, max_points=max_points
        )
    except compare.ScatterError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ScatterOut(
        window={"from": str(window[0]), "to": str(window[1])} if window else None,
        grid_ns=str(out.grid_ns),
        x=_axis(members[0]),
        y=_axis(members[1]),
        n_bins=out.n_bins,
        r=out.r,
        ts=[str(t) for t in out.ts],
        xs=out.xs,
        ys=out.ys,
        missing=out.missing,
    )
