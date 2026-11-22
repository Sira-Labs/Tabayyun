"""Several series on one chart, the residual of a balance or a pair (spec 018) and the
scatter of one series against another (spec 033).

All read the raw layer of the Parquet cache like the series chart (spec 025) and hand the
rows to the core: `compare_series` for M4 points on the window's bins plus the statistics the
page normalises with, `residual` and `scatter` for the members aligned on the checks' grid.
"""

from __future__ import annotations

import asyncio
import math
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

import pyarrow as pa
import tabayyun_core
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tabayyun.authz import Scope
from tabayyun.db.models import Series, SeriesGroup
from tabayyun.services import series_chart
from tabayyun.services.groups import MemberOut

MIN_SERIES = 2
MAX_SERIES = 8
# The default window: the last 7 days of the members' data.
DEFAULT_WINDOW_NS = 7 * 86_400 * 10**9
# `tby.balance_residual`'s default loss band, as shares of the input (the core's defaults).
DEFAULT_LOSS = (-0.01, 0.05)


class NoResidualError(ValueError):
    """The group has no residual: it is neither a balance nor a pair, or its params are unusable."""


@dataclass(frozen=True)
class SeriesPoints:
    """One series of the compare chart: M4 points and the raw window's statistics."""

    series: Series
    ts: list[int]
    values: list[float | None]
    n_raw: int
    stats: dict[str, Any] | None


@dataclass(frozen=True)
class GroupResidual:
    """Chart points of a group's residual; `lo` and `hi` are the loss band of a balance."""

    kind: Literal["balance", "pair"]
    grid_ns: int
    ts: list[int]
    residual: list[float | None]
    lo: list[float | None] | None
    hi: list[float | None] | None
    missing: list[str]


async def series_by_id(
    session: AsyncSession, scope: Scope, ids: Sequence[uuid.UUID]
) -> tuple[list[Series], list[uuid.UUID]]:
    """The workspace's series for `ids`, in that order, and the ids it does not have."""
    stmt = select(Series).where(Series.id.in_(ids), Series.workspace_id == scope.workspace_id)
    found = {s.id: s for s in (await session.scalars(stmt)).all()}
    return [found[i] for i in ids if i in found], [i for i in ids if i not in found]


async def extents(session: AsyncSession, series: Sequence[Series]) -> list[series_chart.Extent]:
    """The covered extents of the series that have any data in the cache."""
    found = [await series_chart.extent(session, s.id) for s in series]
    return [e for e in found if e is not None]


def default_window(found: Sequence[series_chart.Extent]) -> tuple[int, int] | None:
    """The last 7 days of the members' data, or None when no member has any."""
    if not found:
        return None
    start, end = min(e.start_ns for e in found), max(e.end_ns for e in found)
    return max(start, end - DEFAULT_WINDOW_NS), end


def check_size(found: Sequence[series_chart.Extent], start_ns: int, end_ns: int, limit: int) -> None:
    """Refuse a window whose series together hold more than `limit` rows.

    Raises:
        WindowTooLargeError: the coverage estimate over all series exceeds `limit`.
    """
    estimate = sum(series_chart.estimate_rows(e, start_ns, end_ns) for e in found)
    if estimate > limit:
        raise series_chart.WindowTooLargeError(estimate, limit)


def _nullable(values: Sequence[float]) -> list[float | None]:
    """NaN and infinities become None: JSON has neither, and a sum of huge samples can overflow."""
    return [float(v) if math.isfinite(v) else None for v in values]


def finite_stats(stats: dict[str, Any] | None) -> dict[str, Any] | None:
    """The window statistics, or None when one of them is not finite (nothing to normalise with)."""
    if stats is None or not all(math.isfinite(stats[k]) for k in ("min", "max", "mean", "sd", "first_value")):
        return None
    return stats


async def series_points(
    series: Series, batch: pa.RecordBatch | None, start_ns: int, end_ns: int, buckets: int
) -> SeriesPoints:
    """M4 points of one series on the window's bins and the statistics of its raw samples."""
    if batch is None:
        return SeriesPoints(series=series, ts=[], values=[], n_raw=0, stats=None)
    out = await asyncio.to_thread(
        tabayyun_core.compare_series,
        batch,
        start_ns,
        end_ns,
        buckets,
        expected_interval_ns=series.expected_interval_ns,
    )
    return SeriesPoints(
        series=series,
        ts=[int(t) for t in out["ts"]],
        values=_nullable(out["values"]),
        n_raw=int(out["n_raw"]),
        stats=finite_stats(out["stats"]),
    )


# A window without rows, in the cache's columns.
_NO_ROWS = pa.record_batch(
    {
        "ts": pa.array([], pa.timestamp("ns", tz="UTC")),
        "value": pa.array([], pa.float64()),
        "quality": pa.array([], pa.string()),
    }
)


@dataclass(frozen=True)
class PairScatter:
    """Points of one series against another on the checks' grid (spec 033)."""

    grid_ns: int
    n_bins: int
    r: float | None
    ts: list[int]
    xs: list[float]
    ys: list[float]
    missing: list[str]


class ScatterError(ValueError):
    """The core refused the request: `grid` is neither `auto` nor a duration it can read."""


