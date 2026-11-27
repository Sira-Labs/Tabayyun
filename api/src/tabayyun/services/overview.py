"""The workspace overview (spec 032): score tiles with their trend, the worst series, new
findings per day and the sources' health, read from `score_days` instead of `scores`."""

from __future__ import annotations

import math
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import Date, cast, func, select, true
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import ColumnElement

from tabayyun.authz.scope import Scope
from tabayyun.db.models import SCORE_DIMENSIONS, Finding, ScoreDay, Series, Source
from tabayyun.services.findings import SEVERITIES
from tabayyun.services.series import UNRESOLVED_STATUSES

WINDOWS = (7, 30, 90)
DEFAULT_WINDOW = 30
OVERALL = "overall"
METRICS = (OVERALL, *SCORE_DIMENSIONS)
WORST_SHARE = 0.05
WORST_LISTED = 50
MAX_ASSET_PREFIX = 200
LAYER = "raw"


@dataclass(frozen=True)
class Filters:
    """What narrows the overview: the window in days and, optionally, an asset path prefix
    and one source."""

    days: int = DEFAULT_WINDOW
    asset: str | None = None
    source: uuid.UUID | None = None


@dataclass(frozen=True)
class Latest:
    """A series' newest day in the window: its scores by metric (None where it has none)."""

    series_id: uuid.UUID
    values: Mapping[str, float | None]
    computed_at: datetime

    def lowest(self) -> tuple[str, float]:
        """The metric with the lowest value and that value; the overall score on a tie."""
        present = [(v, i, m) for i, m in enumerate(METRICS) if (v := self.values.get(m)) is not None]
        value, _, metric = min(present)
        return metric, value


def window_of(days: int, today: date) -> tuple[date, date]:
    """The first and last day of a window of `days` UTC days that ends today."""
    return today - timedelta(days=days - 1), today


def summarise_tiles(
    latest: Sequence[Latest], trend: Mapping[str, list[dict[str, Any]]]
) -> list[dict[str, Any]]:
    """One tile per metric, in order: mean, lowest value and number of series over each
    series' newest day, with the metric's trend."""
    tiles = []
    for metric in METRICS:
        values = [v for row in latest if (v := row.values.get(metric)) is not None]
        tiles.append(
            {
                "metric": metric,
                "mean": sum(values) / len(values) if values else None,
                "min": min(values) if values else None,
                "n_series": len(values),
                "trend": trend.get(metric, []),
            }
        )
    return tiles


def rank_worst(latest: Sequence[Latest]) -> tuple[int, list[Latest]]:
    """How many series make the worst 5 % (rounded up) and the first of them, lowest value
    first; ties go to the lower overall score, then to the series id for a stable order."""
    n = math.ceil(len(latest) * WORST_SHARE)

    def key(row: Latest) -> tuple[float, float, str]:
        return row.lowest()[1], row.values.get(OVERALL) or 0.0, str(row.series_id)

    return n, sorted(latest, key=key)[: min(n, WORST_LISTED)]


def fill_days(start: date, end: date, counts: Iterable[tuple[date, str, int]]) -> list[dict[str, Any]]:
    """One entry per day from `start` to `end` with a count per severity, zeros included."""
    by_day: dict[date, dict[str, int]] = {}
    for day, severity, n in counts:
        by_day.setdefault(day, {})[severity] = n
    days = []
    for offset in range((end - start).days + 1):
        day = start + timedelta(days=offset)
        found = by_day.get(day, {})
        days.append({"day": day.isoformat(), **{s: found.get(s, 0) for s in reversed(SEVERITIES)}})
    return days


