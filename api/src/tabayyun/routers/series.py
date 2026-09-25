"""Series endpoints: catalogue, metadata and partial updates (spec 004), metric points and
score history (spec 003)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from tabayyun import core
from tabayyun.authz import ReadScope, Scope, WriteScope, get_session
from tabayyun.db.models import Series
from tabayyun.services import findings as findings_service
from tabayyun.services import series as series_service
from tabayyun.services.timeconv import datetime_to_ns, parse_time

router = APIRouter(prefix="/api/series", tags=["series"])

SeriesKind = core.SeriesKind
FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]


class LatestScore(BaseModel):
    """The newest raw-layer score of a series."""

    overall: float
    computed_at: datetime


class SeriesSummary(BaseModel):
    """One row of `GET /api/series`."""

    id: str
    source_id: str
    external_id: str
    name: str
    unit: str | None
    kind: str
    latest_score: LatestScore | None
    open_findings: int
    last_run_at: datetime | None


class SeriesList(BaseModel):
    """One page of series and the cursor of the next page, if any."""

    items: list[SeriesSummary]
    next_cursor: str | None


class SeriesOut(SeriesSummary):
    """The full series: metadata, latest score and counts."""

    expected_interval_ns: int | None
    physical_min: float | None
    physical_max: float | None
    operational_min: float | None
    operational_max: float | None
    resolution: float | None
    non_negative: bool | None
    asset_path: str | None
    metadata: dict[str, Any]
    n_runs: int
    created_at: datetime
    updated_at: datetime


class SeriesPatch(BaseModel):
    """Body of `PATCH /api/series/{id}`: any subset of the editable fields; null clears one."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=256)
    unit: str | None = Field(default=None, max_length=32)
    kind: SeriesKind | None = None
    expected_interval_ns: int | None = Field(default=None, gt=0)
    physical_min: FiniteFloat | None = None
    physical_max: FiniteFloat | None = None
    operational_min: FiniteFloat | None = None
    operational_max: FiniteFloat | None = None
    resolution: FiniteFloat | None = Field(default=None, gt=0)
    non_negative: bool | None = None
    asset_path: str | None = Field(default=None, max_length=512)
    metadata: dict[str, Any] | None = None


def _summary_fields(series: Series, stats: series_service.SeriesStats) -> dict[str, Any]:
    """Fields shared by the summary and the full record."""
    return {
        "id": str(series.id),
        "source_id": str(series.source_id),
        "external_id": series.external_id,
        "name": series.name,
        "unit": series.unit,
        "kind": series.kind,
        "latest_score": stats.latest_score,
        "open_findings": stats.open_findings,
        "last_run_at": stats.last_run_at,
    }


def _series_out(series: Series, stats: series_service.SeriesStats) -> SeriesOut:
    """The full record of one series."""
    return SeriesOut(
        **_summary_fields(series, stats),
        expected_interval_ns=series.expected_interval_ns,
        physical_min=series.physical_min,
        physical_max=series.physical_max,
        operational_min=series.operational_min,
        operational_max=series.operational_max,
        resolution=series.resolution,
        non_negative=series.non_negative,
        asset_path=series.asset_path,
        metadata=series.metadata_,
        n_runs=stats.n_runs,
        created_at=series.created_at,
        updated_at=series.updated_at,
    )


def _parse_series_id(series_id: str) -> uuid.UUID:
    """A malformed id is simply not found (404)."""
    try:
        return uuid.UUID(series_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="series not found") from exc


