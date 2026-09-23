"""Execute a queued run of either kind: an upload (spec 002) or a dataset run (spec 008).

The job carries only the run id; the run's trigger says which executor it needs.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tabayyun.db.models import Run
from tabayyun.services import dataset_runs, runs
from tabayyun.services.cache import RunCache


async def execute(
    factory: async_sessionmaker[AsyncSession], run_id: uuid.UUID, cache: RunCache | None
) -> None:
    """Run the executor matching the run's trigger. Never raises; failures land on the run."""
    async with factory() as session:
        trigger = (await session.execute(select(Run.trigger).where(Run.id == run_id))).scalar_one_or_none()
    if trigger == dataset_runs.DATASET_TRIGGER:
        await dataset_runs.execute_dataset_run(factory, run_id, cache)
    else:
        await runs.execute_run(factory, run_id, cache)
