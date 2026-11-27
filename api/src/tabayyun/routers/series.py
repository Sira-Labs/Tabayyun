"""Series endpoints: catalogue, metadata and partial updates (spec 004), metric points and
score history (spec 003), chart points and profile (spec 025), baselines (spec 028)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

import structlog
from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from tabayyun import core
from tabayyun.auth.deps import client_ip
from tabayyun.authz import Principal, ReadScope, Scope, WriteScope, get_principal, get_session
from tabayyun.db import for_org
from tabayyun.db.models import Score, Series, SeriesBaseline
from tabayyun.routers.windows import layer_or_422, read_layer_or_503, version_or_422, window_or_422
from tabayyun.services import baselines as baselines_service
from tabayyun.services import findings as findings_service
from tabayyun.services import series as series_service
from tabayyun.services import series_chart
from tabayyun.services.admin import audit
from tabayyun.services.cache import CacheError
from tabayyun.services.timeconv import datetime_to_ns, parse_time

router = APIRouter(prefix="/api/series", tags=["series"])
log = structlog.get_logger()

SeriesKind = core.SeriesKind
FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]


class LatestScore(BaseModel):
    """The newest raw-layer score of a series."""

    overall: float
    computed_at: datetime


class RawScore(BaseModel):
    """The raw layer scored like the corrected one beside it."""

    overall: float | None


class CorrectedScore(BaseModel):
    """The newest score of a series' corrected layer (spec 036): its number, the corrected
    version that was scored, and the raw layer scored with the same checks and window."""

    overall: float
    computed_at: datetime
    version: int | None
    raw: RawScore


class SeriesSummary(BaseModel):
    """One row of `GET /api/series`."""

    id: str
    source_id: str
    external_id: str
    name: str
    unit: str | None
    kind: str
    latest_score: LatestScore | None
    corrected_score: CorrectedScore | None
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
        "corrected_score": stats.corrected_score,
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
    unit: Annotated[str | None, Query(max_length=32)] = None,
    score_max: Annotated[float | None, Query(ge=0, le=100)] = None,
) -> SeriesList:
    """Series by name; `q` matches name or external id case-insensitively, `unit` the unit,
    `score_max` the latest overall score at or below it (spec 024)."""
    parsed_source: uuid.UUID | None = None
    if source_id is not None:
        try:
            parsed_source = uuid.UUID(source_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="source_id is not a UUID") from exc
    try:
        rows, next_cursor = await series_service.list_series(
            session,
            scope,
            q=q or None,
            source_id=parsed_source,
            kind=kind,
            limit=limit,
            cursor=cursor,
            unit=unit or None,
            score_max=score_max,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    stats = await series_service.series_stats(session, [s.id for s in rows])
    return SeriesList(
        items=[SeriesSummary(**_summary_fields(s, stats[s.id])) for s in rows], next_cursor=next_cursor
    )


class UnitCount(BaseModel):
    """A unit and how many series use it."""

    unit: str
    n: int


class UnitList(BaseModel):
    """The workspace's units, most used first (spec 024)."""

    items: list[UnitCount]


@router.get("/units", response_model=UnitList)
async def list_units(session: Annotated[AsyncSession, Depends(get_session)], scope: ReadScope) -> UnitList:
    """Distinct units of the workspace's series, for the catalogue's unit filter."""
    rows = await series_service.list_units(session, scope)
    return UnitList(items=[UnitCount(unit=unit, n=n) for unit, n in rows])


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
    # For a row of the layer `corrected` (spec 036): the raw layer scored the same way,
    # `{overall, dimensions, n_findings}`, and the corrected version that was scored.
    baseline: dict[str, Any] | None
    version: int | None


class ScoreList(BaseModel):
    """Score rows of one series, newest first."""

    items: list[ScoreOut]