async def record_score_day(
    session: AsyncSession,
    scope: Scope,
    *,
    series_id: uuid.UUID,
    overall: float,
    dimensions: Mapping[str, Any],
    n_findings: int,
    computed_at: datetime,
    layer: str = LAYER,
) -> None:
    """Keep the score as its series' score of that UTC day in `layer` (raw unless named), in
    the caller's transaction; an older score of the same day is replaced, a newer one stays.
    The overview reads the raw layer only."""
    values = {
        "org_id": scope.org_id,
        "workspace_id": scope.workspace_id,
        "series_id": series_id,
        "layer": layer,
        "day": computed_at.astimezone(UTC).date(),
        "overall": overall,
        **{d: dimensions.get(d) for d in SCORE_DIMENSIONS},
        "n_findings": n_findings,
        "computed_at": computed_at,
    }
    stmt = insert(ScoreDay).values(values)
    kept = ("org_id", "workspace_id", "series_id", "layer", "day")
    stmt = stmt.on_conflict_do_update(
        index_elements=[ScoreDay.series_id, ScoreDay.layer, ScoreDay.day],
        set_={k: stmt.excluded[k] for k in values if k not in kept},
        where=ScoreDay.computed_at <= stmt.excluded.computed_at,
    )
    await session.execute(stmt)


def _series_filter(scope: Scope, filters: Filters) -> list[ColumnElement[bool]]:
    """Conditions on `series` for the workspace and the filters."""
    conditions: list[ColumnElement[bool]] = [Series.workspace_id == scope.workspace_id]
    if filters.asset:
        conditions.append(Series.asset_path.startswith(filters.asset, autoescape=True))
    if filters.source is not None:
        conditions.append(Series.source_id == filters.source)
    return conditions


def _narrowed(filters: Filters) -> bool:
    return bool(filters.asset) or filters.source is not None


async def _latest(
    session: AsyncSession, scope: Scope, filters: Filters, start: date, end: date
) -> list[Latest]:
    """Each series' newest day in the window, both ends included.

    One probe of the primary key per series (a lateral join), not a sort of every day of
    the window: with 10 000 series and 30 days that is 10 000 index lookups instead of
    300 000 rows.
    """
    columns = [getattr(ScoreDay, m) for m in METRICS]
    newest = (
        select(*columns, ScoreDay.computed_at)
        .where(
            ScoreDay.series_id == Series.id,
            ScoreDay.layer == LAYER,
            ScoreDay.day.between(start, end),
        )
        .order_by(ScoreDay.day.desc())
        .limit(1)
        .lateral("newest")
    )
    stmt = select(Series.id, newest).join(newest, true()).where(*_series_filter(scope, filters))
    return [
        Latest(row[0], dict(zip(METRICS, row[1 : 1 + len(METRICS)], strict=True)), row[-1])
        for row in await session.execute(stmt)
    ]


async def _trend(
    session: AsyncSession, scope: Scope, filters: Filters, start: date, end: date
) -> dict[str, list[dict[str, Any]]]:
    """Per metric, each day's mean over the series scored that day and how many they were."""
    columns = [getattr(ScoreDay, m) for m in METRICS]
    stmt = (
        select(ScoreDay.day, *(func.avg(c) for c in columns), *(func.count(c) for c in columns))
        .where(
            ScoreDay.workspace_id == scope.workspace_id,
            ScoreDay.layer == LAYER,
            ScoreDay.day.between(start, end),
        )
        .group_by(ScoreDay.day)
        .order_by(ScoreDay.day)
    )
    if _narrowed(filters):
        stmt = stmt.join(Series, Series.id == ScoreDay.series_id).where(*_series_filter(scope, filters))
    trend: dict[str, list[dict[str, Any]]] = {m: [] for m in METRICS}
    for row in await session.execute(stmt):
        means, counts = row[1 : 1 + len(METRICS)], row[1 + len(METRICS) :]
        for metric, mean, n in zip(METRICS, means, counts, strict=True):
            if n:
                trend[metric].append({"day": row[0].isoformat(), "mean": float(mean), "n": int(n)})
    return trend


