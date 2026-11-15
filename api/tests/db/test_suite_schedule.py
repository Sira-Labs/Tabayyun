"""The suite scheduler (spec 027): due suites fire once, busy ones wait, downtime gives one run."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text

from tabayyun.db import for_org
from tabayyun.db.models import DEFAULT_ORG_ID, CheckSuite, Run
from tabayyun.main import create_app
from tabayyun.services import suites
from tenancy import app_settings

# Fixed clock: the suite fires at five past every hour.
T0 = datetime(2026, 3, 2, 10, 0, tzinfo=UTC)
DUE = T0.replace(minute=5)
HOURLY = "5 * * * *"


def test_next_run_follows_the_schedule():
    assert suites.next_run(HOURLY, True, T0) == DUE
    assert suites.next_run(HOURLY, True, DUE) == DUE + timedelta(hours=1)  # strictly after now
    assert suites.next_run(HOURLY, True, DUE - timedelta(microseconds=1)) == DUE
    assert suites.next_run("0 6 * * 1-5", True, datetime(2026, 3, 6, 7, tzinfo=UTC)) == datetime(
        2026, 3, 9, 6, tzinfo=UTC
    )  # Friday after six: Monday
    assert suites.next_run("30 2 31 * *", True, datetime(2026, 2, 1, tzinfo=UTC)) == datetime(
        2026, 3, 31, 2, 30, tzinfo=UTC
    )
    # Schedules are UTC whatever the zone of `now`.
    riyadh = T0.astimezone(tz=datetime.fromisoformat("2026-01-01T00:00:00+03:00").tzinfo)
    assert suites.next_run(HOURLY, True, riyadh) == DUE
    assert suites.next_run(None, True, T0) is None and suites.next_run(HOURLY, False, T0) is None


class Env:
    def __init__(self, app, client, dataset_id: str) -> None:
        self.app, self.client, self.dataset_id = app, client, dataset_id
        self.all_orgs = app.state.session_factory
        self.factory = for_org(app.state.session_factory, DEFAULT_ORG_ID)

    async def suite(self, name: str, *, at: datetime = T0, **fields) -> uuid.UUID:
        """A suite created through the API whose next run is computed as of `at`."""
        body = {"name": name, "dataset_id": self.dataset_id, "schedule": HOURLY} | fields
        r = await self.client.post("/api/suites", json=body)
        assert r.status_code == 201, r.text
        suite_id = uuid.UUID(r.json()["id"])
        async with self.factory() as session, session.begin():
            suite = await session.get(CheckSuite, suite_id)
            suite.next_run_at = suites.next_run(suite.schedule, suite.enabled, at)
        return suite_id

    async def row(self, suite_id: uuid.UUID) -> CheckSuite:
        async with self.factory() as session:
            return (await session.execute(select(CheckSuite).where(CheckSuite.id == suite_id))).scalar_one()

    async def runs(self, suite_id: uuid.UUID) -> list[Run]:
        async with self.factory() as session:
            stmt = select(Run).where(Run.suite_id == suite_id).order_by(Run.created_at, Run.id)
            return list((await session.execute(stmt)).scalars())

    async def tick(self, now: datetime) -> list[uuid.UUID]:
        return await suites.fire_due_suites(self.all_orgs, now)

    async def finish(self, run_id: uuid.UUID) -> None:
        async with self.factory() as session, session.begin():
            await session.execute(text("UPDATE runs SET status = 'succeeded' WHERE id = :id"), {"id": run_id})


@pytest.fixture
async def env(db_url, fresh_schema, tmp_path):
    fresh_schema("auto")
    # Inline jobs run the fixture's upload; the scheduler queues its runs for the worker either way.
    app = create_app(app_settings(db_url, inline_jobs=True, cache_url=str(tmp_path)))
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers={"X-Tabayyun-Request": "1"}
    ) as client:
        rows = "ts,value\n" + "".join(f"2026-01-01T{h:02d}:00:00Z,{h}\n" for h in range(24))
        r = await client.post(
            "/api/runs", files={"file": ("f.csv", rows.encode(), "text/csv")}, data={"series_id": "s"}
        )
        run = (await client.get(f"/api/runs/{r.json()['id']}")).json()
        assert run["status"] == "succeeded", run
        body = {"name": "D", "series_ids": [run["series"][0]["id"]], "window": {"last": "24h"}}
        dataset = await client.post("/api/datasets", json=body)
        assert dataset.status_code == 201, dataset.text
        yield Env(app, client, dataset.json()["id"])
    await app.state.engine.dispose()


async def test_due_suite_gets_one_run(env):
    checks = [{"id": "tby.flatline", "params": {"min_run": 20}}, {"id": "tby.spikes", "enabled": False}]
    suite_id = await env.suite("hourly", checks=checks)
    assert await env.tick(DUE - timedelta(seconds=1)) == []
    [run_id] = await env.tick(DUE)
    [run] = await env.runs(suite_id)
    assert (run.id, run.trigger, run.status, run.dataset_id) == (
        run_id,
        "suite",
        "queued",
        uuid.UUID(env.dataset_id),
    )
    assert run.checks == [
        {"id": "tby.flatline", "enabled": True, "params": {"min_run": 20}},
        {"id": "tby.spikes", "enabled": False, "params": {}},
    ]
    # The window is the dataset's at the firing time.
    assert (run.window_start, run.window_end) == (DUE - timedelta(hours=24), DUE)
    suite = await env.row(suite_id)
    assert (suite.last_run_at, suite.next_run_at) == (DUE, DUE + timedelta(hours=1))
    # Queued for the worker under the run's org.
    async with env.factory() as session:
        jobs = (
            await session.execute(
                text("SELECT args FROM procrastinate_jobs WHERE task_name = 'tabayyun.run_checks'")
            )
        ).all()
    assert [j.args for j in jobs] == [{"run_id": str(run_id), "org_id": str(DEFAULT_ORG_ID)}]
    # A minute later nothing is due; a later change to the suite leaves the queued run as it is.
    assert await env.tick(DUE + timedelta(minutes=1)) == []
    r = await env.client.patch(f"/api/suites/{suite_id}", json={"checks": [{"id": "tby.completeness"}]})
    assert r.status_code == 200, r.text
    assert [c["id"] for c in (await env.runs(suite_id))[0].checks] == ["tby.flatline", "tby.spikes"]


async def test_not_due_disabled_or_unscheduled_suites_do_not_fire(env):
    later = await env.suite("later", schedule="5 11 * * *")
    off = await env.suite("off", enabled=False)
    unscheduled = await env.suite("unscheduled", schedule=None)
    orphan = await env.suite("orphan")
    due = await env.suite("due")
    # Disabled after its next run was set, and a suite whose dataset is gone: the claim skips both.
    async with env.factory() as session, session.begin():
        await session.execute(
            text("UPDATE check_suites SET next_run_at = :t WHERE id = :id"), {"t": DUE, "id": off}
        )
        await session.execute(
            text("UPDATE check_suites SET dataset_id = NULL WHERE id = :id"), {"id": orphan}
        )
    [first] = await env.tick(DUE)
    await env.finish(first)
    assert [len(await env.runs(s)) for s in (later, off, unscheduled, orphan, due)] == [0, 0, 0, 0, 1]
    assert (await env.row(orphan)).next_run_at == DUE  # untouched until it has a dataset again
    assert len(await env.tick(DUE + timedelta(hours=1))) == 2  # `later` at 11:05, `due` again
    assert [len(await env.runs(s)) for s in (later, due)] == [1, 1 + 1]


async def test_busy_suite_is_skipped(env):
    suite_id = await env.suite("slow")
    [first] = await env.tick(DUE)
    # Still queued an hour later: no second run, and the suite waits for its next time.
    assert await env.tick(DUE + timedelta(hours=1)) == []
    suite = await env.row(suite_id)
    assert (suite.last_run_at, suite.next_run_at) == (DUE, DUE + timedelta(hours=2))
    await env.finish(first)
    assert await env.tick(DUE + timedelta(hours=1, minutes=30)) == []
    [second] = await env.tick(DUE + timedelta(hours=2))
    assert [r.id for r in await env.runs(suite_id)] == [first, second]


async def test_downtime_gives_one_run(env):
    suite_id = await env.suite("hourly")
    back = DUE + timedelta(hours=7, minutes=20)  # seven firings missed
    [run_id] = await env.tick(back)
    assert [r.id for r in await env.runs(suite_id)] == [run_id]
    suite = await env.row(suite_id)
    assert (suite.last_run_at, suite.next_run_at) == (back, DUE + timedelta(hours=8))
    await env.finish(run_id)
    assert await env.tick(back + timedelta(minutes=1)) == []


async def test_lease_refires_after_a_lost_worker(env):
    suite_id = await env.suite("hourly")
    # A worker claims the suite and dies before it starts the run.
    claimed = await suites.claim_due_suites(env.all_orgs, DUE)
    assert [c[0] for c in claimed] == [suite_id] and claimed[0][1] == DEFAULT_ORG_ID
    assert (await env.row(suite_id)).next_run_at == DUE + timedelta(minutes=10)
    assert await env.runs(suite_id) == []
    assert await env.tick(DUE + timedelta(minutes=9)) == []
    [run_id] = await env.tick(DUE + timedelta(minutes=10))
    assert [r.id for r in await env.runs(suite_id)] == [run_id]
    assert (await env.row(suite_id)).next_run_at == DUE + timedelta(hours=1)
