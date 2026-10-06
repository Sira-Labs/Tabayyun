"""The fetch engine (spec 021): fill a connector source's coverage gaps from its system.

A fetch is a `source_fetches` row (manual, poll, run or check). Executing it:

1. loads the source, its series and their gaps (coverage against the fetch window), decrypts
   the credentials and builds the connector, then marks the fetch `running`;
2. runs the planned calls in time order, paced per source; each call's batches go to the raw
   layer of the cache, and the call's span is recorded as covered up to `now - settle_s`, in
   its own transaction, so progress survives a later failure;
3. ends `succeeded`, or `partial` / `failed` on an error, and updates the source's health.

A retryable error leaves the fetch `partial` and the caller (the job) retries; the retry
fetches only what is still missing. Connector errors carry admin-facing messages; anything
else a connector raises is logged with its type only and reported as an internal error, so a
library message cannot leak a credential.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import pyarrow as pa
import structlog
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tabayyun import connectors, secrets
from tabayyun.connectors import BATCH_SCHEMA, QUALITIES, Connector, ConnectorError, NetPolicy, PointRef
from tabayyun.connectors.plan import Call, Pacer, plan_calls, settled_end
from tabayyun.db.models import Series, Source, SourceFetch
from tabayyun.jobs.names import CHECK_SOURCE_TASK, FETCH_QUEUE, FETCH_WINDOW_TASK
from tabayyun.services import coverage as coverage_service
from tabayyun.services.cache import CacheError, RunCache
from tabayyun.services.runs import utc_now
from tabayyun.services.timeconv import datetime_to_ns

log = structlog.get_logger()

SECOND_NS = 1_000_000_000
DEGRADED_AFTER = 1
FAILING_AFTER = 3
KEEP_FOR = timedelta(days=30)
INTERNAL_ERROR = "internal connector error"
BUDGET_SPENT = "fetch budget spent"
NO_MASTER_KEY = "credentials unavailable: TABAYYUN_MASTER_KEY is not set"

# Procrastinate's defer function with a lock: one fetch per source runs at a time.
DEFER_LOCKED_SQL = text(
    "SELECT procrastinate_defer_jobs_v1(ARRAY[ROW(:queue, :task, 0, :lock, NULL, CAST(:args AS jsonb), NULL)"
    "::procrastinate_job_to_defer_v1])"
)
CLAIM_DUE_SQL = text("SELECT source_id, org_id, workspace_id FROM tabayyun_claim_due_sources(:now, :types)")
PRUNE_SQL = text("SELECT tabayyun_prune_source_fetches(:before)")


class RetryableFetchError(Exception):
    """A fetch stopped on a retryable error; the job raises it so Procrastinate retries."""


@dataclass
class FetchDeps:
    """What executing a fetch needs besides the database."""

    cache: RunCache | None
    net: NetPolicy
    keyring: secrets.Keyring | None
    clock: Callable[[], datetime] = utc_now
    pacer: Callable[[float], Pacer] = Pacer
    monotonic: Callable[[], float] = time.monotonic


@dataclass
class FetchOutcome:
    """How a fetch ended."""

    status: str
    calls: int = 0
    rows: int = 0
    error: str | None = None
    retryable: bool = False
    series_rows: dict[str, int] = field(default_factory=dict)


@dataclass
class _Plan:
    source_id: uuid.UUID
    org_id: uuid.UUID
    connector: Connector
    calls: list[Call]
    settle_ns: int


class _StopError(Exception):
    """Ends a fetch early with an error message."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


# Creation (request path, poll, run)


async def create_fetch(
    session: AsyncSession,
    source: Source,
    *,
    trigger: str,
    start: datetime,
    end: datetime,
    series_ids: Sequence[uuid.UUID] | None = None,
    force: bool = False,
    user_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
    status: str = "queued",
) -> SourceFetch:
    """A fetch row of `source` in the caller's transaction."""
    fetch = SourceFetch(
        org_id=source.org_id,
        workspace_id=source.workspace_id,
        source_id=source.id,
        trigger=trigger,
        status=status,
        window_start=start,
        window_end=end,
        series_ids=list(series_ids) if series_ids is not None else None,
        force=force,
        requested_by=user_id,
        run_id=run_id,
    )
    session.add(fetch)
    await session.flush()
    return fetch