async def _worst_items(session: AsyncSession, worst: Sequence[Latest]) -> list[dict[str, Any]]:
    """The worst series with their names and unresolved findings, in rank order."""
    ids = [row.series_id for row in worst]
    if not ids:
        return []
    names = {
        sid: (name, asset)
        for sid, name, asset in await session.execute(
            select(Series.id, Series.name, Series.asset_path).where(Series.id.in_(ids))
        )
    }
    open_counts = {
        sid: int(n)
        for sid, n in await session.execute(
            select(Finding.series_id, func.count())
            .where(Finding.series_id.in_(ids), Finding.status.in_(UNRESOLVED_STATUSES))
            .group_by(Finding.series_id)
        )
    }
    items = []
    for row in worst:
        metric, value = row.lowest()
        name, asset = names.get(row.series_id, ("", None))
        items.append(
            {
                "series_id": str(row.series_id),
                "name": name,
                "asset_path": asset,
                "overall": row.values[OVERALL],
                "metric": metric,
                "value": value,
                "open_findings": open_counts.get(row.series_id, 0),
                "computed_at": row.computed_at,
            }
        )
    return items


async def _findings(
    session: AsyncSession, scope: Scope, filters: Filters, start: date, end: date
) -> dict[str, Any]:
    """Unresolved findings by severity, and the findings first stored on each day of the window."""
    narrowed = _narrowed(filters)

    def of_workspace(stmt: Any) -> Any:
        stmt = stmt.where(Finding.workspace_id == scope.workspace_id)
        if narrowed:
            stmt = stmt.join(Series, Series.id == Finding.series_id).where(*_series_filter(scope, filters))
        return stmt

    unresolved = of_workspace(
        select(Finding.severity, func.count())
        .where(Finding.status.in_(UNRESOLVED_STATUSES))
        .group_by(Finding.severity)
    )
    open_by = {severity: int(n) for severity, n in await session.execute(unresolved)}
    day = cast(func.timezone("UTC", Finding.created_at), Date)
    first = datetime(start.year, start.month, start.day, tzinfo=UTC)
    per_day = of_workspace(
        select(day, Finding.severity, func.count())
        .where(
            Finding.created_at >= first, Finding.created_at < first + timedelta(days=(end - start).days + 1)
        )
        .group_by(day, Finding.severity)
    )
    counts = [(d, severity, int(n)) for d, severity, n in await session.execute(per_day)]
    return {
        "open": {s: open_by.get(s, 0) for s in reversed(SEVERITIES)},
        "days": fill_days(start, end, counts),
    }


async def _sources(session: AsyncSession, scope: Scope) -> list[dict[str, Any]]:
    """Every source of the workspace with its health and series count, by name."""
    n_series = func.count(Series.id)
    rows = await session.execute(
        select(Source, n_series)
        .outerjoin(Series, Series.source_id == Source.id)
        .where(Source.workspace_id == scope.workspace_id)
        .group_by(Source.id)
        .order_by(func.lower(Source.name), Source.id)
    )
    return [
        {
            "id": str(source.id),
            "name": source.name,
            "type": source.type,
            "enabled": source.enabled,
            "status": (source.health or {}).get("status", "unknown"),
            "last_fetch": (source.health or {}).get("last_fetch"),
            "n_series": int(n),
        }
        for source, n in rows
    ]


async def overview(session: AsyncSession, scope: Scope, filters: Filters, *, today: date) -> dict[str, Any]:
    """The overview of the workspace for the window that ends `today`."""
    start, end = window_of(filters.days, today)
    latest = await _latest(session, scope, filters, start, end)
    n_worst, worst = rank_worst(latest)
    total = await session.scalar(
        select(func.count()).select_from(Series).where(*_series_filter(scope, filters))
    )
    return {
        "window": {"days": filters.days, "start": start.isoformat(), "end": end.isoformat()},
        "series": {"total": int(total or 0), "scored": len(latest)},
        "tiles": summarise_tiles(latest, await _trend(session, scope, filters, start, end)),
        "worst": {"share": WORST_SHARE, "n": n_worst, "items": await _worst_items(session, worst)},
        "findings": await _findings(session, scope, filters, start, end),
        "sources": await _sources(session, scope),
    }
