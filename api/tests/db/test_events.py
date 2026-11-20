"""Live events against the database (spec 030): what publishes, what the listener hears and
what the stream of a workspace carries."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import psycopg
from sqlalchemy import text

from admin_support import BOB, MIA, NED, ORG_B, WORKSPACE_B
from synth_csv import faulty_csv
from tabayyun import events
from tabayyun.authz.scope import Scope
from tabayyun.db import for_org
from tabayyun.db.models import DEFAULT_ORG_ID, DEFAULT_WORKSPACE_ID
from tabayyun.events.broker import Broker
from tabayyun.events.listener import APPLICATION_NAME, Listener
from tabayyun.jobs import libpq_conninfo
from tabayyun.services import runs as runs_service
from tenancy import app_url, org_session

SCOPE = Scope(org_id=DEFAULT_ORG_ID, workspace_id=DEFAULT_WORKSPACE_ID)
OTHER_WORKSPACE = uuid.UUID("00000000-0000-0000-0000-0000000000e2")


@asynccontextmanager
async def heard(db_url: str) -> AsyncIterator[list[events.Event]]:
    """Everything published on the channel while the block runs, read after a sentinel."""
    conninfo = libpq_conninfo(app_url(db_url))
    got: list[events.Event] = []
    async with await psycopg.AsyncConnection.connect(conninfo, autocommit=True) as conn:
        await conn.execute(f"LISTEN {events.CHANNEL}")
        yield got
        await conn.execute("SELECT pg_notify(%s, %s)", (events.CHANNEL, "end"))
        async for notification in conn.notifies(timeout=5):
            if notification.payload == "end":
                break
            got.append(events.Event.decode(notification.payload))


async def _post_run(client, csv: bytes) -> str:
    data = {"series_id": "demo", "unit": "m3/h", "quality_col": "quality"}
    r = await client.post("/api/runs", files={"file": ("f.csv", csv, "text/csv")}, data=data)
    assert r.status_code == 202, r.text
    return r.json()["id"]


async def test_run_publishes_completed_and_created(admin_env, db_url):
    """A succeeded run tells its status and its new findings; the same upload again finds
    nothing new and says only that it completed. An upload's cache and coverage are stored
    after the run completed, so `run.updated` follows each: a page that reloaded on
    `run.completed` reloads once more."""
    async with admin_env.client() as c, heard(db_url) as got:
        first = await _post_run(c, faulty_csv(["gap", "flatline"]))
        second = await _post_run(c, faulty_csv(["gap", "flatline"]))
    new = admin_env.scalar("SELECT (stats->>'n_findings_new')::int FROM runs WHERE id = :id", id=first)
    assert new > 0
    assert admin_env.scalar("SELECT stats->'cache'->>'written' FROM runs WHERE id = :id", id=first) == "true"
    assert [(e.type, e.data) for e in got] == [
        (events.RUN_COMPLETED, {"run_id": first, "status": "succeeded", "n_findings_new": new}),
        (events.FINDING_CREATED, {"run_id": first, "count": new}),
        (events.RUN_UPDATED, {"run_id": first}),
        (events.RUN_COMPLETED, {"run_id": second, "status": "succeeded", "n_findings_new": 0}),
        (events.RUN_UPDATED, {"run_id": second}),
    ]
    assert {(e.org_id, e.workspace_id) for e in got} == {(DEFAULT_ORG_ID, DEFAULT_WORKSPACE_ID)}


async def test_failed_run_publishes_completed(admin_env, db_url):
    """A run that fails says so, once: failing it again changes nothing and tells nobody."""
    run_id = uuid.uuid4()
    admin_env.sql(
        "INSERT INTO runs (id, org_id, workspace_id, trigger, status) "
        "VALUES (:id, :o, :w, 'upload', 'queued')",
        id=run_id,
        o=DEFAULT_ORG_ID,
        w=DEFAULT_WORKSPACE_ID,
    )
    async with admin_env.client():
        factory = for_org(admin_env.apps[-1].state.session_factory, DEFAULT_ORG_ID)
        async with heard(db_url) as got:
            await runs_service.mark_failed(factory, run_id, "core error: boom")
            await runs_service.mark_failed(factory, run_id, "again")
    assert admin_env.sql("SELECT status, error FROM runs WHERE id = :id", id=run_id) == [
        ("failed", "core error: boom")
    ]
    assert [(e.type, e.data) for e in got] == [
        (events.RUN_COMPLETED, {"run_id": str(run_id), "status": "failed", "n_findings_new": 0})
    ]


async def test_rolled_back_run_publishes_nothing(admin_env, db_url):
    """An event leaves with its transaction's commit and not at all with its rollback."""
    async with admin_env.client() as c:
        app = admin_env.apps[-1]
        async with heard(db_url) as got:
            async with org_session(app) as session:
                await events.publish_run_completed(session, SCOPE, uuid.uuid4(), "succeeded", new_findings=2)
                await session.rollback()
            kept = uuid.uuid4()
            async with org_session(app) as session, session.begin():
                await events.publish_run_completed(session, SCOPE, kept, "failed")
        assert (await c.get("/healthz")).status_code == 200
    assert [e.data["run_id"] for e in got] == [str(kept)]