async def enqueue(session: AsyncSession, fetch: SourceFetch) -> None:
    """Defer the fetch's job in the caller's transaction, locked to its source."""
    task = CHECK_SOURCE_TASK if fetch.trigger == "check" else FETCH_WINDOW_TASK
    args = {"fetch_id": str(fetch.id), "org_id": str(fetch.org_id)}
    await session.execute(
        DEFER_LOCKED_SQL,
        {"queue": FETCH_QUEUE, "task": task, "lock": f"source:{fetch.source_id}", "args": json.dumps(args)},
    )


# Execution (worker, or inline)


async def execute_fetch(
    factory: async_sessionmaker[AsyncSession],
    deps: FetchDeps,
    fetch_id: uuid.UUID,
    *,
    deadline: float | None = None,
) -> FetchOutcome | None:
    """Execute a queued (or partial, on a retry) fetch; None when it is not one to run.

    `deadline` (on `deps.monotonic`) stops before a call that would start after it.
    """
    fetch_log = log.bind(fetch_id=str(fetch_id))
    started = deps.monotonic()
    outcome = FetchOutcome(status="running")
    try:
        plan = await _prepare(factory, deps, fetch_id)
    except _StopError as stop:
        return await _finish(factory, deps, fetch_id, FetchOutcome("failed", error=str(stop)), fetch_log)
    if plan is None:
        fetch_log.warning("fetch.skipped")
        return None
    fetch_log = fetch_log.bind(source_id=str(plan.source_id))
    fetch_log.info("fetch.started", calls=len(plan.calls))
    pacer = deps.pacer(plan.connector.limits().requests_per_second)
    try:
        for call in plan.calls:
            if deadline is not None and deps.monotonic() >= deadline:
                raise _StopError(BUDGET_SPENT, retryable=False)
            await pacer.wait()
            rows = await _run_call(factory, deps, plan, fetch_id, call, outcome)
            outcome.calls += 1
            outcome.rows += rows
    except _StopError as stop:
        # Retryable: the retry continues where this one stopped. A spent budget kept what it
        # fetched. Anything else will not get better by itself.
        outcome.status = "partial" if stop.retryable or str(stop) == BUDGET_SPENT else "failed"
        outcome.error, outcome.retryable = str(stop), stop.retryable
    else:
        outcome.status = "succeeded"
    result = await _finish(factory, deps, fetch_id, outcome, fetch_log)
    fetch_log.info(
        "fetch.finished",
        status=outcome.status,
        calls=outcome.calls,
        rows=outcome.rows,
        duration_s=round(deps.monotonic() - started, 3),
    )
    return result


async def execute_check(
    factory: async_sessionmaker[AsyncSession], deps: FetchDeps, fetch_id: uuid.UUID
) -> FetchOutcome | None:
    """Reach the source's system with its connector's `check`; the outcome lands in its health."""
    fetch_log = log.bind(fetch_id=str(fetch_id))
    try:
        plan = await _prepare(factory, deps, fetch_id, with_calls=False)
    except _StopError as stop:
        return await _finish(factory, deps, fetch_id, FetchOutcome("failed", error=str(stop)), fetch_log)
    if plan is None:
        return None
    try:
        await plan.connector.check()
    except ConnectorError as exc:
        outcome = FetchOutcome("failed", error=str(exc), retryable=exc.retryable)
    except Exception as exc:  # noqa: BLE001  (a connector bug must land on the fetch, not the worker)
        fetch_log.error("fetch.connector_crashed", error_type=type(exc).__name__)
        outcome = FetchOutcome("failed", error=INTERNAL_ERROR)
    else:
        outcome = FetchOutcome("succeeded")
    return await _finish(factory, deps, fetch_id, outcome, fetch_log)


async def _prepare(
    factory: async_sessionmaker[AsyncSession],
    deps: FetchDeps,
    fetch_id: uuid.UUID,
    *,
    with_calls: bool = True,
) -> _Plan | None:
    """Claim the fetch and plan its calls; None when it is not queued (or partial)."""
    async with factory() as session, session.begin():
        fetch = await session.get(SourceFetch, fetch_id, with_for_update=True)
        if fetch is None or fetch.status not in ("queued", "partial"):
            return None
        source = await session.get(Source, fetch.source_id)
        if source is None:
            raise _StopError("source deleted", retryable=False)
        fetch.status = "running"
        fetch.started_at = fetch.started_at or deps.clock()
        if not source.enabled and fetch.trigger != "check":
            raise _StopError("source disabled", retryable=False)
        try:
            credentials = await secrets.load(session, deps.keyring, org_id=source.org_id, source_id=source.id)
        except secrets.CredentialsUnavailableError:
            raise _StopError(NO_MASTER_KEY, retryable=False) from None
        except secrets.CredentialsError as exc:
            raise _StopError(str(exc), retryable=False) from None
        try:
            connector = connectors.build(source.type, source.config, credentials, deps.net)
        except ConnectorError as exc:
            raise _StopError(str(exc), retryable=False) from None
        calls: list[Call] = []
        if with_calls:
            gaps = await _gaps(session, source, fetch)
            calls = plan_calls(gaps, connector.limits().within(connectors.Limits.of(connector.config)))
        return _Plan(source.id, source.org_id, connector, calls, connector.config.settle_s * SECOND_NS)


