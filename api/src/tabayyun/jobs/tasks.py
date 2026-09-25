"""Procrastinate tasks: run execution and the stale-run reaper (spec 002)."""

from __future__ import annotations

import uuid

import structlog

from tabayyun.db import for_org
from tabayyun.db.models import DEFAULT_ORG_ID
from tabayyun.jobs import app, runtime
from tabayyun.jobs.names import MAINTENANCE_QUEUE, REAP_STALE_RUNS_TASK, RUN_CHECKS_TASK, RUNS_QUEUE
from tabayyun.services import execution
from tabayyun.services import runs as runs_service

log = structlog.get_logger()


@app.task(queue=RUNS_QUEUE, name=RUN_CHECKS_TASK, retry=False)
async def run_checks_job(run_id: str, org_id: str | None = None) -> None:
    """Execute one queued run (upload or dataset); a failure is recorded on the run, never retried.

    Every transaction runs in the run's org (spec 007). A job queued before 0004 carries no
    org and belongs to the default org, which held all data then.
    """
    org = DEFAULT_ORG_ID if org_id is None else uuid.UUID(org_id)
    factory = for_org(runtime.session_factory(), org)
    await execution.execute(factory, uuid.UUID(run_id), runtime.run_cache())


@app.periodic(cron="*/10 * * * *")
@app.task(queue=MAINTENANCE_QUEUE, name=REAP_STALE_RUNS_TASK, retry=False)
async def reap_stale_runs(timestamp: int) -> None:
    """Every 10 minutes: runs `running` for longer than the stale limit become `failed`."""
    reaped = await runs_service.reap_stale_runs(runtime.session_factory())
    if reaped:
        log.warning("runs.reaped", count=reaped, tick=timestamp)
