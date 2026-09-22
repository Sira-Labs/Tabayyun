"""Worker-side helpers: the stale-run reaper and the task registration (spec 002)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from tabayyun.db import make_engine, make_session_factory
from tabayyun.db.models import DEFAULT_ORG_ID, DEFAULT_WORKSPACE_ID, Run, Upload
from tabayyun.jobs import app as jobs_app
from tabayyun.jobs import libpq_conninfo
from tabayyun.jobs.names import REAP_STALE_RUNS_TASK, RUN_CHECKS_TASK
from tabayyun.services import runs as runs_service
from tabayyun.settings import Settings


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
    engine = make_engine(Settings(env="test", database_url=db_url))
    factory = make_session_factory(engine)
    try:
        stale, fresh, done = _run("running", 45), _run("running", 5), _run("succeeded", 60)
        async with factory() as session, session.begin():
            session.add_all([stale, fresh, done])
            await session.flush()
            session.add(Upload(run_id=stale.id, filename="f.csv", size_bytes=1, data=b"x", params={}))
        assert await runs_service.reap_stale_runs(factory) == 1
        async with factory() as session:
            reaped = await session.get(Run, stale.id)
            assert (
                reaped is not None and reaped.status == "failed" and reaped.error == runs_service.WORKER_LOST
            )
            assert reaped.finished_at is not None
            assert (await session.get(Run, fresh.id)).status == "running"
            assert (await session.get(Run, done.id)).status == "succeeded"
            assert await session.get(Upload, stale.id) is None
        assert await runs_service.reap_stale_runs(factory) == 0
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


def test_unused_uuid_helper_is_valid():
    """Sanity: run ids are UUID4 strings on the wire."""
    assert uuid.UUID(str(uuid.uuid4())).version == 4