async def _gaps(
    session: AsyncSession, source: Source, fetch: SourceFetch
) -> dict[PointRef, list[tuple[int, int]]]:
    """Per series of the fetch, the parts of its window the cache lacks (all of it with `force`)."""
    stmt = select(Series.id, Series.external_id).where(Series.source_id == source.id)
    if fetch.series_ids is not None:
        stmt = stmt.where(Series.id.in_(fetch.series_ids))
    start_ns, end_ns = datetime_to_ns(fetch.window_start), datetime_to_ns(fetch.window_end)
    gaps: dict[PointRef, list[tuple[int, int]]] = {}
    for series_id, external_id in (await session.execute(stmt.order_by(Series.external_id))).tuples():
        if fetch.force:
            gaps[PointRef(series_id, external_id)] = [(start_ns, end_ns)]
            continue
        covered = await coverage_service.covered_ranges(session, series_id)
        missing = coverage_service.missing_ranges(covered, start_ns, end_ns)
        if missing:
            gaps[PointRef(series_id, external_id)] = missing
    return gaps


async def _run_call(
    factory: async_sessionmaker[AsyncSession],
    deps: FetchDeps,
    plan: _Plan,
    fetch_id: uuid.UUID,
    call: Call,
    outcome: FetchOutcome,
) -> int:
    """One connector call: collect its batches, write them to the cache, record coverage."""
    wanted = {p.series_id for p in call.points}
    tables: dict[uuid.UUID, list[pa.Table]] = defaultdict(list)
    try:
        async for batch in plan.connector.fetch(call.points, call.start_ns, call.end_ns):
            if batch.series_id not in wanted:
                raise _StopError("connector returned rows for a point it was not asked for", retryable=False)
            tables[batch.series_id].append(_checked(batch.table))
    except ConnectorError as exc:
        raise _StopError(str(exc), retryable=exc.retryable) from None
    except _StopError:
        raise
    except Exception as exc:  # noqa: BLE001  (library messages may carry URLs or credentials)
        log.error("fetch.connector_crashed", fetch_id=str(fetch_id), error_type=type(exc).__name__)
        raise _StopError(INTERNAL_ERROR, retryable=False) from None
    if deps.cache is None and tables:
        raise _StopError("no cache configured", retryable=False)
    rows = 0
    for series_id, parts in tables.items():
        table = pa.concat_tables(parts)
        if table.num_rows == 0:
            continue
        try:
            assert deps.cache is not None
            written = await asyncio.to_thread(
                deps.cache.write_series,
                source_id=str(plan.source_id),
                series_id=str(series_id),
                table=table,
                ts_col="ts",
                value_col="value",
                quality_col="quality",
                ingest_col=None,
            )
        except CacheError as exc:
            raise _StopError(f"cache write failed: {exc}", retryable=True) from None
        rows += written.rows
        outcome.series_rows[str(series_id)] = outcome.series_rows.get(str(series_id), 0) + written.rows
    now = deps.clock()
    covered_end = settled_end(call.end_ns, datetime_to_ns(now), plan.settle_ns)
    async with factory() as session, session.begin():
        if covered_end > call.start_ns:
            for point in call.points:
                await coverage_service.record(
                    session,
                    org_id=plan.org_id,
                    series_id=point.series_id,
                    start_ns=call.start_ns,
                    end_ns=covered_end,
                    rows=outcome.series_rows.get(str(point.series_id), 0),
                    now=now,
                )
        await session.execute(
            update(SourceFetch)
            .where(SourceFetch.id == fetch_id)
            .values(calls=SourceFetch.calls + 1, rows=SourceFetch.rows + rows)
        )
    return rows