@router.get("", response_model=SeriesList)
async def list_series(
    session: Annotated[AsyncSession, Depends(get_session)],
    scope: ReadScope,
    q: Annotated[str | None, Query(max_length=256)] = None,
    source_id: str | None = None,
    kind: SeriesKind | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    cursor: str | None = None,
) -> SeriesList:
    """Series by name; `q` matches name or external id case-insensitively."""
    parsed_source: uuid.UUID | None = None
    if source_id is not None:
        try:
            parsed_source = uuid.UUID(source_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="source_id is not a UUID") from exc
    try:
        rows, next_cursor = await series_service.list_series(
            session, scope, q=q or None, source_id=parsed_source, kind=kind, limit=limit, cursor=cursor
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    stats = await series_service.series_stats(session, [s.id for s in rows])
    return SeriesList(
        items=[SeriesSummary(**_summary_fields(s, stats[s.id])) for s in rows], next_cursor=next_cursor
    )


@router.get("/{series_id}", response_model=SeriesOut)
async def get_series(
    series_id: str, session: Annotated[AsyncSession, Depends(get_session)], scope: ReadScope
) -> SeriesOut:
    """One series with its metadata, latest score and counts."""
    series = await series_service.get_series(session, scope, _parse_series_id(series_id))
    if series is None:
        raise HTTPException(status_code=404, detail="series not found")
    stats = await series_service.series_stats(session, [series.id])
    return _series_out(series, stats[series.id])


@router.patch("/{series_id}", response_model=SeriesOut)
async def patch_series(
    series_id: str,
    patch: Annotated[SeriesPatch, Body()],
    session: Annotated[AsyncSession, Depends(get_session)],
    scope: WriteScope,
) -> SeriesOut:
    """Partial update; the merged metadata is validated and 422 names the offending field."""
    changes = patch.model_dump(include=patch.model_fields_set)
    try:
        series = await series_service.patch_series(
            session, scope, _parse_series_id(series_id), changes, now=datetime.now(UTC)
        )
    except series_service.MetadataError as exc:
        raise HTTPException(
            status_code=422,
            detail=[{"loc": ["body", exc.field], "msg": exc.message, "type": "value_error"}],
        ) from exc
    if series is None:
        raise HTTPException(status_code=404, detail="series not found")
    stats = await series_service.series_stats(session, [series.id])
    return _series_out(series, stats[series.id])


class MetricPoint(BaseModel):
    """One metric point; `ts` is ns since the epoch."""

    ts: int
    value: float
    run_id: str
    check_id: str
    name: str


class MetricList(BaseModel):
    """Metric points of one series, newest first."""

    items: list[MetricPoint]


class ScoreOut(BaseModel):
    """One stored score row of a series."""

    series_id: str
    run_id: str
    layer: str
    method_version: str
    overall: float
    dimensions: dict[str, Any]
    n_findings: int
    computed_at: datetime


class ScoreList(BaseModel):
    """Score rows of one series, newest first."""

    items: list[ScoreOut]


async def _series_id_or_404(session: AsyncSession, scope: Scope, series_id: str) -> uuid.UUID:
    """Parse the series id and check it exists in the workspace; 404 otherwise."""
    try:
        parsed = uuid.UUID(series_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="series not found") from exc
    if await series_service.get_series(session, scope, parsed) is None:
        raise HTTPException(status_code=404, detail="series not found")
    return parsed


def _time_or_422(value: str | None, name: str) -> datetime | None:
    """Parse an optional RFC 3339 or epoch-ns query parameter; 422 when malformed."""
    if value is None:
        return None
    try:
        return parse_time(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"{name}: {exc}") from exc


@router.get("/{series_id}/metrics", response_model=MetricList)
async def list_metrics(
    series_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    scope: ReadScope,
    name: Annotated[str | None, Query(max_length=128)] = None,
    since: str | None = None,
    until: str | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> MetricList:
    """Metric points newest first, optionally one metric name and a `[since, until)` range."""
    parsed = await _series_id_or_404(session, scope, series_id)
    rows = await findings_service.list_metrics(
        session,
        parsed,
        name=name,
        since=_time_or_422(since, "since"),
        until=_time_or_422(until, "until"),
        limit=limit,
    )
    return MetricList(
        items=[
            MetricPoint(
                ts=datetime_to_ns(m.ts), value=m.value, run_id=str(m.run_id), check_id=m.check_id, name=m.name
            )
            for m in rows
        ]
    )


@router.get("/{series_id}/scores", response_model=ScoreList)
async def list_scores(
    series_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    scope: ReadScope,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    run_id: str | None = None,
) -> ScoreList:
    """Score rows newest first; `run_id` selects the rows one run wrote."""
    parsed = await _series_id_or_404(session, scope, series_id)
    parsed_run: uuid.UUID | None = None
    if run_id is not None:
        try:
            parsed_run = uuid.UUID(run_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="run_id is not a UUID") from exc
    rows = await findings_service.list_scores(session, parsed, limit=limit, run_id=parsed_run)
    return ScoreList(
        items=[
            ScoreOut(
                series_id=str(s.series_id),
                run_id=str(s.run_id),
                layer=s.layer,
                method_version=s.method_version,
                overall=s.overall,
                dimensions=s.dimensions,
                n_findings=s.n_findings,
                computed_at=s.computed_at,
            )
            for s in rows
        ]
    )
