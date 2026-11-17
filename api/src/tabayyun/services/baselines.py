"""Baseline profiles (spec 028): a series' usual behaviour, stored and versioned.

A baseline is the core's profile of the trailing 28 days of what the cache holds of a series,
without bad-quality samples and without the windows of its open critical findings. Dataset
runs judge a series against its newest baseline; the scheduler refreshes each about once a day.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

import pyarrow as pa
import pyarrow.compute as pc
import structlog
import tabayyun_core
from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tabayyun import core
from tabayyun.db.models import Finding, Series, SeriesBaseline
from tabayyun.jobs.names import MAINTENANCE_QUEUE, PROFILE_SERIES_TASK
from tabayyun.services import series_chart
from tabayyun.services.cache import RunCache
from tabayyun.services.fetches import DEFER_LOCKED_SQL
from tabayyun.services.timeconv import datetime_to_ns, ns_to_datetime, ns_to_datetime_ceil

log = structlog.get_logger()

DAY_NS = 86_400 * 10**9
# Fewer usable samples than this teach nothing: no baseline is stored.
MIN_SAMPLES = 50
KEEP_VERSIONS = 30
# Series one scheduler tick queues; with an hourly tick, 4800 series are refreshed a day.
CLAIM_LIMIT = 200
# Every baseline job shares this lock, so they run one after another on one worker slot.
JOB_LOCK = "baselines"
CLAIM_SQL = text("SELECT series_id, org_id FROM tabayyun_claim_series_for_baseline(:now, :limit)")

Trigger = Literal["schedule", "manual"]
# A computation whose inputs went stale this often is given up; the next refresh tries again.
MAX_ATTEMPTS = 3

Status = Literal["stored", "unchanged", "gone", "no_data", "too_large", "too_few", "superseded"]


@dataclass(frozen=True)
class Outcome:
    """How one baseline computation ended; `version` is set when a version was stored."""

    status: Status
    version: int | None = None


@dataclass(frozen=True)
class _Inputs:
    """What the computation reads, loaded in one transaction."""

    series: Series
    start_ns: int
    end_ns: int
    windows: list[tuple[int, int]]
    # The current version when the inputs were read (0: none), to notice a store in between.
    seen_version: int


def fit_window(ext: series_chart.Extent, max_rows: int) -> tuple[int, int] | None:
    """The trailing 28 days of the extent, halved until the coverage estimate fits `max_rows`;
    None when not even one day fits."""
    days = series_chart.PROFILE_WINDOW_NS // DAY_NS
    while days >= 1:
        start = max(ext.start_ns, ext.end_ns - days * DAY_NS)
        if series_chart.estimate_rows(ext, start, ext.end_ns) <= max_rows:
            return start, ext.end_ns
        days //= 2
    return None


def usable(batch: pa.RecordBatch, windows: Sequence[tuple[int, int]]) -> tuple[pa.RecordBatch, int, int]:
    """The batch without bad-quality samples, without samples that hold no number and without
    samples inside `windows` (`[start_ns, end_ns)`), and how many the first and the last rule
    removed (bad quality counts first). Samples without a number teach nothing and are in
    neither count; without them the minimum is counted on what the profile really uses."""
    not_bad = batch.filter(pc.not_equal(batch.column("quality"), series_chart.QUALITY_CODES["bad"]))
    n_bad = batch.num_rows - not_bad.num_rows
    good = not_bad.filter(pc.fill_null(pc.is_finite(not_bad.column("value")), False))
    if not windows or good.num_rows == 0:
        return good, n_bad, 0
    ts = good.column("ts").cast(pa.int64())
    keep = pa.array([True] * good.num_rows)
    for start, end in windows:
        inside = pc.and_(pc.greater_equal(ts, start), pc.less(ts, end))
        keep = pc.and_(keep, pc.invert(inside))
    kept = good.filter(keep)
    return kept, n_bad, good.num_rows - kept.num_rows


async def _open_critical_windows(
    session: AsyncSession, series_id: uuid.UUID, start_ns: int, end_ns: int
) -> list[tuple[int, int]]:
    """Windows of the series' open critical findings that touch `[start_ns, end_ns)`, in ns."""
    rows = await session.execute(
        select(Finding.window_start, Finding.window_end)
        .where(
            Finding.series_id == series_id,
            Finding.status == "open",
            Finding.severity == "critical",
            Finding.window_start < ns_to_datetime_ceil(end_ns),
            # `>=`: a finding at one instant has an empty window, also at the window's start.
            Finding.window_end >= ns_to_datetime(start_ns),
        )
        .order_by(Finding.window_start, Finding.window_end)
    )
    # The empty window of a finding at one instant leaves out that sample.
    windows = [(s, max(e, s + 1)) for s, e in ((datetime_to_ns(a), datetime_to_ns(b)) for a, b in rows)]
    return [(s, e) for s, e in windows if e > start_ns]