def _checked(table: pa.Table) -> pa.Table:
    """A connector's table in `BATCH_SCHEMA` with known qualities, else a non-retryable stop."""
    try:
        table = table.select(["ts", "value", "quality"]).cast(BATCH_SCHEMA)
    except (KeyError, pa.ArrowInvalid, pa.ArrowNotImplementedError, ValueError):
        raise _StopError("connector returned a malformed batch", retryable=False) from None
    unknown = set(table.column("quality").unique().to_pylist()) - set(QUALITIES)
    if unknown:
        raise _StopError("connector returned an unknown quality", retryable=False)
    return table


async def _finish(
    factory: async_sessionmaker[AsyncSession],
    deps: FetchDeps,
    fetch_id: uuid.UUID,
    outcome: FetchOutcome,
    fetch_log: Any,
) -> FetchOutcome:
    """Store the outcome on the fetch and the source's health."""
    now = deps.clock()
    async with factory() as session, session.begin():
        fetch = await session.get(SourceFetch, fetch_id, with_for_update=True)
        if fetch is None:
            return outcome
        fetch.status = outcome.status
        fetch.error = outcome.error
        fetch.finished_at = now
        source = await session.get(Source, fetch.source_id, with_for_update=True)
        if source is not None:
            before = (source.health or {}).get("status", "unknown")
            source.health = next_health(
                source.health or {}, fetch=fetch, now=now, retryable=outcome.retryable
            )
            if source.health["status"] != before:
                log.info(
                    "source.health_changed",
                    source_id=str(source.id),
                    before=before,
                    after=source.health["status"],
                )
    if outcome.status != "succeeded":
        fetch_log.warning(
            "fetch.failed", status=outcome.status, error=outcome.error, retryable=outcome.retryable
        )
    return outcome


def next_health(
    health: dict[str, Any], *, fetch: SourceFetch, now: datetime, retryable: bool
) -> dict[str, Any]:
    """The source's health after `fetch` ended (spec 021): ok, then degraded, then failing."""
    out = dict(health)
    out["checked_at"] = now.isoformat()
    if fetch.status == "succeeded":
        out.update(status="ok", consecutive_failures=0, last_success_at=now.isoformat())
    else:
        failures = int(out.get("consecutive_failures", 0)) + 1
        out.update(
            status="failing" if failures >= FAILING_AFTER else "degraded",
            consecutive_failures=failures,
            last_error={"at": now.isoformat(), "message": fetch.error, "retryable": retryable},
        )
    if fetch.trigger != "check":
        out["last_fetch"] = {
            "id": str(fetch.id),
            "status": fetch.status,
            "finished_at": now.isoformat(),
            "rows": int(fetch.rows or 0),
        }
    return out


# Polling and pruning (cross-org, through the definer functions of migration 0008)


async def claim_due_sources(
    factory: async_sessionmaker[AsyncSession], now: datetime
) -> list[tuple[uuid.UUID, uuid.UUID, uuid.UUID]]:
    """Sources whose poll interval has passed, marked polled: (source, org, workspace)."""
    types = sorted(t for t in ("synthetic", "csv_dir", "pi_web_api", "opc_ua") if connectors.is_connector(t))
    async with factory() as session, session.begin():
        rows = await session.execute(CLAIM_DUE_SQL, {"now": now, "types": types})
        return [(r.source_id, r.org_id, r.workspace_id) for r in rows]


async def queue_poll(
    factory: async_sessionmaker[AsyncSession], source_id: uuid.UUID, now: datetime, *, inline: bool = False
) -> uuid.UUID | None:
    """A `poll` fetch over `[now - backfill_s, now)` for one claimed source (in its org's factory)."""
    async with factory() as session, session.begin():
        source = await session.get(Source, source_id)
        if source is None:
            return None
        backfill = int((source.config or {}).get("backfill_s", 86_400))
        fetch = await create_fetch(
            session, source, trigger="poll", start=now - timedelta(seconds=backfill), end=now
        )
        if not inline:
            await enqueue(session, fetch)
        return fetch.id


async def prune(factory: async_sessionmaker[AsyncSession], now: datetime | None = None) -> int:
    """Delete fetches older than 30 days; how many."""
    async with factory() as session, session.begin():
        return int((await session.execute(PRUNE_SQL, {"before": (now or utc_now()) - KEEP_FOR})).scalar_one())


async def in_flight(session: AsyncSession, source_id: uuid.UUID) -> int:
    """Fetches of the source queued or running."""
    count = await session.scalar(
        select(func.count())
        .select_from(SourceFetch)
        .where(SourceFetch.source_id == source_id, SourceFetch.status.in_(("queued", "running")))
    )
    return int(count or 0)