def _score_out(score: Score) -> ScoreOut:
    """One score row; a corrected one with its baseline and, apart from it, its version."""
    base = dict(score.baseline) if score.baseline is not None else None
    version = base.pop("version", None) if base is not None else None
    return ScoreOut(
        series_id=str(score.series_id),
        run_id=str(score.run_id),
        layer=score.layer,
        method_version=score.method_version,
        overall=score.overall,
        dimensions=score.dimensions,
        n_findings=score.n_findings,
        computed_at=score.computed_at,
        baseline=base,
        version=version,
    )


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
    layer: Literal["raw", "corrected"] | None = None,
) -> ScoreList:
    """Score rows newest first; `run_id` selects the rows one run wrote, `layer` one layer's."""
    parsed = await _series_id_or_404(session, scope, series_id)
    parsed_run: uuid.UUID | None = None
    if run_id is not None:
        try:
            parsed_run = uuid.UUID(run_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="run_id is not a UUID") from exc
    rows = await findings_service.list_scores(session, parsed, limit=limit, run_id=parsed_run, layer=layer)
    return ScoreList(items=[_score_out(s) for s in rows])


# Chart and profile (spec 025)


class TimeWindow(BaseModel):
    """A half-open window `[from, to)`, ns since the epoch as strings."""

    from_: str = Field(alias="from")
    to: str


class ExtentOut(BaseModel):
    """What the cache holds of a series: the earliest start and latest end of its coverage."""

    start: str
    end: str


class QualityRunOut(BaseModel):
    """A run of one non-good quality class at the chart's bin resolution."""

    start: str
    end: str
    quality: str = Field(description="uncertain, bad or estimated")


class ChartOut(BaseModel):
    """M4 points of one series over a window; `values` holds null where the line breaks."""

    window: TimeWindow | None
    extent: ExtentOut | None
    layer: str
    width_px: int
    n_raw: int
    ts: list[str]
    values: list[float | None]
    quality: list[QualityRunOut]
    # Only in the answer to a request that names a layer other than raw (spec 036): the
    # version that was read, null for a series without an approved correction.
    layer_version: int | None = None


class BandOut(BaseModel):
    """The operating band and where it comes from."""

    lo: float
    hi: float
    source: Literal["metadata", "profile"]


class ProfileOut(BaseModel):
    """The profile of a window without bad-quality samples, and the band derived from it."""

    window: TimeWindow | None
    profile: dict[str, Any] | None
    quality_counts: dict[str, int]
    band: BandOut | None
    # Only in the answer to a request that names a layer other than raw (spec 036).
    layer: str | None = None
    layer_version: int | None = None


LayerQuery = Annotated[
    str | None, Query(description="raw (the default), corrected@latest or corrected@v{n}", max_length=64)
]


async def _series_or_404(session: AsyncSession, scope: Scope, series_id: str) -> Series:
    """The series in the caller's workspace; 404 otherwise."""
    series = await series_service.get_series(session, scope, _parse_series_id(series_id))
    if series is None:
        raise HTTPException(status_code=404, detail="series not found")
    return series


def _window_out(window: tuple[int, int] | None) -> TimeWindow | None:
    return TimeWindow.model_validate({"from": str(window[0]), "to": str(window[1])}) if window else None