async def _inputs(session: AsyncSession, series_id: uuid.UUID, max_rows: int) -> _Inputs | Outcome:
    series = await session.get(Series, series_id)
    if series is None:
        return Outcome("gone")
    # Read first: a store that commits while the rest is being read (each statement sees what
    # is committed by then) must leave this version behind, so the stale result is not stored.
    current = await latest(session, series.id)
    ext = await series_chart.extent(session, series.id)
    if ext is None:
        return Outcome("no_data")
    window = fit_window(ext, max_rows)
    if window is None:
        return Outcome("too_large")
    windows = await _open_critical_windows(session, series.id, *window)
    session.expunge(series)
    return _Inputs(
        series=series,
        start_ns=window[0],
        end_ns=window[1],
        windows=windows,
        seen_version=0 if current is None else current.version,
    )


async def compute_baseline(
    factory: async_sessionmaker[AsyncSession],
    cache: RunCache,
    series_id: uuid.UUID,
    *,
    trigger: Trigger,
    max_rows: int,
) -> Outcome:
    """Compute the series' baseline and store it as a new version when it differs from the
    current one. `factory` is the org's.

    Computations of one series may overlap (a queued job and a refresh from the page). A
    result is stored only if no version was stored since its inputs were read; otherwise it is
    computed again from the newer state, so an older computation never lands on top of a
    newer one.

    Raises:
        CacheError: the store cannot be read; nothing is stored.
    """
    job_log = log.bind(series_id=str(series_id), trigger=trigger)
    for _ in range(MAX_ATTEMPTS):
        outcome = await _attempt(factory, cache, series_id, trigger=trigger, max_rows=max_rows)
        if outcome is not None:
            if outcome.status == "stored":
                job_log.info("baseline.stored", version=outcome.version)
            else:
                job_log.info("baseline.skipped", reason=outcome.status, version=outcome.version)
            return outcome
    job_log.warning("baseline.skipped", reason="superseded", attempts=MAX_ATTEMPTS)
    return Outcome("superseded")


async def _attempt(
    factory: async_sessionmaker[AsyncSession],
    cache: RunCache,
    series_id: uuid.UUID,
    *,
    trigger: Trigger,
    max_rows: int,
) -> Outcome | None:
    """One computation from freshly read inputs; None when a version was stored meanwhile."""
    async with factory() as session, session.begin():
        inputs = await _inputs(session, series_id, max_rows)
    if isinstance(inputs, Outcome):
        return inputs
    batch = await series_chart.read_window(cache, inputs.series, inputs.start_ns, inputs.end_ns)
    if batch is None:
        return Outcome("no_data")
    # The window was sized from coverage, which spreads a range's rows evenly; a dense burst
    # can hold more. The limit is on what is profiled, so it is checked on the rows themselves.
    if batch.num_rows > max_rows:
        return Outcome("too_large")
    kept, n_bad, n_findings = usable(batch, inputs.windows)
    if kept.num_rows < MIN_SAMPLES:
        return Outcome("too_few")
    profile: dict[str, Any] = await asyncio.to_thread(
        tabayyun_core.profile, kept, {"id": str(series_id)}, quality_col="quality"
    )
    excluded = {"bad": n_bad, "findings": n_findings, "windows": [list(w) for w in inputs.windows]}
    window_start, window_end = ns_to_datetime(inputs.start_ns), ns_to_datetime_ceil(inputs.end_ns)
    async with factory() as session, session.begin():
        # The series row is the lock that orders the stores of overlapping computations.
        series = await session.get(Series, series_id, with_for_update=True)
        if series is None:
            return Outcome("gone")
        current = await latest(session, series_id)
        if (0 if current is None else current.version) != inputs.seen_version:
            return None
        if (
            current is not None
            and current.profile == profile
            and current.excluded == excluded
            and (current.window_start, current.window_end) == (window_start, window_end)
        ):
            return Outcome("unchanged", current.version)
        version = inputs.seen_version + 1
        session.add(
            SeriesBaseline(
                org_id=series.org_id,
                workspace_id=series.workspace_id,
                series_id=series_id,
                version=version,
                window_start=window_start,
                window_end=window_end,
                n_samples=kept.num_rows,
                excluded=excluded,
                profile=profile,
                trigger=trigger,
            )
        )
        await session.execute(
            delete(SeriesBaseline).where(
                SeriesBaseline.series_id == series_id, SeriesBaseline.version <= version - KEEP_VERSIONS
            )
        )
    return Outcome("stored", version)


