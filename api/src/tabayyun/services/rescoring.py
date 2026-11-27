"""The corrected layer's score (spec 036): after an approval, the series' corrected layer is
scored with the checks, the window and the `now` of its newest raw score's run, and the raw
layer is scored the same way beside it, so that the two numbers compare like with like.

One `scores` row of the layer `corrected` holds both (the raw result in `baseline`), and the
`score_days` row of that layer follows it. Nothing of the raw layer is written here: no raw
score, no finding and no metric (behaviour 6). When there is nothing to score, no row is
written and the reason is logged; the approval stands either way.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from typing import Any, Literal

import pyarrow as pa
import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tabayyun import core
from tabayyun.authz.scope import Scope
from tabayyun.db.models import Run, Score, Series
from tabayyun.jobs.names import RESCORE_CORRECTED_TASK, RUNS_QUEUE
from tabayyun.services import baselines as baselines_service
from tabayyun.services import corrections, dataset_runs, series_chart
from tabayyun.services import overview as overview_service
from tabayyun.services import series as series_service
from tabayyun.services.cache import CacheError, RunCache
from tabayyun.services.runs import DEFER_JOB_SQL
from tabayyun.services.timeconv import datetime_to_ns, to_core_ns

log = structlog.get_logger()

CORRECTED = "corrected"
RAW = "raw"
SERIES_SCOPE = "series"

Status = Literal[
    "scored", "never_scored", "not_corrected", "too_large", "no_data", "cache_error", "check_error"
]


@dataclass(frozen=True)
class Outcome:
    """How a re-scoring ended: `scored` with the row's numbers, or why nothing was written."""

    status: Status
    version: int | None = None
    overall: float | None = None
    raw_overall: float | None = None


@dataclass(frozen=True)
class _Inputs:
    """What one re-scoring reads and runs, taken in one transaction."""

    series: Series
    run_id: uuid.UUID
    start_ns: int
    end_ns: int
    now_ns: int
    version: int
    layers: list[corrections.Layered]
    meta: core.SeriesMetaIn
    configs: list[dict[str, Any]] | None
    left_out: list[str]
    profile: dict[str, Any] | None


async def enqueue(session: AsyncSession, series_id: uuid.UUID, org_id: uuid.UUID) -> None:
    """Queue the re-scoring of a series in the caller's transaction, so the job exists exactly
    when the approval that asks for it does."""
    args = {"series_id": str(series_id), "org_id": str(org_id)}
    await session.execute(
        DEFER_JOB_SQL, {"queue": RUNS_QUEUE, "task": RESCORE_CORRECTED_TASK, "args": json.dumps(args)}
    )


def single_series_configs(
    checks: list[dict[str, Any]] | None, *, dataset_run: bool
) -> tuple[list[dict[str, Any]] | None, list[str]]:
    """The run's checks that look at one series, as the core's configs (None is the default
    set), and the ids of the checks across series that are left out."""
    scopes = {c["id"]: c.get("scope") for c in core.check_catalogue()}
    configs = dataset_runs.run_configs(checks)
    if configs is None:
        # The default set runs every check; those across series only ran on a dataset's groups.
        across = sorted(i for i, scope in scopes.items() if scope != SERIES_SCOPE) if dataset_run else []
        return None, across
    kept = [c for c in configs if scopes.get(c["id"]) == SERIES_SCOPE]
    return kept, sorted({c["id"] for c in configs if scopes.get(c["id"]) != SERIES_SCOPE})


def _window(run: Run) -> tuple[int, int] | None:
    """The run's window in the core's ns: the exact bounds of its stats, else its columns."""
    window = (run.stats or {}).get("window") or {}
    if "start" in window and "end" in window:
        return int(window["start"]), int(window["end"])
    if run.window_start is None or run.window_end is None:
        return None
    return to_core_ns(datetime_to_ns(run.window_start)), to_core_ns(datetime_to_ns(run.window_end))


async def _inputs(session: AsyncSession, series_id: uuid.UUID, max_rows: int) -> _Inputs | Status:
    """The re-scoring's inputs, or why there is nothing to score."""
    series = await session.get(Series, series_id)
    if series is None:
        return "no_data"
    newest = await session.scalar(
        select(Score)
        .where(Score.series_id == series_id, Score.layer == RAW)
        .order_by(Score.computed_at.desc())
        .limit(1)
    )
    run = None if newest is None else await session.get(Run, newest.run_id)
    window = None if run is None else _window(run)
    if run is None or window is None:
        return "never_scored"
    version = await corrections.latest_approved(session, series_id)
    if version is None:
        return "not_corrected"
    try:
        series_chart.check_size(await series_chart.extent(session, series_id), *window, max_rows)
    except series_chart.WindowTooLargeError:
        return "too_large"
    try:
        meta = series_service.meta_for_core(str(series_id), series_service.merged_for_run(series, {}))
    except series_service.MetadataError:
        return "check_error"
    configs, left_out = single_series_configs(run.checks, dataset_run=run.dataset_id is not None)
    stored = (await baselines_service.current_for(session, [series_id])).get(series_id)
    layers = await corrections.approved_layers(session, series_id, version, *window)
    session.expunge(series)
    return _Inputs(
        series=series,
        run_id=run.id,
        start_ns=window[0],
        end_ns=window[1],
        now_ns=run.now_ns if run.now_ns is not None else window[1],
        version=version,
        layers=layers,
        meta=meta,
        configs=configs,
        left_out=left_out,
        profile=None if stored is None else stored.profile,
    )


