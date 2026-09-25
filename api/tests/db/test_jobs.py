"""Worker-side helpers: the stale-run reaper and the task registration (spec 002)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, text

from synth_csv import faulty_csv
from tabayyun.authz import Principal, get_principal, get_workspace_id
from tabayyun.db import for_org, make_engine, make_session_factory
from tabayyun.db.models import DEFAULT_ORG_ID, DEFAULT_WORKSPACE_ID, Run, Upload
from tabayyun.jobs import app as jobs_app
from tabayyun.jobs import libpq_conninfo, make_app, tasks, worker_application_name
from tabayyun.jobs.names import REAP_STALE_RUNS_TASK, RUN_CHECKS_TASK
from tabayyun.main import create_app
from tabayyun.services import runs as runs_service
from tenancy import app_settings


def _run(status: str, started_minutes_ago: int) -> Run:
    return Run(
        org_id=DEFAULT_ORG_ID,
        workspace_id=DEFAULT_WORKSPACE_ID,
        trigger="upload",
        status=status,
        started_at=datetime.now(UTC) - timedelta(minutes=started_minutes_ago),
        stats={},
        created_at=datetime.now(UTC),
    )


async def test_reaper_marks_stale_runs_failed(db_url, fresh_schema):
    """Runs running for longer than the limit fail with `worker lost`; fresh ones are untouched."""
    fresh_schema("auto")
    engine = make_engine(app_settings(db_url))
    # The reaper spans orgs: it gets a factory without an org, the fixtures one with.
    plain = make_session_factory(engine)
    factory = for_org(plain, DEFAULT_ORG_ID)
    try:
        stale, fresh, done = _run("running", 45), _run("running", 5), _run("succeeded", 60)
        async with factory() as session, session.begin():
            session.add_all([stale, fresh, done])
            await session.flush()
            session.add(
                Upload(
                    run_id=stale.id,
                    org_id=DEFAULT_ORG_ID,
                    filename="f.csv",
                    size_bytes=1,
                    data=b"x",
                    params={},
                )
            )
        assert await runs_service.reap_stale_runs(plain) == 1
        async with factory() as session:
            reaped = await session.get(Run, stale.id)
            assert (
                reaped is not None and reaped.status == "failed" and reaped.error == runs_service.WORKER_LOST
            )
            assert reaped.finished_at is not None
            assert (await session.get(Run, fresh.id)).status == "running"
            assert (await session.get(Run, done.id)).status == "succeeded"
            assert await session.get(Upload, stale.id) is None
        assert await runs_service.reap_stale_runs(plain) == 0
    finally:
        await engine.dispose()


def test_tasks_registered_on_the_app():
    """Both tasks exist under their stable names; the reaper is periodic."""
    assert RUN_CHECKS_TASK in jobs_app.tasks
    assert REAP_STALE_RUNS_TASK in jobs_app.tasks
    assert jobs_app.tasks[RUN_CHECKS_TASK].queue == "runs"


def test_libpq_conninfo_strips_the_sqlalchemy_driver():
    """Procrastinate needs a plain libpq URI."""
    assert (
        libpq_conninfo("postgresql+psycopg://u:p%40ss@db:5432/tabayyun")
        == "postgresql://u:p%40ss@db:5432/tabayyun"
    )


def test_worker_application_name_carries_the_commit():
    """The worker names its connections after the commit it runs, when it knows it."""
    assert worker_application_name("c0ffee1") == "tabayyun-worker/c0ffee1"
    assert worker_application_name(None) == "tabayyun-worker"


async def test_version_lists_the_commits_of_connected_workers(db_url, fresh_schema):
    """A connected worker's commit shows up in /api/version (the release workflow checks it)."""
    fresh_schema("auto")
    worker = make_app(app_settings(db_url, commit="c0ffee1"))
    api = create_app(app_settings(db_url))
    try:
        async with worker.open_async():
            await worker.connector.execute_query_one_async("SELECT 1 AS one")
            async with AsyncClient(transport=ASGITransport(app=api), base_url="http://test") as c:
                workers = (await c.get("/api/version")).json()["workers"]
        assert "c0ffee1" in workers
    finally:
        await api.state.engine.dispose()