async def latest(session: AsyncSession, series_id: uuid.UUID) -> SeriesBaseline | None:
    """The series' current baseline."""
    stmt = (
        select(SeriesBaseline)
        .where(SeriesBaseline.series_id == series_id)
        .order_by(SeriesBaseline.version.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_baselines(session: AsyncSession, series_id: uuid.UUID, limit: int) -> list[SeriesBaseline]:
    """The series' baselines, newest first."""
    stmt = (
        select(SeriesBaseline)
        .where(SeriesBaseline.series_id == series_id)
        .order_by(SeriesBaseline.version.desc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars())


async def current_for(
    session: AsyncSession, series_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, SeriesBaseline]:
    """The current baseline of each of the series that has one."""
    if not series_ids:
        return {}
    stmt = (
        select(SeriesBaseline)
        .where(SeriesBaseline.series_id.in_(series_ids))
        .ext(distinct_on(SeriesBaseline.series_id))
        .order_by(SeriesBaseline.series_id, SeriesBaseline.version.desc())
    )
    return {b.series_id: b for b in (await session.execute(stmt)).scalars()}


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _rate_limit(
    profile: dict[str, Any], series: Series, defaults: dict[str, Any]
) -> tuple[float | None, str]:
    rate, factor = _number(profile.get("rate_p999")), _number(defaults.get("factor")) or 0.0
    if rate is None or rate <= 0:
        return None, "the baseline holds no change between samples to learn a rate from"
    return factor * rate, f"{factor:g} × the 99.9th percentile rate ({rate:.6g} per second)"


def _spike_floor(
    profile: dict[str, Any], series: Series, defaults: dict[str, Any]
) -> tuple[float | None, str]:
    resolution = series.resolution if series.resolution is not None else _number(profile.get("resolution"))
    noise = _number(profile.get("noise_mad"))
    if resolution is None:
        # A constant baseline has no step to read a resolution from. The check then reads it
        # from the window it is checking, which this page cannot know: no number is shown.
        known = "" if noise is None else f" (noise floor {noise:.6g})"
        return None, f"no resolution in the baseline: the check reads it from the window it checks{known}"
    floor = max(resolution, noise or 0.0)
    return (
        floor,
        f"the larger of the resolution ({resolution:.6g}) and the noise floor ({(noise or 0.0):.6g})",
    )


# How each parameter the catalogue marks as learned from one series' baseline gets its value.
# They restate what the check does with the profile; a parameter without an entry here is
# listed with the catalogue's note and no value.
_LEARNED = {
    ("tby.rate_of_change", "max_rate"): _rate_limit,
    ("tby.spikes", "min_scale"): _spike_floor,
}


def learned_thresholds(profile: dict[str, Any], series: Series) -> list[dict[str, Any]]:
    """The thresholds this profile gives the checks, with the checks' default parameters."""
    out = []
    for check in core.check_catalogue():
        if check["scope"] != "series":
            continue
        for param, auto in check["auto"].items():
            if auto["source"] != "baseline":
                continue
            learn = _LEARNED.get((check["id"], param))
            value, basis = learn(profile, series, check["defaults"]) if learn else (None, auto["note"])
            out.append({"check_id": check["id"], "param": param, "value": value, "basis": basis})
    return out


# Queueing


async def enqueue(session: AsyncSession, series_id: uuid.UUID, org_id: uuid.UUID, trigger: Trigger) -> None:
    """Defer one `profile_series` job in the caller's transaction, under the shared lock."""
    args = {"series_id": str(series_id), "org_id": str(org_id), "trigger": trigger}
    await session.execute(
        DEFER_LOCKED_SQL,
        {"queue": MAINTENANCE_QUEUE, "task": PROFILE_SERIES_TASK, "lock": JOB_LOCK, "args": json.dumps(args)},
    )


async def queue_due(
    factory: async_sessionmaker[AsyncSession], now: datetime, limit: int = CLAIM_LIMIT
) -> int:
    """One scheduler tick: claim the series that are due, in every org, and queue their jobs.
    The claim and the jobs commit together. Returns how many were queued."""
    async with factory() as session, session.begin():
        claimed = (await session.execute(CLAIM_SQL, {"now": now, "limit": limit})).all()
        for row in claimed:
            await enqueue(session, row.series_id, row.org_id, "schedule")
    return len(claimed)
