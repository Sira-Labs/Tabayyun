"""Scans of file-drop sources (spec 026): read new and changed files of a `csv_dir` source.

A scan is a `source_fetches` row with trigger `ingest`, claimed and finished like a fetch
(`fetches.prepare`, `fetches.finish`), so the source's health follows it. Executing one:

1. lists the source's folder (`CsvDirConnector.list_files`): files ready to read, oldest first;
2. skips known files: a `source_files` row with the same size and time, or else the same
   SHA-256 (the row then takes the new size and time);
3. reads at most `max_files_per_scan` unknown files. For each, in order: read it (blocking, in a
   thread), create the series it names, write each series to the raw layer of the cache, then
   record coverage, the file's row and the scan's counters in one transaction. A file that
   cannot be read gets a `failed` row and the scan goes on; a cache that cannot be written
   stops the scan, and the file is read again next time.

Files are only read, never moved or changed.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pyarrow as pa
import structlog
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tabayyun.connectors import ConnectorError
from tabayyun.connectors.csv_dir import (
    CsvDirConfig,
    CsvDirConnector,
    FileReadError,
    FileRows,
    FoundFile,
    read_file,
)
from tabayyun.connectors.csv_dir import files as file_listing
from tabayyun.db.models import Series, SourceFetch, SourceFile
from tabayyun.services import coverage as coverage_service
from tabayyun.services.cache import CacheError, CacheWrite, RunCache
from tabayyun.services.fetches import FetchDeps, FetchOutcome, JobPlan, StopError, finish, prepare
from tabayyun.services.timeconv import datetime_to_ns, ns_to_datetime, ns_to_datetime_ceil

log = structlog.get_logger()

READ_FAILED = "could not read the file"
OPEN_FAILED = "could not open the file"
NO_CACHE = "no cache configured"
SERIES_CHUNK = 1000


@dataclass(frozen=True)
class _Known:
    """What a file's row says about the content it last read."""

    size: int
    mtime_us: int
    sha256: str


@dataclass
class _Scan:
    """A scan in progress: its plan, its connector and what it has done."""

    plan: JobPlan
    connector: CsvDirConnector
    fetch_id: uuid.UUID
    clock: Callable[[], datetime]
    results: list[dict[str, Any]] = field(default_factory=list)
    rows: int = 0

    @property
    def config(self) -> CsvDirConfig:
        return self.connector.settings


def _us(ns: int) -> int:
    """Nanoseconds truncated to the microsecond, as timestamptz keeps them."""
    return ns // 1000 * 1000


async def execute_ingest(
    factory: async_sessionmaker[AsyncSession], deps: FetchDeps, fetch_id: uuid.UUID
) -> FetchOutcome | None:
    """Scan a `csv_dir` source's folder; None when the job is not one to run (spec 026)."""
    scan_log = log.bind(fetch_id=str(fetch_id))
    try:
        plan = await prepare(factory, deps, fetch_id, with_calls=False)
    except StopError as stop:
        return await finish(factory, deps, fetch_id, FetchOutcome("failed", error=str(stop)), scan_log)
    if plan is None:
        scan_log.warning("ingest.skipped")
        return None
    scan_log = scan_log.bind(source_id=str(plan.source_id))
    if not isinstance(plan.connector, CsvDirConnector):
        outcome = FetchOutcome("failed", error="this source does not ingest files")
        return await finish(factory, deps, fetch_id, outcome, scan_log)
    assert plan.workspace_id is not None  # `prepare` sets it
    scan = _Scan(plan, plan.connector, fetch_id, deps.clock)
    try:
        outcome = await _scan(factory, deps, scan, scan_log)
    except StopError as stop:
        outcome = FetchOutcome("failed", rows=scan.rows, error=str(stop), result=_result(scan, None))
    result = await finish(factory, deps, fetch_id, outcome, scan_log)
    scan_log.info("ingest.finished", status=outcome.status, files=len(scan.results), rows=scan.rows)
    return result