@router.get("/{series_id}/chart", response_model=ChartOut, response_model_exclude_unset=True)
async def get_chart(
    series_id: str,
    request: Request,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
    scope: ReadScope,
    from_: Annotated[str | None, Query(alias="from")] = None,
    to: str | None = None,
    width_px: Annotated[int, Query(ge=100, le=4000)] = 1200,
    layer: LayerQuery = None,
) -> ChartOut | Response:
    """M4-downsampled points of a window (default: the whole extent, or its latest week) of the
    raw layer, or of the corrected layer when `layer` names it (spec 036).

    422 `window_too_large` when the window holds more rows than `TABAYYUN_CHART_MAX_ROWS`,
    422 `unknown_layer_version` for a `corrected@v{n}` that was never approved.
    """
    ref = layer_or_422(layer)
    series = await _series_or_404(session, scope, series_id)
    requested = window_or_422(from_, to)
    read_version, layer_mark = await version_or_422(session, series, ref)
    # Windows, the extent and the row limit are the raw layer's coverage for every layer.
    ext = await series_chart.extent(session, series.id)
    window = requested or (series_chart.default_chart_window(ext) if ext else None)
    if window is not None:
        try:
            series_chart.check_size(ext, *window, request.app.state.settings.chart_max_rows)
        except series_chart.WindowTooLargeError as exc:
            raise HTTPException(status_code=422, detail="window_too_large") from exc
    version = ext.version if ext else None
    # A raw answer's tag is what it always was; another layer's also names the version read
    # and the state of the layer up to it, so an approval changes it.
    parts: tuple[object, ...] = () if ref.is_raw else (read_version, layer_mark)
    tag = series_chart.etag(
        "chart", series.id, ref.name, window, width_px, version, series.updated_at, *parts
    )
    headers = {"ETag": tag, "Cache-Control": "private, no-cache"}
    if request.headers.get("if-none-match") == tag:
        return Response(status_code=304, headers=headers)
    batch = await read_layer_or_503(request.app.state.run_cache, session, series, window, read_version)
    points = (
        await series_chart.chart_points(batch, *window, width_px, series.expected_interval_ns)
        if window
        else series_chart.ChartPoints(ts=[], values=[], quality=[], n_raw=0)
    )
    response.headers.update(headers)
    return ChartOut(
        **({} if ref.is_raw else {"layer_version": read_version}),
        window=_window_out(window),
        extent=ExtentOut(start=str(ext.start_ns), end=str(ext.end_ns)) if ext else None,
        layer=ref.name,
        width_px=width_px,
        n_raw=points.n_raw,
        ts=[str(t) for t in points.ts],
        values=points.values,
        quality=[QualityRunOut(start=str(s), end=str(e), quality=q) for s, e, q in points.quality],
    )


@router.get("/{series_id}/profile", response_model=ProfileOut, response_model_exclude_unset=True)
async def get_profile(
    series_id: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    scope: ReadScope,
    from_: Annotated[str | None, Query(alias="from")] = None,
    to: str | None = None,
    layer: LayerQuery = None,
) -> ProfileOut:
    """The profile of a window without bad samples (default: the trailing 28 days) and the
    band, of the raw layer or of the corrected layer when `layer` names it (spec 036)."""
    ref = layer_or_422(layer)
    series = await _series_or_404(session, scope, series_id)
    requested = window_or_422(from_, to)
    read_version, _ = await version_or_422(session, series, ref)
    ext = await series_chart.extent(session, series.id)
    window = requested or (series_chart.default_profile_window(ext) if ext else None)
    if window is not None:
        try:
            series_chart.check_size(ext, *window, request.app.state.settings.chart_max_rows)
        except series_chart.WindowTooLargeError as exc:
            raise HTTPException(status_code=422, detail="window_too_large") from exc
    batch = await read_layer_or_503(request.app.state.run_cache, session, series, window, read_version)
    profile = await series_chart.window_profile(batch, str(series.id))
    band = series_chart.band(series, profile)
    named: dict[str, Any] = {} if ref.is_raw else {"layer": ref.name, "layer_version": read_version}
    return ProfileOut(
        **named,
        window=_window_out(window),
        profile=profile,
        quality_counts=series_chart.quality_counts(batch),
        band=BandOut(**band) if band else None,
    )


# Baselines (spec 028)


class ThresholdOut(BaseModel):
    """A threshold a check learns from the baseline, with the check's default parameters."""

    check_id: str
    param: str
    value: float | None
    basis: str


class ExcludedOut(BaseModel):
    """What a baseline left out: sample counts and the finding windows, ns as strings."""

    bad: int
    findings: int
    windows: list[tuple[str, str]]