def _score(inputs: _Inputs, batch: pa.RecordBatch) -> core.Score:
    """The core's score of one layer's rows, checked as a dataset run checks a series; blocking."""
    sid = str(inputs.series.id)
    report = core.run_checks_multi(
        {sid: batch},
        {sid: inputs.meta},
        [],
        window=(inputs.start_ns, inputs.end_ns),
        now_ns=inputs.now_ns,
        configs=inputs.configs,
        quality_col="quality",
        # A series without a stored baseline is judged against its own data in the window.
        profiles=None if inputs.profile is None else {sid: inputs.profile},
    )
    return report.reports[sid].score


async def _scores(cache: RunCache, inputs: _Inputs, max_rows: int) -> tuple[core.Score, core.Score] | Status:
    """The corrected layer's score and the raw layer's, or why there is none.

    Raises:
        CacheError: the store cannot be opened or read.
    """
    window = (inputs.start_ns, inputs.end_ns)
    raw = await corrections.read_composed(cache, inputs.series, [], *window)
    # Coverage spreads a range's rows evenly; the limit is on what is read.
    if raw is not None and raw.num_rows > max_rows:
        return "too_large"
    parts = await corrections.read_overlays(cache, inputs.series, inputs.layers, *window)
    corrected = corrections.overlay(raw, parts)
    if raw is None or corrected is None:
        return "no_data"
    if corrected.num_rows > max_rows:
        return "too_large"
    try:
        return await asyncio.to_thread(_score, inputs, corrected), await asyncio.to_thread(
            _score, inputs, raw
        )
    # As in `corrections._run`: the core refuses with ValueError, and any other ordinary error
    # of it (a panic surfacing as RuntimeError) must end this re-scoring, not the worker's job
    # or, with inline jobs, the request that approved the correction.
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "rescore.check_failed", series_id=str(inputs.series.id), error=corrections.first_line(exc)
        )
        return "check_error"


async def _store(
    factory: async_sessionmaker[AsyncSession], inputs: _Inputs, corrected: core.Score, raw: core.Score
) -> None:
    """One `scores` row of the layer `corrected` with the raw result as its baseline, and the
    day's `score_days` row of that layer, in one transaction."""
    series = inputs.series
    async with factory() as session, session.begin():
        # The database's clock, as for approvals: a later approval's row is the newer one.
        computed_at = (await session.execute(select(func.clock_timestamp()))).scalar_one()
        session.add(
            Score(
                series_id=series.id,
                org_id=series.org_id,
                run_id=inputs.run_id,
                layer=CORRECTED,
                method_version=corrected.method_version,
                overall=corrected.overall,
                dimensions=corrected.dimensions,
                n_findings=corrected.n_findings,
                computed_at=computed_at,
                baseline={
                    "overall": raw.overall,
                    "dimensions": raw.dimensions,
                    "n_findings": raw.n_findings,
                    "version": inputs.version,
                },
            )
        )
        await overview_service.record_score_day(
            session,
            Scope(org_id=series.org_id, workspace_id=series.workspace_id),
            series_id=series.id,
            overall=corrected.overall,
            dimensions=corrected.dimensions,
            n_findings=corrected.n_findings,
            computed_at=computed_at,
            layer=CORRECTED,
        )


async def rescore(
    factory: async_sessionmaker[AsyncSession], series_id: uuid.UUID, cache: RunCache, max_rows: int
) -> Outcome:
    """Score the series' corrected layer at its highest approved version, and its raw layer
    the same way, with the checks, window and `now` of its newest raw score's run; store the
    pair as one `corrected` score. `factory` is the org's.

    Nothing is written, and the status says why, for a series that was never scored or has no
    approved correction, a window over `max_rows`, a window without rows, a cache that cannot
    be read and checks that cannot run.
    """
    run_log = log.bind(series_id=str(series_id))
    async with factory() as session, session.begin():
        inputs = await _inputs(session, series_id, max_rows)
    if not isinstance(inputs, _Inputs):
        run_log.info("rescore.skipped", reason=inputs)
        return Outcome(status=inputs)
    try:
        scores = await _scores(cache, inputs, max_rows)
    except CacheError as exc:
        # The store's own message goes to the log only: it names hosts and buckets.
        run_log.warning("rescore.skipped", reason="cache_error", error=str(exc))
        return Outcome(status="cache_error", version=inputs.version)
    if not isinstance(scores, tuple):
        run_log.info("rescore.skipped", reason=scores, version=inputs.version)
        return Outcome(status=scores, version=inputs.version)
    corrected, raw = scores
    await _store(factory, inputs, corrected, raw)
    run_log.info(
        "rescore.scored",
        version=inputs.version,
        run_id=str(inputs.run_id),
        overall=corrected.overall,
        raw_overall=raw.overall,
        left_out=inputs.left_out,
    )
    return Outcome(
        status="scored", version=inputs.version, overall=corrected.overall, raw_overall=raw.overall
    )