async def test_reaper_publishes_completed(admin_env, db_url):
    """A run the reaper fails is told like any other failed run (migration 0016)."""
    run_id = uuid.uuid4()
    admin_env.sql(
        "INSERT INTO runs (id, org_id, workspace_id, trigger, status, started_at) "
        "VALUES (:id, :o, :w, 'upload', 'running', now() - interval '2 hours')",
        id=run_id,
        o=DEFAULT_ORG_ID,
        w=DEFAULT_WORKSPACE_ID,
    )
    async with admin_env.client():
        app = admin_env.apps[-1]
        async with heard(db_url) as got:
            assert await runs_service.reap_stale_runs(app.state.session_factory) == 1
            assert await runs_service.reap_stale_runs(app.state.session_factory) == 0
    assert [(e.type, e.org_id, e.workspace_id, e.data) for e in got] == [
        (
            events.RUN_COMPLETED,
            DEFAULT_ORG_ID,
            DEFAULT_WORKSPACE_ID,
            {"run_id": str(run_id), "status": "failed", "n_findings_new": 0},
        )
    ]
    assert got[0].at.tzinfo is not None


async def test_listener_delivers_and_resyncs_after_reconnect(admin_env, db_url):
    """The listener tells the streams that were open before it listened to reload, hands
    events to the broker, drops what is not an event, and after losing its connection comes
    back and tells the streams to reload again."""
    broker = Broker(max_streams=5)
    listener = Listener(libpq_conninfo(app_url(db_url)), broker, backoff_first_s=0.05)
    subscription = broker.subscribe(SCOPE)
    listener.ensure_started()
    listener.ensure_started()
    try:
        assert await listener.listening(5)
        # The stream was there before the channel was listened on: it may have missed events.
        assert subscription.queue.get_nowait().type == events.RESYNC
        async with admin_env.client():
            app = admin_env.apps[-1]
            first = uuid.uuid4()
            async with org_session(app) as session, session.begin():
                await session.execute(text("SELECT pg_notify(:c, 'not an event')"), {"c": events.CHANNEL})
                await events.publish_run_completed(session, SCOPE, first, "succeeded")
            event = await asyncio.wait_for(subscription.queue.get(), 5)
            assert (event.type, event.data["run_id"]) == (events.RUN_COMPLETED, str(first))

            killed = admin_env.scalar(
                "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity WHERE application_name = :n",
                n=APPLICATION_NAME,
            )
            assert killed == 1
            assert (await asyncio.wait_for(subscription.queue.get(), 5)).type == events.RESYNC
            assert await listener.listening(5)

            second = uuid.uuid4()
            async with org_session(app) as session, session.begin():
                await events.publish_run_completed(session, SCOPE, second, "failed")
            event = await asyncio.wait_for(subscription.queue.get(), 5)
            assert event.data == {"run_id": str(second), "status": "failed", "n_findings_new": 0}
    finally:
        await listener.stop()
    assert (
        admin_env.scalar(
            "SELECT count(*) FROM pg_stat_activity WHERE application_name = :n", n=APPLICATION_NAME
        )
        == 0
    )


