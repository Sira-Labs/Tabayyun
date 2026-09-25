"""Runs: create from an upload, enqueue, execute, list and read (spec 002).

Lifecycle: `POST /api/runs` stores the raw upload and a `queued` run in one transaction and
enqueues the job in the same transaction (or, with `TABAYYUN_INLINE_JOBS`, runs it in the
request's background task). The worker moves the run to `running`, executes the core and
persists the outcome in one transaction; any exception marks the run `failed` with a
one-line message. The completion transaction persists the score row, the run statistics
and, through `services.findings`, the metrics and deduplicated findings (spec 003); series
metadata follows the precedence of `services.series` (spec 004). After the run succeeded,
the upload is written to the Parquet cache and its coverage recorded (spec 006); a cache
failure is reported in `stats.cache` and never fails the run.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import structlog
from sqlalchemy import delete, func, select, text, tuple_, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from tabayyun import core
from tabayyun.authz.scope import Scope
from tabayyun.db.models import Run, Score, Upload
from tabayyun.jobs.names import RUN_CHECKS_TASK, RUNS_QUEUE
from tabayyun.services import coverage as coverage_service
from tabayyun.services import findings as findings_service
from tabayyun.services import series as series_service
from tabayyun.services.cache import CacheError, RunCache
from tabayyun.services.pagination import decode_keyset, encode_keyset
from tabayyun.services.timeconv import datetime_to_ns, ns_to_datetime, ns_to_datetime_ceil

log = structlog.get_logger()

MAX_UPLOAD_BYTES = 50 * 1024 * 1024
STALE_AFTER = timedelta(minutes=30)
WORKER_LOST = "worker lost"

# Procrastinate's defer function, called on the run's own connection so that the job and the
# run row commit or roll back together (ADR-0004). Priority 0, no locks, run immediately.
DEFER_JOB_SQL = text(
    "SELECT procrastinate_defer_jobs_v1(ARRAY[ROW(:queue, :task, 0, NULL, NULL, CAST(:args AS jsonb), NULL)"
    "::procrastinate_job_to_defer_v1])"
)


REAP_SQL = text("SELECT tabayyun_reap_stale_runs(:older_than)")


class UploadError(Exception):
    """An upload the API rejects before storing it; carries the HTTP status to answer with."""

    def __init__(self, status_code: int, detail: str) -> None:
        """Keep the HTTP status and the detail for the router."""
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class RunFailureError(Exception):
    """An execution error whose message is safe to store on the run (`cannot parse CSV: …`)."""


@dataclass(frozen=True)
class RunParams:
    """Form fields of an upload, stored with the upload and replayed by the worker."""

    series_id: str = "uploaded"
    unit: str | None = None
    ts_col: str = "ts"
    value_col: str = "value"
    quality_col: str | None = None
    ingest_col: str | None = None
    physical_min: float | None = None
    physical_max: float | None = None
    now_ns: int | None = None
    ts_unit: str = "auto"

    def to_json(self) -> dict[str, Any]:
        """Plain dict for the `uploads.params` column."""
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> RunParams:
        """Inverse of `to_json`; unknown keys are ignored."""
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


def parse_upload(
    data: bytes,
    *,
    ts_col: str,
    value_col: str,
    quality_col: str | None,
    ingest_col: str | None,
    ts_unit: core.TsUnit = "auto",
) -> core.ParsedCsv:
    """Validate size and parse a CSV upload; raises `UploadError` with the HTTP status."""
    if len(data) > MAX_UPLOAD_BYTES:
        raise UploadError(413, "upload larger than 50 MiB")
    if not data:
        raise UploadError(400, "empty upload")
    try:
        return core.read_csv(data, ts_col, value_col, quality_col or None, ingest_col or None, ts_unit)
    except core.TimestampUnitError as exc:
        raise UploadError(422, str(exc)) from exc
    except Exception as exc:  # pyarrow raises several ArrowInvalid/KeyError variants
        raise UploadError(422, f"cannot parse CSV: {exc}") from exc


def utc_now() -> datetime:
    """Current time, timezone-aware UTC."""
    return datetime.now(UTC)


def error_line(exc: BaseException) -> str:
    """First line of an exception message, or its type name when the message is empty."""
    message = str(exc).strip().splitlines()
    return message[0] if message else type(exc).__name__


# Creation and enqueue (request path)


async def create_run(
    session: AsyncSession,
    scope: Scope,
    *,
    data: bytes,
    filename: str,
    content_type: str | None,
    params: RunParams,
) -> Run:
    """Insert the upload and the queued run in the caller's transaction."""
    now = utc_now()
    run = Run(
        org_id=scope.org_id,
        workspace_id=scope.workspace_id,
        trigger="upload",
        status="queued",
        now_ns=params.now_ns,
        stats={},
        created_at=now,
    )
    session.add(run)
    await session.flush()
    session.add(
        Upload(
            run_id=run.id,
            org_id=scope.org_id,
            filename=filename,
            content_type=content_type,
            size_bytes=len(data),
            data=data,
            params=params.to_json(),
            created_at=now,
        )
    )
    await session.flush()
    log.info("run.queued", run_id=str(run.id), series=params.series_id, bytes=len(data))
    return run


