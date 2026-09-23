"""Dataset runs (spec 008): check a dataset's cached series over its window.

`POST /api/runs {dataset_id}` resolves the dataset's window against "now", stores it in
`stats.window` and queues a run with trigger `manual`. The worker then:

1. reads every series of the dataset from the Parquet cache (spec 006) and notes the parts
   of the window the cache does not cover (`stats.missing`, from the coverage rows);
2. skips series without cached rows (`stats.series_skipped`) and fails the run when none has
   any;
3. runs the single-series checks on every series and the cross-series checks on every group
   whose members are all in the dataset and have data; other groups are listed in
   `stats.groups_skipped`;
4. persists scores and findings per series like an upload run (spec 003), in one transaction
   that takes the per-series locks in series-id order, so two dataset runs sharing series
   cannot deadlock.

A cache read failure fails the run (unlike an upload's cache write): the run has no data
without it.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pyarrow as pa
import structlog
from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tabayyun import core
from tabayyun.db.models import DEFAULT_ORG_ID, DEFAULT_WORKSPACE_ID, DatasetSeries, Run, Score, Series
from tabayyun.services import coverage as coverage_service
from tabayyun.services import datasets as datasets_service
from tabayyun.services import findings as findings_service
from tabayyun.services import groups as groups_service
from tabayyun.services import runs as runs_service
from tabayyun.services import series as series_service
from tabayyun.services.cache import CacheError, RunCache
from tabayyun.services.timeconv import datetime_to_ns, ns_to_datetime, ns_to_datetime_ceil

log = structlog.get_logger()

# `runs` is imported as a module, not by name: `runs` -> jobs -> tasks -> execution -> here is
# an import cycle, harmless while names are looked up at call time.

DATASET_TRIGGER = "manual"
NO_CACHED_DATA = "no cached data"
NOT_IN_DATASET = "members not in dataset"
INGEST_COL = "ingest_ts"


class DatasetNotFoundError(LookupError):
    """The dataset does not exist in the workspace (404)."""


@dataclass(frozen=True)
class Member:
    """One series of the dataset as the worker needs it."""

    id: uuid.UUID
    external_id: str
    source_id: uuid.UUID
    meta: core.SeriesMetaIn


@dataclass
class Plan:
    """What a dataset run reads and checks, loaded when the run starts."""

    dataset_id: uuid.UUID
    start_ns: int
    end_ns: int
    now_ns: int
    members: list[Member]
    groups: list[dict[str, Any]] = field(default_factory=list)
    groups_outside: list[dict[str, Any]] = field(default_factory=list)
    missing: dict[str, list[list[int]]] = field(default_factory=dict)


# Creation (request path)


async def create_dataset_run(session: AsyncSession, dataset_id: uuid.UUID, *, now: datetime) -> Run:
    """Queue a run of the dataset over its window resolved at `now`, in the caller's transaction."""
    dataset = await datasets_service.get_dataset(session, dataset_id)
    if dataset is None:
        raise DatasetNotFoundError(str(dataset_id))
    start, end = datasets_service.resolve_window(dataset.window_policy, now)
    run = Run(
        org_id=DEFAULT_ORG_ID,
        workspace_id=DEFAULT_WORKSPACE_ID,
        dataset_id=dataset.id,
        trigger=DATASET_TRIGGER,
        status="queued",
        window_start=start,
        window_end=end,
        now_ns=datetime_to_ns(now),
        stats={"window": {"start": datetime_to_ns(start), "end": datetime_to_ns(end)}},
        created_at=runs_service.utc_now(),
    )
    session.add(run)
    await session.flush()
    log.info("run.queued", run_id=str(run.id), dataset_id=str(dataset.id), window=dataset.window_policy)
    return run


# Execution (worker path)