async def test_route_refusals(admin_env):
    """Who may not have a stream: no role in the workspace, another org's workspace, a
    malformed workspace, and anyone once the process serves as many streams as it may."""
    async with admin_env.client(NED) as c:
        assert (await c.get("/api/events")).status_code == 404
    async with admin_env.client(BOB, org=ORG_B) as c:
        assert (await c.get(f"/api/events?workspace={DEFAULT_WORKSPACE_ID}")).status_code == 404
    async with admin_env.client(MIA) as c:
        bad = await c.get("/api/events?workspace=nope")
        assert (bad.status_code, bad.json()) == (400, {"detail": "invalid_workspace"})
        assert (await c.get(f"/api/events?workspace={WORKSPACE_B}")).status_code == 404
    async with admin_env.client(MIA, events_max_streams=1) as c:
        app = admin_env.apps[-1]
        held = app.state.event_broker.subscribe(SCOPE)
        full = await c.get("/api/events")
        assert (full.status_code, full.json()) == (503, {"detail": "too_many_streams"})
        assert full.headers["Retry-After"] == "30"
        # Nothing was started for a refused stream, and the place comes back with the stream.
        assert app.state.event_broker.streams == 1
        app.state.event_broker.unsubscribe(held)
        assert app.state.event_broker.streams == 0


async def test_route_streams_the_workspaces_events(admin_env):
    """A viewer's stream carries the events of its workspace and nothing of another workspace
    or org, keeps no database connection, ends at its maximum age and frees its place."""
    admin_env.sql(
        "INSERT INTO workspaces (id, org_id, name) VALUES (:id, :o, 'other')",
        id=OTHER_WORKSPACE,
        o=DEFAULT_ORG_ID,
    )
    mine, elsewhere, foreign = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    seen: dict[str, int] = {}

    async with admin_env.client(MIA, events_max_age_s=1.5, events_heartbeat_s=0.2) as c:
        app = admin_env.apps[-1]

        async def publish_while_open() -> None:
            """Once the stream is open and the listener hears, publish to three workspaces."""
            for _ in range(100):
                if app.state.event_broker.streams == 1 and await app.state.event_listener.listening(0.05):
                    break
                await asyncio.sleep(0.02)
            seen["streams"] = app.state.event_broker.streams
            seen["connections"] = app.state.engine.pool.checkedout()
            async with org_session(app) as session, session.begin():
                await events.publish_run_completed(session, SCOPE, mine, "succeeded", new_findings=4)
                other = Scope(org_id=DEFAULT_ORG_ID, workspace_id=OTHER_WORKSPACE)
                await events.publish_run_completed(session, other, elsewhere, "succeeded", new_findings=1)
            async with org_session(app, ORG_B) as session, session.begin():
                await events.publish_run_completed(
                    session, Scope(org_id=ORG_B, workspace_id=WORKSPACE_B), foreign, "failed"
                )

        publisher = asyncio.create_task(publish_while_open())
        response = await c.get(f"/api/events?workspace={DEFAULT_WORKSPACE_ID}")
        await publisher
        await app.state.event_listener.stop()

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
    assert seen == {"streams": 1, "connections": 0}
    assert app.state.event_broker.streams == 0

    body = response.text
    assert body.startswith("retry: 5000\n\n: connected\n\n")
    assert ": keep-alive\n\n" in body
    assert body.count("event: resync\n") == 1
    frames = [f for f in body.split("\n\n") if f.startswith("event: ") and "resync" not in f]
    told = [(f.split("\n")[0], json.loads(f.split("data: ", 1)[1])) for f in frames]
    assert [(name, data["run_id"]) for name, data in told] == [
        ("event: run.completed", str(mine)),
        ("event: finding.created", str(mine)),
    ]
    assert told[0][1]["status"] == "succeeded" and told[1][1]["count"] == 4
    assert str(elsewhere) not in body and str(foreign) not in body
    assert "org_id" not in body and "workspace_id" not in body