async def _scan(
    factory: async_sessionmaker[AsyncSession], deps: FetchDeps, scan: _Scan, scan_log: Any
) -> FetchOutcome:
    """List, skip the known files, read the rest; the scan's outcome.

    Raises:
        StopError: the folder cannot be listed, or the cache is missing or unwritable.
    """
    try:
        listing = await asyncio.to_thread(scan.connector.list_files, datetime_to_ns(deps.clock()))
    except ConnectorError as exc:
        raise StopError(str(exc), retryable=False) from None
    if deps.cache is None:
        raise StopError(NO_CACHE, retryable=False)
    known = await _known(factory, scan.plan.source_id, [f.path for f in listing.files])
    todo: list[tuple[FoundFile, str]] = []
    unchanged = 0
    more = False
    for found in listing.files:
        row = known.get(found.path)
        if row is not None and row.size == found.size and row.mtime_us == _us(found.mtime_ns):
            unchanged += 1
            continue
        if len(todo) >= scan.config.max_files_per_scan:
            more = True
            break
        try:
            digest = await asyncio.to_thread(file_listing.sha256, found.full)
        except OSError:
            continue  # gone or unreadable since the listing; the next scan sees it again
        if row is not None and row.sha256 == digest:
            await _touch(factory, scan.plan.source_id, found)
            unchanged += 1
            continue
        todo.append((found, digest))
    for found, digest in todo:
        await _ingest_file(factory, deps.cache, scan, found, digest, scan_log)
    scan_log.info(
        "ingest.listed",
        ready=len(listing.files),
        known=unchanged,
        read=len(todo),
        settling=listing.settling,
        too_large=len(listing.too_large),
        outside_root=len(listing.outside_root),
    )
    result = _result(scan, listing, known=unchanged, more=more or listing.truncated)
    failed = [r for r in scan.results if r["status"] == "failed"]
    if failed:
        error = f"{len(failed)} file(s) failed: {failed[0]['path']}: {failed[0]['error']}"
        return FetchOutcome("partial", rows=scan.rows, error=error[:1000], result=result)
    return FetchOutcome("succeeded", rows=scan.rows, result=result)


def _result(
    scan: _Scan, listing: file_listing.Listing | None, *, known: int = 0, more: bool = False
) -> dict[str, Any]:
    """The scan's `result` (spec 026, behaviour 5)."""
    return {
        "files": scan.results,
        "known": known,
        "settling": 0 if listing is None else listing.settling,
        "too_large": [] if listing is None else listing.too_large[:100],
        "outside_root": [] if listing is None else listing.outside_root[:100],
        "more": more,
    }


async def _known(
    factory: async_sessionmaker[AsyncSession], source_id: uuid.UUID, paths: list[str]
) -> dict[str, _Known]:
    """The rows of these paths, by path."""
    out: dict[str, _Known] = {}
    async with factory() as session:
        for lo in range(0, len(paths), SERIES_CHUNK):
            stmt = select(SourceFile.path, SourceFile.size, SourceFile.mtime, SourceFile.sha256).where(
                SourceFile.source_id == source_id, SourceFile.path.in_(paths[lo : lo + SERIES_CHUNK])
            )
            for path, size, mtime, digest in (await session.execute(stmt)).tuples():
                out[path] = _Known(size, datetime_to_ns(mtime), digest)
    return out


async def _touch(factory: async_sessionmaker[AsyncSession], source_id: uuid.UUID, found: FoundFile) -> None:
    """A file whose content is unchanged takes its new size and time (copied again, touched)."""
    async with factory() as session, session.begin():
        await session.execute(
            update(SourceFile)
            .where(SourceFile.source_id == source_id, SourceFile.path == found.path)
            .values(size=found.size, mtime=ns_to_datetime(found.mtime_ns))
        )


async def _ingest_file(
    factory: async_sessionmaker[AsyncSession],
    cache: RunCache,
    scan: _Scan,
    found: FoundFile,
    digest: str,
    scan_log: Any,
) -> None:
    """Read one file into the cache and record it; a file that cannot be read is recorded failed.

    Raises:
        StopError: the cache cannot be written (the file stays unrecorded and is read again).
    """
    file_log = scan_log.bind(path=found.path)
    try:
        rows = await asyncio.to_thread(read_file, found.full, scan.config)
    except FileReadError as exc:
        await _record_failure(factory, scan, found, digest, str(exc))
        return
    except OSError:
        await _record_failure(factory, scan, found, digest, OPEN_FAILED)
        return
    except Exception as exc:  # noqa: BLE001  (a parser bug must land on the file, not stop the scan)
        file_log.error("ingest.read_crashed", error_type=type(exc).__name__)
        await _record_failure(factory, scan, found, digest, READ_FAILED)
        return
    async with factory() as session, session.begin():
        ids, created = await _series(session, scan.plan, list(rows.series))
    written: dict[uuid.UUID, CacheWrite] = {}
    for external_id, table in rows.series.items():
        if table.num_rows == 0:
            continue
        written[ids[external_id]] = await _write(cache, scan.plan.source_id, ids[external_id], table)
    total = sum(w.rows for w in written.values())
    async with factory() as session, session.begin():
        now = scan.clock()
        for series_id, w in written.items():
            if w.rows:
                await coverage_service.record(
                    session,
                    org_id=scan.plan.org_id,
                    series_id=series_id,
                    start_ns=w.start_ns,
                    end_ns=w.end_ns,
                    rows=w.rows,
                    now=now,
                )
        await _upsert(
            session, scan, found, digest, status="ingested", rows=rows, total=total, created=created
        )
        await _count(session, scan.fetch_id, total)
    scan.rows += total
    scan.results.append(
        {
            "path": found.path,
            "status": "ingested",
            "rows": total,
            "series": len(rows.series),
            "new_series": created,
            "error": None,
        }
    )
    file_log.info("ingest.file_read", rows=total, series=len(rows.series), new_series=created)