async def enqueue_run(session: AsyncSession, run_id: uuid.UUID, org_id: uuid.UUID) -> None:
    """Defer the job on the session's connection: same transaction as the run row.

    The job carries the run's org: the worker needs it to see the run at all (spec 007).
    """
    args = {"run_id": str(run_id), "org_id": str(org_id)}
    await session.execute(
        DEFER_JOB_SQL, {"queue": RUNS_QUEUE, "task": RUN_CHECKS_TASK, "args": json.dumps(args)}
    )


# Reads


async def get_run(session: AsyncSession, scope: Scope, run_id: uuid.UUID) -> Run | None:
    """The run in the scope's workspace, or None."""
    stmt = select(Run).where(Run.id == run_id, Run.workspace_id == scope.workspace_id)
    return (await session.execute(stmt)).scalar_one_or_none()


def encode_cursor(run: Run) -> str:
    """Opaque keyset cursor for `list_runs`."""
    return encode_keyset(run.created_at, run.id)


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    """Inverse of `encode_cursor`; raises ValueError on garbage."""
    return decode_keyset(cursor)


async def list_runs(
    session: AsyncSession, scope: Scope, *, limit: int, cursor: str | None
) -> tuple[list[Run], str | None]:
    """Newest first, keyset-paginated on `(created_at desc, id desc)`."""
    stmt = (
        select(Run)
        .where(Run.workspace_id == scope.workspace_id)
        .order_by(Run.created_at.desc(), Run.id.desc())
        .limit(limit + 1)
    )
    if cursor:
        created_at, run_id = decode_cursor(cursor)
        stmt = stmt.where(tuple_(Run.created_at, Run.id) < (created_at, run_id))
    rows = list((await session.execute(stmt)).scalars())
    next_cursor = encode_cursor(rows[limit - 1]) if len(rows) > limit else None
    return rows[:limit], next_cursor


def run_to_dict(run: Run) -> dict[str, Any]:
    """The `Run` response shape of spec 002."""
    stats = dict(run.stats or {})
    series = stats.pop("series", [])
    # The exact ns bounds live in stats (timestamptz truncates to µs and loses the +1 ns of
    # the core's exclusive end); the columns serve range queries.
    window = stats.pop("window", None)
    if window is None and run.window_start is not None and run.window_end is not None:
        window = {"start": datetime_to_ns(run.window_start), "end": datetime_to_ns(run.window_end)}
    duration_ms = None
    if run.started_at is not None and run.finished_at is not None:
        duration_ms = int((run.finished_at - run.started_at).total_seconds() * 1000)
    return {
        "id": str(run.id),
        "dataset_id": None if run.dataset_id is None else str(run.dataset_id),
        "trigger": run.trigger,
        "status": run.status,
        "window": window,
        "now_ns": run.now_ns,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "duration_ms": duration_ms,
        "stats": stats,
        "series": series,
        "error": run.error,
        "created_at": run.created_at,
    }


# Execution (worker path)


async def mark_failed(factory: async_sessionmaker[AsyncSession], run_id: uuid.UUID, message: str) -> None:
    """Fail a queued or running run with `message` and drop its upload, in its own transaction."""
    async with factory() as session, session.begin():
        await session.execute(
            update(Run)
            .where(Run.id == run_id, Run.status.in_(("queued", "running")))
            .values(status="failed", error=message[:1000], finished_at=utc_now())
        )
        await session.execute(delete(Upload).where(Upload.run_id == run_id))
    log.error("run.failed", run_id=str(run_id), error=message)


