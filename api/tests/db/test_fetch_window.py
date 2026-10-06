"""The fetch engine against Postgres and a local cache (spec 021): a day from the synthetic
connector, idempotence, settling, partial fetches and their retry, failures, health, the
budget, polling, the job lock and pruning."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta

import pyarrow as pa
import pytest
from sqlalchemy import select, text
from structlog.testing import capture_logs

from tabayyun import connectors, secrets
from tabayyun.connectors import ConnectorError, FetchedBatch, NetPolicy, PointRef
from tabayyun.connectors.plan import Pacer
from tabayyun.connectors.synthetic import SyntheticConnector
from tabayyun.db import for_org
from tabayyun.db.models import DEFAULT_ORG_ID, DEFAULT_WORKSPACE_ID, Series, Source, SourceFetch
from tabayyun.main import create_app
from tabayyun.services import coverage as coverage_service
from tabayyun.services import fetches
from tabayyun.services.timeconv import datetime_to_ns
from tenancy import app_settings

DAY0 = datetime(2026, 9, 1, tzinfo=UTC)
NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
POINTS = [{"external_id": "flow", "base": 50, "amplitude": 10}, {"external_id": "level", "base": 2}]


class Env:
    """An app on a fresh schema with a local cache, and helpers around one synthetic source."""

    def __init__(self, app, now: datetime = NOW) -> None:
        self.app = app
        self.factory = for_org(app.state.session_factory, DEFAULT_ORG_ID)
        self.now = now
        self.deps = fetches.FetchDeps(
            cache=app.state.run_cache,
            net=NetPolicy(),
            keyring=None,
            clock=lambda: self.now,
            pacer=lambda rate: Pacer(rate, clock=lambda: 0.0, sleep=_no_sleep),
        )

    async def source(self, *, enabled: bool = True, **config) -> tuple[uuid.UUID, list[uuid.UUID]]:
        """A synthetic source with a series per configured point."""
        config.setdefault("points", POINTS)
        async with self.factory() as session, session.begin():
            source = Source(
                org_id=DEFAULT_ORG_ID,
                workspace_id=DEFAULT_WORKSPACE_ID,
                type="synthetic",
                name=f"syn-{uuid.uuid4().hex[:6]}",
                config=config,
                enabled=enabled,
            )
            session.add(source)
            await session.flush()
            ids = []
            for p in config["points"]:
                series = Series(
                    org_id=DEFAULT_ORG_ID,
                    workspace_id=DEFAULT_WORKSPACE_ID,
                    source_id=source.id,
                    external_id=p["external_id"],
                    name=p["external_id"],
                )
                session.add(series)
                await session.flush()
                ids.append(series.id)
            return source.id, ids

    async def fetch(self, source_id, start=DAY0, end=DAY0 + timedelta(days=1), **kw) -> uuid.UUID:
        async with self.factory() as session, session.begin():
            source = await session.get(Source, source_id)
            return (
                await fetches.create_fetch(session, source, trigger="manual", start=start, end=end, **kw)
            ).id

    async def run(self, fetch_id, **kw):
        return await fetches.execute_fetch(self.factory, self.deps, fetch_id, **kw)

    async def row(self, model, key):
        async with self.factory() as session:
            return await session.get(model, key)

    async def covered(self, series_id) -> list[tuple[int, int]]:
        async with self.factory() as session:
            return await coverage_service.covered_ranges(session, series_id)

    def read(self, source_id, series_id, start=DAY0, end=DAY0 + timedelta(days=1)) -> pa.RecordBatch | None:
        batches = self.app.state.run_cache.read_series(
            source_id=str(source_id),
            series_ids=[str(series_id)],
            start_ns=datetime_to_ns(start),
            end_ns=datetime_to_ns(end),
        )
        return batches.get(str(series_id))


async def _no_sleep(seconds: float) -> None:
    return None


@pytest.fixture
async def env(db_url, fresh_schema, tmp_path) -> AsyncIterator[Env]:
    fresh_schema("auto")
    app = create_app(app_settings(db_url, cache_url=str(tmp_path)))
    yield Env(app)
    await app.state.engine.dispose()


class Scripted(SyntheticConnector):
    """The synthetic connector with scripted failures: `fail[n]` is raised on the n-th call."""

    fail: dict[int, Exception] = {}  # noqa: RUF012
    calls: list[tuple[int, int, int]] = []  # noqa: RUF012

    async def fetch(
        self, points: Sequence[PointRef], start_ns: int, end_ns: int
    ) -> AsyncIterator[FetchedBatch]:
        n = len(Scripted.calls)
        Scripted.calls.append((start_ns, end_ns, len(points)))
        if n in Scripted.fail:
            raise Scripted.fail[n]
        async for batch in super().fetch(points, start_ns, end_ns):
            yield batch


@pytest.fixture
def scripted(monkeypatch):
    Scripted.fail, Scripted.calls = {}, []
    monkeypatch.setitem(connectors._REGISTRY, "synthetic", Scripted)  # noqa: SLF001
    return Scripted


# A day from the synthetic connector


async def test_a_day_into_the_cache_and_coverage(env):
    source_id, (flow, level) = await env.source()
    fetch_id = await env.fetch(source_id)
    outcome = await env.run(fetch_id)
    assert (outcome.status, outcome.calls, outcome.rows) == ("succeeded", 1, 2880)
    for series_id in (flow, level):
        assert env.read(source_id, series_id).num_rows == 1440
        assert await env.covered(series_id) == [
            (datetime_to_ns(DAY0), datetime_to_ns(DAY0 + timedelta(days=1)))
        ]
    row = await env.row(SourceFetch, fetch_id)
    assert (row.status, row.calls, row.rows, row.error) == ("succeeded", 1, 2880, None)
    assert row.started_at is not None and row.finished_at == NOW
    source = await env.row(Source, source_id)
    assert source.health["status"] == "ok" and source.health["last_fetch"]["rows"] == 2880


async def test_a_second_fetch_makes_no_calls(env, scripted):
    source_id, _ = await env.source()
    await env.run(await env.fetch(source_id))
    again = await env.run(await env.fetch(source_id))
    assert (again.status, again.calls) == ("succeeded", 0)
    assert len(scripted.calls) == 1


async def test_force_refetches_and_reads_stay_the_same(env, scripted):
    source_id, (flow, _) = await env.source()
    await env.run(await env.fetch(source_id))
    before = env.read(source_id, flow)
    forced = await env.run(await env.fetch(source_id, force=True))
    assert forced.calls == 1 and env.read(source_id, flow).equals(before)


async def test_only_the_requested_series(env):
    source_id, (flow, level) = await env.source()
    await env.run(await env.fetch(source_id, series_ids=[level]))
    assert env.read(source_id, flow) is None and env.read(source_id, level).num_rows == 1440


async def test_the_last_minutes_are_fetched_but_not_covered(env):
    source_id, (flow, _) = await env.source(settle_s=600)
    end = NOW
    await env.run(await env.fetch(source_id, start=NOW - timedelta(hours=1), end=end))
    assert env.read(source_id, flow, NOW - timedelta(hours=1), NOW).num_rows == 60
    [(lo, hi)] = await env.covered(flow)
    assert hi == datetime_to_ns(NOW - timedelta(minutes=10))


async def test_calls_follow_the_limits(env, scripted):
    source_id, _ = await env.source(max_points=1, max_span_s=6 * 3600)
    outcome = await env.run(await env.fetch(source_id))
    assert outcome.calls == 8 and all(n == 1 for _, _, n in scripted.calls)
    assert all(e - s == 6 * 3600 * 10**9 for s, e, _ in scripted.calls)


# Errors and retries


async def test_retryable_error_keeps_progress_and_the_retry_fetches_the_rest(env, scripted):
    source_id, (flow, _) = await env.source(max_span_s=6 * 3600)
    scripted.fail = {2: ConnectorError("historian busy", retryable=True)}
    fetch_id = await env.fetch(source_id)
    first = await env.run(fetch_id)
    assert (first.status, first.calls, first.retryable, first.error) == ("partial", 2, True, "historian busy")
    assert (await env.row(SourceFetch, fetch_id)).status == "partial"
    day = (datetime_to_ns(DAY0), datetime_to_ns(DAY0 + timedelta(days=1)))
    assert coverage_service.missing_ranges(await env.covered(flow), *day) == [
        (datetime_to_ns(DAY0 + timedelta(hours=12)), day[1])
    ]
    retry = await env.run(fetch_id)  # the job's retry runs the same fetch again
    assert (retry.status, retry.calls) == ("succeeded", 2)
    assert [s for s, _, _ in scripted.calls[3:]] == [
        datetime_to_ns(DAY0 + timedelta(hours=h)) for h in (12, 18)
    ]
    assert env.read(source_id, flow).num_rows == 1440


async def test_non_retryable_error_fails(env, scripted):
    source_id, _ = await env.source()
    scripted.fail = {0: connectors.AuthError("historian refused the credentials")}
    fetch_id = await env.run(await env.fetch(source_id))
    assert (fetch_id.status, fetch_id.retryable) == ("failed", False)


async def test_a_connector_crash_is_reported_without_its_message(env, scripted):
    source_id, _ = await env.source()
    scripted.fail = {0: RuntimeError("GET https://svc:hunter2@pi/ failed")}
    with capture_logs() as logs:
        outcome = await env.run(await env.fetch(source_id))
    assert (outcome.status, outcome.error) == ("failed", fetches.INTERNAL_ERROR)
    assert "hunter2" not in json.dumps(logs, default=str)
    assert any(e["event"] == "fetch.connector_crashed" and e["error_type"] == "RuntimeError" for e in logs)


async def test_a_malformed_batch_fails(env, monkeypatch):
    source_id, _ = await env.source()

    async def bad(self, points, start_ns, end_ns):
        yield FetchedBatch(points[0].series_id, pa.table({"ts": [1], "value": [1.0], "quality": ["fine"]}))

    monkeypatch.setattr(SyntheticConnector, "fetch", bad)
    outcome = await env.run(await env.fetch(source_id))
    assert (outcome.status, outcome.error) == ("failed", "connector returned an unknown quality")


async def test_health_goes_ok_degraded_failing_ok(env, scripted):
    source_id, _ = await env.source()
    statuses = []
    scripted.fail = {n: ConnectorError("down", retryable=False) for n in range(3)}
    for day in range(4):
        start = DAY0 + timedelta(days=day)
        await env.run(await env.fetch(source_id, start=start, end=start + timedelta(days=1)))
        statuses.append((await env.row(Source, source_id)).health["status"])
    assert statuses == ["degraded", "degraded", "failing", "ok"]
    health = (await env.row(Source, source_id)).health
    assert health["consecutive_failures"] == 0 and health["last_error"]["message"] == "down"


async def test_disabled_source_and_missing_key(env):
    disabled, _ = await env.source(enabled=False)
    outcome = await env.run(await env.fetch(disabled))
    assert (outcome.status, outcome.error) == ("failed", "source disabled")
    source_id, _ = await env.source()
    keyring = secrets.Keyring(b"k" * 32)
    async with env.factory() as session, session.begin():
        await secrets.store(
            session, keyring, org_id=DEFAULT_ORG_ID, source_id=source_id, payload={"token": "t"}, user_id=None
        )
    outcome = await env.run(await env.fetch(source_id))
    assert outcome.status == "failed" and outcome.error.startswith("credentials unavailable")


async def test_budget_stops_before_the_next_call(env, scripted):
    source_id, _ = await env.source(max_span_s=6 * 3600)
    ticks = iter([0.0, 0.0, 0.0, 5.0, 10.0, 10.0])
    env.deps.monotonic = lambda: next(ticks, 10.0)
    outcome = await env.run(await env.fetch(source_id), deadline=4.0)
    assert (outcome.status, outcome.calls, outcome.error) == ("partial", 2, fetches.BUDGET_SPENT)


async def test_a_fetch_runs_once(env):
    source_id, _ = await env.source()
    fetch_id = await env.fetch(source_id)
    assert (await env.run(fetch_id)).status == "succeeded"
    assert await env.run(fetch_id) is None


async def test_check(env, scripted):
    source_id, _ = await env.source()
    async with env.factory() as session, session.begin():
        source = await session.get(Source, source_id)
        check = await fetches.create_fetch(session, source, trigger="check", start=NOW, end=NOW)
    outcome = await fetches.execute_check(env.factory, env.deps, check.id)
    assert outcome.status == "succeeded" and not scripted.calls
    health = (await env.row(Source, source_id)).health
    assert health["status"] == "ok" and "last_fetch" not in health


# Jobs, polling and pruning


async def test_enqueue_locks_the_source(env):
    source_id, _ = await env.source()
    async with env.factory() as session, session.begin():
        source = await session.get(Source, source_id)
        fetch = await fetches.create_fetch(session, source, trigger="manual", start=DAY0, end=NOW)
        await fetches.enqueue(session, fetch)
    async with env.factory() as session:
        job = (
            await session.execute(
                text("SELECT task_name, lock, args FROM procrastinate_jobs WHERE queue_name = 'fetch'")
            )
        ).one()
    assert job.task_name == "tabayyun.fetch_window" and job.lock == f"source:{source_id}"
    assert job.args == {"fetch_id": str(fetch.id), "org_id": str(DEFAULT_ORG_ID)}


async def test_polling_claims_due_sources_once(env):
    # A fetch in flight counts for an hour after its (database) creation time: use the real clock.
    now = datetime.now(UTC)
    due, _ = await env.source(poll_interval_s=300, backfill_s=3600)
    unpolled, _ = await env.source()
    off, _ = await env.source(enabled=False, poll_interval_s=300)
    busy, _ = await env.source(poll_interval_s=300)
    await env.fetch(busy)  # queued: skipped
    factory = env.app.state.session_factory
    assert [c[0] for c in await fetches.claim_due_sources(factory, now)] == [due]
    assert await fetches.claim_due_sources(factory, now + timedelta(seconds=299)) == []
    assert [c[0] for c in await fetches.claim_due_sources(factory, now + timedelta(seconds=300))] == [due]
    fetch_id = await fetches.queue_poll(env.factory, due, now)
    row = await env.row(SourceFetch, fetch_id)
    assert (row.trigger, row.window_start, row.window_end) == ("poll", now - timedelta(hours=1), now)
    # The busy source's fetch is over an hour old later on: a lost worker's, so it polls again.
    assert busy in [c[0] for c in await fetches.claim_due_sources(factory, now + timedelta(hours=2))]
    assert (unpolled, off) and (await env.row(Source, unpolled)).polled_at is None


async def test_prune_keeps_thirty_days(env):
    source_id, _ = await env.source()
    old = await env.fetch(source_id)
    recent = await env.fetch(source_id)
    async with env.factory() as session, session.begin():
        await session.execute(
            text("UPDATE source_fetches SET created_at = :at WHERE id = :id"),
            {"at": NOW - timedelta(days=31), "id": old},
        )
    assert await fetches.prune(env.app.state.session_factory, NOW) == 1
    async with env.factory() as session:
        assert (await session.scalars(select(SourceFetch.id))).all() == [recent]
