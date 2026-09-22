"""Procrastinate tasks: run execution and the stale-run reaper (spec 002)."""

from __future__ import annotations

import uuid

import structlog

from tabayyun.jobs import app, runtime
from tabayyun.jobs.names import MAINTENANCE_QUEUE, REAP_STALE_RUNS_TASK, RUN_CHECKS_TASK, RUNS_QUEUE
from tabayyun.services import runs as runs_service

log = structlog.get_logger()


@app.task(queue=RUNS_QUEUE, name=RUN_CHECKS_TASK, retry=False)
async def run_checks_job(run_id: str) -> None:
    """Execute one queued run; a failure is recorded on the run, never retried."""
    await runs_service.execute_run(runtime.session_factory(), uuid.UUID(run_id))


@app.periodic(cron="*/10 * * * *")
@app.task(queue=MAINTENANCE_QUEUE, name=REAP_STALE_RUNS_TASK, retry=False)
async def reap_stale_runs(timestamp: int) -> None:
    """Every 10 minutes: runs `running` for longer than the stale limit become `failed`."""
    reaped = await runs_service.reap_stale_runs(runtime.session_factory())
    if reaped:
        log.warning("runs.reaped", count=reaped, tick=timestamp)