async def _write(cache: RunCache, source_id: uuid.UUID, series_id: uuid.UUID, table: pa.Table) -> CacheWrite:
    """One series' rows to the raw layer.

    Raises:
        StopError: the cache write failed.
    """
    try:
        return await asyncio.to_thread(
            cache.write_series,
            source_id=str(source_id),
            series_id=str(series_id),
            table=table,
            ts_col="ts",
            value_col="value",
            quality_col="quality",
            ingest_col=None,
        )
    except CacheError as exc:
        raise StopError(f"cache write failed: {exc}", retryable=True) from None


async def _series(
    session: AsyncSession, plan: JobPlan, external_ids: list[str]
) -> tuple[dict[str, uuid.UUID], int]:
    """Ids of the source's series with these external ids, creating the missing ones; and how
    many it created."""
    created = 0
    ids: dict[str, uuid.UUID] = {}
    source = plan.source_id
    for lo in range(0, len(external_ids), SERIES_CHUNK):
        chunk = external_ids[lo : lo + SERIES_CHUNK]
        stmt = (
            insert(Series)
            .values(
                [
                    {
                        "id": uuid.uuid4(),
                        "org_id": plan.org_id,
                        "workspace_id": plan.workspace_id,
                        "source_id": source,
                        "external_id": ext,
                        "name": ext,
                    }
                    for ext in chunk
                ]
            )
            .on_conflict_do_nothing(index_elements=[Series.source_id, Series.external_id])
            .returning(Series.id)
        )
        created += len((await session.execute(stmt)).all())
        rows = await session.execute(
            select(Series.external_id, Series.id).where(
                Series.source_id == source, Series.external_id.in_(chunk)
            )
        )
        ids.update(dict(rows.tuples().all()))
    return ids, created


async def _upsert(
    session: AsyncSession,
    scan: _Scan,
    found: FoundFile,
    digest: str,
    *,
    status: str,
    rows: FileRows | None = None,
    total: int = 0,
    created: int = 0,
    error: str | None = None,
) -> None:
    """Insert the file's row, or update it to what this read gave (one more version)."""
    now = scan.clock()
    values: dict[str, Any] = {
        "size": found.size,
        "mtime": ns_to_datetime(found.mtime_ns),
        "sha256": digest,
        "status": status,
        "rows": total,
        "n_series": 0 if rows is None else len(rows.series),
        "n_new_series": created,
        "start_at": None if rows is None or rows.start_ns is None else ns_to_datetime(rows.start_ns),
        "end_at": None if rows is None or rows.end_ns is None else ns_to_datetime_ceil(rows.end_ns),
        "error": error,
        "fetch_id": scan.fetch_id,
        "updated_at": now,
    }
    stmt = insert(SourceFile).values(
        id=uuid.uuid4(),
        org_id=scan.plan.org_id,
        workspace_id=scan.plan.workspace_id,
        source_id=scan.plan.source_id,
        path=found.path,
        created_at=now,
        **values,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[SourceFile.source_id, SourceFile.path],
        set_={**values, "versions": SourceFile.versions + 1},
    )
    await session.execute(stmt)


async def _count(session: AsyncSession, fetch_id: uuid.UUID, rows: int) -> None:
    """One more file read by the scan, and its rows."""
    await session.execute(
        update(SourceFetch)
        .where(SourceFetch.id == fetch_id)
        .values(calls=SourceFetch.calls + 1, rows=SourceFetch.rows + rows)
    )


async def _record_failure(
    factory: async_sessionmaker[AsyncSession], scan: _Scan, found: FoundFile, digest: str, error: str
) -> None:
    """The file's row says why it could not be read; the scan goes on."""
    async with factory() as session, session.begin():
        await _upsert(session, scan, found, digest, status="failed", error=error[:1000])
        await _count(session, scan.fetch_id, 0)
    scan.results.append(
        {
            "path": found.path,
            "status": "failed",
            "rows": 0,
            "series": 0,
            "new_series": 0,
            "error": error[:1000],
        }
    )
