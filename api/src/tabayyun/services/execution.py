"""Execute a queued run of either kind: an upload (spec 002) or a dataset run (spec 008),
started by a person or by a check suite (spec 027), and then evaluate the workspace's alert
rules on what it stored (spec 031).

The job carries only the run id; the run's trigger says which executor it needs.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tabayyun.alerts import delivery
from tabayyun.authz.scope import Scope
from tabayyun.db.models import Run
from tabayyun.services import dataset_runs, runs
from tabayyun.services.cache import RunCache


async def execute(
    factory: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
    cache: RunCache | None,
    run_fetch: dataset_runs.RunFetch | None = None,
    alerts: delivery.Dispatch | None = None,
) -> None:
    """Run the executor matching the run's trigger. Never raises; failures land on the run.
    With `alerts`, a run that succeeded has the alert rules evaluated, once: the evaluation
    claims the run, so a job delivered twice or two calls that raced evaluate it one time."""
    async with factory() as session:
        trigger = await session.scalar(select(Run.trigger).where(Run.id == run_id))
    if trigger in dataset_runs.DATASET_TRIGGERS:
        await dataset_runs.execute_dataset_run(factory, run_id, cache, run_fetch)
    else:
        await runs.execute_run(factory, run_id, cache)
    if alerts is None:
        return
    async with factory() as session:
        after = (
            await session.execute(select(Run.status, Run.org_id, Run.workspace_id).where(Run.id == run_id))
        ).one_or_none()
    if after is not None and after.status == "succeeded":
        scope = Scope(org_id=after.org_id, workspace_id=after.workspace_id)
        await delivery.after_run(factory, scope, run_id, alerts)