async def _plan(session: AsyncSession, run: Run) -> Plan | None:
    """Series, groups and coverage gaps of the run's dataset; None when it was deleted."""
    if run.dataset_id is None:
        return None
    window = (run.stats or {}).get("window") or {}
    start_ns, end_ns = int(window["start"]), int(window["end"])
    rows = (
        await session.execute(
            select(Series)
            .join(DatasetSeries, DatasetSeries.series_id == Series.id)
            .where(DatasetSeries.dataset_id == run.dataset_id)
            .order_by(Series.id)
        )
    ).scalars()
    members = [
        Member(
            id=s.id,
            external_id=s.external_id,
            source_id=s.source_id,
            # Keyed by series id: frames, groups and reports all use it (the name stays readable).
            meta=series_service.meta_for_core(str(s.id), series_service.merged_for_run(s, {})),
        )
        for s in rows
    ]
    plan = Plan(
        dataset_id=run.dataset_id,
        start_ns=start_ns,
        end_ns=end_ns,
        now_ns=run.now_ns if run.now_ns is not None else end_ns,
        members=members,
    )
    in_dataset = {m.id for m in members}
    groups = await groups_service.groups_within(session, list(in_dataset))
    group_members = await groups_service.members_of(session, [g.id for g in groups])
    for group in groups:
        listed = group_members[group.id]
        outside = [str(m.series_id) for m in listed if m.series_id not in in_dataset]
        if outside:
            plan.groups_outside.append(
                {"group_id": str(group.id), "reason": NOT_IN_DATASET, "missing": outside}
            )
            continue
        plan.groups.append(
            {
                "id": str(group.id),
                "name": group.name,
                "kind": group.kind,
                "members": [{"series_id": str(m.series_id), "role": m.role} for m in listed],
                "params": group.params,
            }
        )
    for m in members:
        covered = await coverage_service.covered_ranges(session, m.id)
        gaps = coverage_service.missing_ranges(covered, start_ns, end_ns)
        if gaps:
            plan.missing[str(m.id)] = [[s, e] for s, e in gaps]
    return plan


def _read(cache: RunCache, plan: Plan) -> dict[str, pa.RecordBatch]:
    """Cached rows of every member in the window, keyed by series id; blocking."""
    by_source: dict[uuid.UUID, list[str]] = defaultdict(list)
    for m in plan.members:
        by_source[m.source_id].append(str(m.id))
    batches: dict[str, pa.RecordBatch] = {}
    for source_id, series_ids in by_source.items():
        batches.update(
            cache.read_series(
                source_id=str(source_id), series_ids=series_ids, start_ns=plan.start_ns, end_ns=plan.end_ns
            )
        )
    return batches


def _check(plan: Plan, batches: dict[str, pa.RecordBatch]) -> core.MultiReport:
    """Run the core over the members with data; blocking (the core releases the GIL)."""
    metas = {sid: m.meta for m in plan.members if (sid := str(m.id)) in batches}
    # The binding takes one ingest column for all tables: use it only when every series has one.
    has_ingest = all(INGEST_COL in b.schema.names for b in batches.values())
    return core.run_checks_multi(
        batches,
        metas,
        plan.groups,
        window=(plan.start_ns, plan.end_ns),
        now_ns=plan.now_ns,
        quality_col="quality",
        ingest_col=INGEST_COL if has_ingest else None,
    )


async def execute_dataset_run(
    factory: async_sessionmaker[AsyncSession], run_id: uuid.UUID, cache: RunCache | None
) -> None:
    """Execute one queued dataset run to a terminal state. Never raises; failures land on the run."""
    run_log = log.bind(run_id=str(run_id))
    plan: Plan | None = None
    metadata_error: str | None = None
    try:
        async with factory() as session, session.begin():
            run = await session.get(Run, run_id)
            if run is None or run.status != "queued":
                run_log.warning("run.skipped", status=None if run is None else run.status)
                return
            run.status = "running"
            run.started_at = runs_service.utc_now()
            try:
                plan = await _plan(session, run)
            except series_service.MetadataError as exc:
                metadata_error = str(exc)
    # Any other planning error rolls the claim back and leaves the run queued, where neither a
    # retry nor the stale-run reaper would reach it: fail it from `queued` instead.
    except Exception as exc:  # noqa: BLE001
        run_log.exception("run.plan_failed")
        await runs_service.mark_failed(factory, run_id, f"plan error: {runs_service.error_line(exc)}")
        return
    run_log.info("run.started", kind="dataset")

    try:
        if metadata_error is not None:
            raise runs_service.RunFailureError(f"invalid series metadata: {metadata_error}")
        if plan is None:
            raise runs_service.RunFailureError("dataset deleted")
        if cache is None:
            raise runs_service.RunFailureError("no cache configured")
        try:
            batches = await asyncio.to_thread(_read, cache, plan)
        except CacheError as exc:
            raise runs_service.RunFailureError(f"cache read failed: {exc}") from exc
        if not batches:
            raise runs_service.RunFailureError("no cached data in window")
        report = await asyncio.to_thread(_check, plan, batches)
    except runs_service.RunFailureError as exc:
        await runs_service.mark_failed(factory, run_id, str(exc))
        return
    except Exception as exc:  # noqa: BLE001  (any core error must land on the run, not the worker)
        run_log.exception("run.crashed")
        await runs_service.mark_failed(factory, run_id, f"core error: {runs_service.error_line(exc)}")
        return

    try:
        stats = await _persist(factory, run_id, plan, report)
    except SQLAlchemyError as exc:
        run_log.exception("run.persist_failed")
        await runs_service.mark_failed(factory, run_id, f"persist error: {runs_service.error_line(exc)}")
        return
    if stats is None:
        run_log.warning("run.completion_skipped", reason="run no longer running")
        return
    run_log.info(
        "run.finished",
        kind="dataset",
        n_series=stats["n_series"],
        n_findings=stats["n_findings"],
        n_findings_new=stats["n_findings_new"],
        groups=len(stats["groups"]),
        groups_skipped=len(stats["groups_skipped"]),
    )