async def execute_run(
    factory: async_sessionmaker[AsyncSession], run_id: uuid.UUID, cache: RunCache | None = None
) -> None:
    """Execute one queued run to a terminal state. Never raises; failures land on the run.

    With a `cache`, a succeeded upload run is also written to the Parquet cache (spec 006).
    """
    run_log = log.bind(run_id=str(run_id))
    async with factory() as session, session.begin():
        run = await session.get(Run, run_id)
        if run is None or run.status != "queued":
            run_log.warning("run.skipped", status=None if run is None else run.status)
            return
        run.status = "running"
        run.started_at = utc_now()
        scope = Scope(org_id=run.org_id, workspace_id=run.workspace_id)
        upload = await session.get(Upload, run_id)
        payload = None if upload is None else (upload.data, RunParams.from_json(upload.params))
        # Stored metadata is read, not created: a run that fails leaves no series behind.
        stored = (
            None
            if payload is None
            else await series_service.find_upload_series(session, scope, payload[1].series_id)
        )
    run_log.info("run.started")

    try:
        if payload is None:
            raise RunFailureError("upload missing")
        data, params = payload
        overrides = series_service.upload_overrides(params.unit, params.physical_min, params.physical_max)
        try:
            merged = series_service.merged_for_run(stored, overrides)
        except series_service.MetadataError as exc:
            raise RunFailureError(f"invalid series metadata: {exc}") from exc
        try:
            parsed = parse_upload(
                data,
                ts_col=params.ts_col,
                value_col=params.value_col,
                quality_col=params.quality_col,
                ingest_col=params.ingest_col,
                ts_unit=cast(core.TsUnit, params.ts_unit),  # validated when the run was created
            )
        except UploadError as exc:
            raise RunFailureError(exc.detail) from exc
        table = parsed.table
        meta = series_service.meta_for_core(params.series_id, merged)
        # The core releases the GIL; a thread keeps the worker's event loop responsive.
        report = await asyncio.to_thread(
            core.run_checks,
            table,
            meta,
            now_ns=params.now_ns,
            ts_col=params.ts_col,
            value_col=params.value_col,
            quality_col=params.quality_col or None,
            ingest_col=params.ingest_col or None,
        )
    except RunFailureError as exc:
        await mark_failed(factory, run_id, str(exc))
        return
    except Exception as exc:  # noqa: BLE001  (any core error must land on the run, not the worker)
        run_log.exception("run.crashed")
        await mark_failed(factory, run_id, f"core error: {error_line(exc)}")
        return

    try:
        async with factory() as session, session.begin():
            finished_at = utc_now()
            # Only a run still `running` completes: the reaper may have failed it meanwhile.
            # The persistence below is skipped (and rolled back) unless exactly one row changed.
            result = await session.execute(
                update(Run)
                .where(Run.id == run_id, Run.status == "running")
                .values(
                    status="succeeded",
                    finished_at=finished_at,
                    window_start=ns_to_datetime(report.window["start"]),
                    window_end=ns_to_datetime_ceil(report.window["end"]),
                    now_ns=report.now_ns,
                    error=None,
                )
            )
            if int(getattr(result, "rowcount", 0) or 0) != 1:
                await session.rollback()
                run_log.warning("run.completion_skipped", reason="run no longer running")
                return
            series = await series_service.upsert_upload_series(
                session, scope, params.series_id, overrides, now=finished_at
            )
            cache_target = (series.id, series.source_id)
            session.add(
                Score(
                    series_id=series.id,
                    org_id=scope.org_id,
                    run_id=run_id,
                    layer="raw",
                    method_version=report.score.method_version,
                    overall=report.score.overall,
                    dimensions=report.score.dimensions,
                    n_findings=report.score.n_findings,
                    computed_at=finished_at,
                )
            )
            outcome = await findings_service.persist_report(
                session, scope, run_id=run_id, series_id=series.id, report=report, now=finished_at
            )
            stats = {
                "n_series": 1,
                "n_samples": report.n_samples,
                "n_findings": len(report.findings),
                "n_metrics": outcome.metrics,
                "n_findings_new": outcome.new,
                "n_findings_merged": outcome.merged,
                "skipped": len(report.skipped),
                # Which checks could not run and what they need, for the run report (spec 005).
                "skipped_checks": report.skipped,
                "ts_unit": parsed.ts_unit,
                "window": {"start": report.window["start"], "end": report.window["end"]},
                "series": [
                    {"id": str(series.id), "external_id": series.external_id, "score": report.score.overall}
                ],
            }
            await session.execute(update(Run).where(Run.id == run_id).values(stats=stats))
            await session.execute(delete(Upload).where(Upload.run_id == run_id))
    except series_service.MetadataError as exc:
        # Rolled back: the series changed since the run started and no longer fits the upload.
        await mark_failed(factory, run_id, f"invalid series metadata: {exc}")
        return
    except SQLAlchemyError as exc:
        run_log.exception("run.persist_failed")
        await mark_failed(factory, run_id, f"persist error: {error_line(exc)}")
        return
    cache_info = (
        None
        if cache is None
        else await _cache_upload(
            factory,
            run_id,
            cache,
            org_id=scope.org_id,
            series_id=cache_target[0],
            source_id=cache_target[1],
            table=table,
            params=params,
        )
    )
    run_log.info(
        "run.finished",
        n_findings=len(report.findings),
        n_findings_new=outcome.new,
        n_findings_merged=outcome.merged,
        n_samples=report.n_samples,
        cached=None if cache_info is None else cache_info["written"],
    )


