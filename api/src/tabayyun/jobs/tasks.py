"""Procrastinate tasks: run execution and the stale-run reaper (spec 002), invitation email
(spec 014)."""

from __future__ import annotations

import uuid

import structlog
from procrastinate import JobContext, RetryStrategy

from tabayyun.db import for_org
from tabayyun.db.models import DEFAULT_ORG_ID
from tabayyun.jobs import app, runtime
from tabayyun.jobs.names import (
    MAIL_QUEUE,
    MAINTENANCE_QUEUE,
    PRUNE_RATE_LIMITS_TASK,
    REAP_STALE_RUNS_TASK,
    RUN_CHECKS_TASK,
    RUNS_QUEUE,
    SEND_INVITATION_TASK,
)
from tabayyun.mail import MailError, SmtpConfig
from tabayyun.services import execution, rate_limits
from tabayyun.services import runs as runs_service
from tabayyun.services.admin import invitations
from tabayyun.settings import get_settings

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


@app.periodic(cron="*/10 * * * *")
@app.task(queue=MAINTENANCE_QUEUE, name=PRUNE_RATE_LIMITS_TASK, retry=False)
async def prune_rate_limits(timestamp: int) -> None:
    """Every 10 minutes: drop rate-limit windows older than a day (spec 015)."""
    pruned = await rate_limits.prune(runtime.session_factory())
    if pruned:
        log.info("rate.pruned", count=pruned, tick=timestamp)


MAIL_ATTEMPTS = 3


@app.task(
    queue=MAIL_QUEUE,
    name=SEND_INVITATION_TASK,
    pass_context=True,
    retry=RetryStrategy(max_attempts=MAIL_ATTEMPTS, exponential_wait=30, retry_exceptions=[MailError]),
)
async def send_invitation_email(context: JobContext, invitation_id: str, org_id: str) -> None:
    """Send an invitation's email; three attempts with exponential backoff, the last one recorded."""
    settings = get_settings()
    config = SmtpConfig.from_settings(settings)
    if config is None:
        # The api queued it with its own settings; this worker has none, so it will not go out.
        log.warning("mail.not_configured", invitation_id=invitation_id)
        await invitations.mark_not_configured(
            for_org(runtime.session_factory(), uuid.UUID(org_id)), uuid.UUID(invitation_id)
        )
        return
    await invitations.deliver(
        for_org(runtime.session_factory(), uuid.UUID(org_id)),
        config,
        uuid.UUID(invitation_id),
        public_url=settings.public_url,
        final_attempt=context.job.attempts + 1 >= MAIL_ATTEMPTS,
    )