async def pair_scatter(
    x: Series,
    y: Series,
    batches: dict[str, pa.RecordBatch],
    start_ns: int,
    end_ns: int,
    *,
    grid: str = "auto",
    max_points: int = 2000,
) -> PairScatter:
    """`y` against `x` over the window: one point per grid bin in which both have a value.

    `batches` holds the window's rows of the series that have any; one without is reported
    in `missing`, and there are then no points. Every value is finite: a bin whose mean
    overflowed counts as a bin without a value.

    Raises:
        ScatterError: the core refused the request (a `grid` it cannot parse).
    """
    ids = [str(x.id), str(y.id)]
    # Without any rows the core is still asked, so that `grid` is judged the same way.
    template = next(iter(batches.values()), _NO_ROWS)
    tables = {sid: batches.get(sid, _empty_like(template)) for sid in ids}
    metas = {
        str(s.id): {"id": str(s.id), "expected_interval_ns": s.expected_interval_ns}
        for s in (x, y)
        if s.expected_interval_ns
    }
    try:
        out = await asyncio.to_thread(
            tabayyun_core.scatter,
            tables,
            metas,
            ids[0],
            ids[1],
            start_ns,
            end_ns,
            grid=grid,
            max_points=max_points,
        )
    except ValueError as exc:
        raise ScatterError(str(exc)) from exc
    return PairScatter(
        grid_ns=int(out["grid_ns"]),
        n_bins=int(out["n_bins"]),
        r=float(out["r"]) if out["r"] is not None and math.isfinite(out["r"]) else None,
        ts=[int(t) for t in out["ts"]],
        xs=[float(v) for v in out["x"]],
        ys=[float(v) for v in out["y"]],
        missing=[str(m) for m in out["missing"]],
    )


def residual_kind(group: SeriesGroup, members: Sequence[MemberOut]) -> Literal["balance", "pair"]:
    """`balance` for a balance group, `pair` for any other group of exactly two members.

    Raises:
        NoResidualError: any other group.
    """
    if group.kind == "balance":
        return "balance"
    if len(members) == 2:
        return "pair"
    raise NoResidualError("only balance groups and groups of exactly two members have a residual")


def signs(kind: str, members: Sequence[MemberOut]) -> list[tuple[str, float]]:
    """`(series id, sign)` per member: inputs minus outputs for a balance, A − B for a pair."""
    if kind == "balance":
        return [(str(m.series_id), 1.0 if m.role == "input" else -1.0) for m in members]
    return [(str(m.series_id), 1.0 if i == 0 else -1.0) for i, m in enumerate(members)]


def loss_band(params: dict[str, Any]) -> tuple[float, float]:
    """The balance's loss band from the group's params, with the check's defaults.

    Raises:
        NoResidualError: the params hold a band the check would refuse.
    """
    lo, hi = params.get("loss_min", DEFAULT_LOSS[0]), params.get("loss_max", DEFAULT_LOSS[1])
    numbers = all(isinstance(x, int | float) and not isinstance(x, bool) for x in (lo, hi))
    if not numbers or not lo < hi:
        raise NoResidualError("the group's loss_min must be a number below its loss_max")
    return float(lo), float(hi)


def _empty_like(batch: pa.RecordBatch) -> pa.RecordBatch:
    return batch.slice(0, 0)


async def group_residual(
    group: SeriesGroup,
    members: Sequence[MemberOut],
    series: Sequence[Series],
    batches: dict[str, pa.RecordBatch],
    start_ns: int,
    end_ns: int,
    buckets: int,
) -> GroupResidual:
    """The residual of `group` over the window as chart points.

    `batches` holds the window's rows of the members that have any. A member without usable
    data is reported in `missing`, and the residual is then empty.

    Raises:
        NoResidualError: the group has no residual (kind, member count or params).
    """
    kind = residual_kind(group, members)
    loss = loss_band(group.params) if kind == "balance" else None
    grid = group.params.get("grid", "auto")
    if not isinstance(grid, str):
        raise NoResidualError("the group's grid must be `auto` or a duration such as 15m")
    order = signs(kind, members)
    if not batches:
        missing = [sid for sid, _ in order]
        return GroupResidual(kind, 0, [], [], [] if loss else None, [] if loss else None, missing)
    template = next(iter(batches.values()))
    tables = {sid: batches.get(sid, _empty_like(template)) for sid, _ in order}
    metas = {
        str(s.id): {"id": str(s.id), "expected_interval_ns": s.expected_interval_ns}
        for s in series
        if s.expected_interval_ns
    }
    try:
        out = await asyncio.to_thread(
            tabayyun_core.residual,
            tables,
            metas,
            order,
            start_ns,
            end_ns,
            grid=grid,
            buckets=buckets,
            loss=loss,
        )
    except ValueError as exc:
        raise NoResidualError(str(exc)) from exc
    return GroupResidual(
        kind=kind,
        grid_ns=int(out["grid_ns"]),
        ts=[int(t) for t in out["ts"]],
        residual=_nullable(out["residual"]),
        lo=_nullable(out["lo"]) if loss else None,
        hi=_nullable(out["hi"]) if loss else None,
        missing=[str(m) for m in out["missing"]],
    )