async def _persist(
    factory: async_sessionmaker[AsyncSession], run_id: uuid.UUID, plan: Plan, report: core.MultiReport
) -> dict[str, Any] | None:
    """Complete the run and store scores, metrics and findings per series; None if not running."""
    async with factory() as session, session.begin():
        finished_at = runs_service.utc_now()
        result = await session.execute(
            update(Run)
            .where(Run.id == run_id, Run.status == "running")
            .values(
                status="succeeded",
                finished_at=finished_at,
                window_start=ns_to_datetime(plan.start_ns),
                window_end=ns_to_datetime_ceil(plan.end_ns),
                now_ns=plan.now_ns,
                error=None,
            )
        )
        if int(getattr(result, "rowcount", 0) or 0) != 1:
            await session.rollback()
            return None
        checked = [m for m in plan.members if str(m.id) in report.reports]  # already in id order
        totals = {
            "n_samples": 0,
            "n_findings": 0,
            "n_metrics": 0,
            "n_findings_new": 0,
            "n_findings_merged": 0,
        }
        series_out: list[dict[str, Any]] = []
        skipped_checks: list[dict[str, str]] = []
        for m in checked:
            rep = report.reports[str(m.id)]
            session.add(
                Score(
                    series_id=m.id,
                    run_id=run_id,
                    layer="raw",
                    method_version=rep.score.method_version,
                    overall=rep.score.overall,
                    dimensions=rep.score.dimensions,
                    n_findings=rep.score.n_findings,
                    computed_at=finished_at,
                )
            )
            outcome = await findings_service.persist_report(
                session, run_id=run_id, series_id=m.id, report=rep, now=finished_at
            )
            totals["n_samples"] += rep.n_samples
            totals["n_findings"] += len(rep.findings)
            totals["n_metrics"] += outcome.metrics
            totals["n_findings_new"] += outcome.new
            totals["n_findings_merged"] += outcome.merged
            series_out.append({"id": str(m.id), "external_id": m.external_id, "score": rep.score.overall})
            skipped_checks.extend({"series_id": str(m.id), **s} for s in rep.skipped)
        core_skipped = [s.model_dump() for s in report.groups_skipped]
        skipped_ids = {s["group_id"] for s in core_skipped}
        stats: dict[str, Any] = {
            "n_series": len(checked),
            **totals,
            "skipped": len(skipped_checks),
            "skipped_checks": skipped_checks,
            "window": {"start": plan.start_ns, "end": plan.end_ns},
            "series": series_out,
            "groups": [g["id"] for g in plan.groups if g["id"] not in skipped_ids],
            "groups_skipped": plan.groups_outside + core_skipped,
            "series_skipped": [
                {"series_id": str(m.id), "external_id": m.external_id, "reason": NO_CACHED_DATA}
                for m in plan.members
                if str(m.id) not in report.reports
            ],
            "missing": plan.missing,
        }
        await session.execute(update(Run).where(Run.id == run_id).values(stats=stats))
    return stats