async def _cache_upload(
    factory: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
    cache: RunCache,
    *,
    org_id: uuid.UUID,
    series_id: uuid.UUID,
    source_id: uuid.UUID,
    table: Any,
    params: RunParams,
) -> dict[str, Any]:
    """Write a succeeded upload to the Parquet cache, record coverage and `stats.cache`.

    Runs after the completion transaction so no network I/O happens while the series lock is
    held; the run is already `succeeded` and stays so whatever happens here.
    """
    run_log = log.bind(run_id=str(run_id))
    written = None
    try:
        written = await asyncio.to_thread(
            cache.write_series,
            source_id=str(source_id),
            series_id=str(series_id),
            table=table,
            ts_col=params.ts_col,
            value_col=params.value_col,
            quality_col=params.quality_col or None,
            ingest_col=params.ingest_col or None,
        )
        info: dict[str, Any] = {"written": True, "rows": written.rows, "files": written.files}
    except CacheError as exc:
        run_log.warning("cache.write_failed", error=str(exc))
        info = {"written": False, "error": str(exc)}
    try:
        async with factory() as session, session.begin():
            if written is not None and written.rows:
                await coverage_service.record(
                    session,
                    org_id=org_id,
                    series_id=series_id,
                    start_ns=written.start_ns,
                    end_ns=written.end_ns,
                    rows=written.rows,
                    now=utc_now(),
                )
            run = await session.get(Run, run_id, with_for_update=True)
            if run is not None:
                run.stats = {**(run.stats or {}), "cache": info}
    except SQLAlchemyError:
        run_log.exception("cache.record_failed")
    return info


# Maintenance and health


async def reap_stale_runs(
    factory: async_sessionmaker[AsyncSession], *, stale_after: timedelta = STALE_AFTER
) -> int:
    """Mark runs `running` for longer than `stale_after` as failed with `worker lost`.

    The one job that spans orgs: the owner's `tabayyun_reap_stale_runs` function does the work
    (migration 0004), so the app login needs no tenant context and no way around RLS.
    """
    async with factory() as session, session.begin():
        reaped = await session.scalar(REAP_SQL, {"older_than": stale_after})
    return int(reaped or 0)


async def queue_counts(engine: AsyncEngine) -> dict[str, int] | None:
    """Pending (`todo`) and running (`doing`) jobs, or None when the queue schema is absent."""
    stmt = text(
        "SELECT status::text, count(*) FROM procrastinate_jobs WHERE status IN ('todo', 'doing') GROUP BY 1"
    )
    try:
        async with engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
    except SQLAlchemyError:
        return None
    counts = {status: int(n) for status, n in rows}
    return {"pending": counts.get("todo", 0), "running": counts.get("doing", 0)}


async def count_runs(session: AsyncSession) -> int:
    """Number of runs in the default workspace (used by tests and health details)."""
    return int(await session.scalar(select(func.count()).select_from(Run)) or 0)