def test_unused_uuid_helper_is_valid():
    """Sanity: run ids are UUID4 strings on the wire."""
    assert uuid.UUID(str(uuid.uuid4())).version == 4


ORG_B = uuid.UUID("00000000-0000-0000-0000-00000000000b")
WORKSPACE_B = uuid.UUID("00000000-0000-0000-0000-0000000000b2")
USER_B = uuid.UUID("00000000-0000-0000-0000-0000000000b3")


def _org_b(db_url: str) -> None:
    """A second org with a workspace and an owner, inserted as the table owner."""
    owner = create_engine(db_url)
    try:
        with owner.begin() as conn:
            conn.execute(text("INSERT INTO orgs (id, name) VALUES (:o, 'b')"), {"o": ORG_B})
            conn.execute(
                text("INSERT INTO workspaces (id, org_id, name) VALUES (:w, :o, 'b')"),
                {"w": WORKSPACE_B, "o": ORG_B},
            )
            conn.execute(
                text("INSERT INTO users (id, email, display_name) VALUES (:u, 'b@example.test', 'b')"),
                {"u": USER_B},
            )
            conn.execute(
                text("INSERT INTO org_memberships (org_id, user_id, role) VALUES (:o, :u, 'owner')"),
                {"o": ORG_B, "u": USER_B},
            )
    finally:
        owner.dispose()


async def _queue_run(app, csv: bytes) -> str:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/runs", files={"file": ("f.csv", csv, "text/csv")}, data={"series_id": "w"})
        assert r.status_code == 202, r.text
        return r.json()["id"]


async def _status(app, run_id: str) -> int | str:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get(f"/api/runs/{run_id}")
        return r.json()["status"] if r.status_code == 200 else r.status_code


@pytest.mark.parametrize("org", ["b", "legacy"])
async def test_job_runs_in_its_org(db_url, fresh_schema, tmp_path, monkeypatch, org):
    """The worker runs a job in the org it names; a job without one runs in the default org."""
    fresh_schema("auto")
    _org_b(db_url)
    settings = app_settings(db_url, inline_jobs=False, cache_url=str(tmp_path))
    app_a, app_b = create_app(settings), create_app(settings)
    app_b.dependency_overrides[get_principal] = lambda: Principal(user_id=USER_B, org_id=ORG_B)
    app_b.dependency_overrides[get_workspace_id] = lambda: WORKSPACE_B
    try:
        owner_app, other_app = (app_b, app_a) if org == "b" else (app_a, app_b)
        run_id = await _queue_run(owner_app, faulty_csv(["gap"]))
        monkeypatch.setattr(tasks.runtime, "session_factory", lambda: owner_app.state.session_factory)
        monkeypatch.setattr(tasks.runtime, "run_cache", lambda: owner_app.state.run_cache)
        await tasks.run_checks_job.func(run_id, org_id=str(ORG_B) if org == "b" else None)
        assert await _status(owner_app, run_id) == "succeeded"
        assert await _status(other_app, run_id) == 404
    finally:
        await app_a.state.engine.dispose()
        await app_b.state.engine.dispose()


async def test_reaper_spans_orgs_while_the_app_login_cannot(db_url, fresh_schema):
    """The reaper fails stale runs of every org; the app login alone cannot even see them."""
    fresh_schema("auto")
    _org_b(db_url)
    owner = create_engine(db_url)
    stale = datetime.now(UTC) - timedelta(minutes=45)
    try:
        with owner.begin() as conn:
            for org, ws in ((DEFAULT_ORG_ID, DEFAULT_WORKSPACE_ID), (ORG_B, WORKSPACE_B)):
                conn.execute(
                    text(
                        "INSERT INTO runs (id, org_id, workspace_id, trigger, status, started_at) "
                        "VALUES (:id, :org, :ws, 'upload', 'running', :at)"
                    ),
                    {"id": uuid.uuid4(), "org": org, "ws": ws, "at": stale},
                )
    finally:
        owner.dispose()
    engine = make_engine(app_settings(db_url))
    plain = make_session_factory(engine)
    try:
        async with plain() as session, session.begin():
            result = await session.execute(text("UPDATE runs SET status = 'failed' WHERE status = 'running'"))
            assert result.rowcount == 0
            await session.rollback()
        assert await runs_service.reap_stale_runs(plain) == 2
    finally:
        await engine.dispose()