class BaselineOut(BaseModel):
    """One version of a series' baseline."""

    version: int
    created_at: datetime
    trigger: Literal["schedule", "manual"]
    window: TimeWindow
    n_samples: int
    excluded: ExcludedOut
    thresholds: list[ThresholdOut]
    band: BandOut | None
    profile: dict[str, Any]


class BaselineList(BaseModel):
    """A series' baselines, newest first; the first is the current one."""

    items: list[BaselineOut]
    next_cursor: None = None


class BaselineQueued(BaseModel):
    """A baseline computation was started."""

    queued: bool = True


def _baseline_out(row: SeriesBaseline, series: Series) -> BaselineOut:
    excluded = row.excluded or {}
    band = series_chart.band(series, row.profile)
    return BaselineOut(
        version=row.version,
        created_at=row.created_at,
        trigger=row.trigger,  # type: ignore[arg-type]  # the table's check constraint
        window=TimeWindow.model_validate(
            {"from": str(datetime_to_ns(row.window_start)), "to": str(datetime_to_ns(row.window_end))}
        ),
        n_samples=row.n_samples,
        excluded=ExcludedOut(
            bad=int(excluded.get("bad", 0)),
            findings=int(excluded.get("findings", 0)),
            windows=[(str(s), str(e)) for s, e in excluded.get("windows", [])],
        ),
        thresholds=[ThresholdOut(**t) for t in baselines_service.learned_thresholds(row.profile, series)],
        band=BandOut(**band) if band else None,
        profile=row.profile,
    )


@router.get("/{series_id}/baselines", response_model=BaselineList)
async def list_baselines(
    series_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    scope: ReadScope,
    limit: Annotated[int, Query(ge=1, le=baselines_service.KEEP_VERSIONS)] = baselines_service.KEEP_VERSIONS,
) -> BaselineList:
    """The series' baselines, newest first, with the thresholds each one gives the checks."""
    series = await _series_or_404(session, scope, series_id)
    rows = await baselines_service.list_baselines(session, series.id, limit)
    return BaselineList(items=[_baseline_out(row, series) for row in rows])


async def _refresh_inline(app_state: Any, org_id: uuid.UUID, series_id: uuid.UUID) -> None:
    """Compute the baseline after the response (inline jobs); a failure is logged, as in a job."""
    try:
        await baselines_service.compute_baseline(
            for_org(app_state.session_factory, org_id),
            app_state.run_cache,
            series_id,
            trigger="manual",
            max_rows=app_state.settings.baseline_max_rows,
        )
    except CacheError as exc:
        log.warning("baseline.cache_read_failed", series_id=str(series_id), error=str(exc))


@router.post("/{series_id}/baselines", response_model=BaselineQueued, status_code=202)
async def refresh_baseline(
    series_id: str,
    request: Request,
    background: BackgroundTasks,
    session: Annotated[AsyncSession, Depends(get_session)],
    scope: WriteScope,
    principal: Annotated[Principal, Depends(get_principal)],
) -> BaselineQueued:
    """Compute the series' baseline now. 409 `no_data` when the cache holds nothing of it."""
    series = await _series_or_404(session, scope, series_id)
    if await series_chart.extent(session, series.id) is None:
        raise HTTPException(status_code=409, detail="no_data")
    await audit.record(
        session,
        audit.Actor(
            user_id=principal.user_id,
            org_id=principal.org_id,
            ip_address=client_ip(request),
            user_agent=request.headers.get("user-agent"),
        ),
        "series.baseline_refresh_requested",
        "series",
        series.id,
        workspace_id=scope.workspace_id,
        details={"name": series.name},
    )
    if request.app.state.settings.inline_jobs:
        await session.commit()
        background.add_task(_refresh_inline, request.app.state, scope.org_id, series.id)
    else:
        await baselines_service.enqueue(session, series.id, scope.org_id, "manual")
    return BaselineQueued()
