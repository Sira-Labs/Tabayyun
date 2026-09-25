"""Worker entry point: `python -m tabayyun.jobs`.

Refuses to start on a schema that is not at head (exit code 3, like the API), then runs a
Procrastinate worker on every queue, including the periodic reaper.
"""

from __future__ import annotations

import asyncio

import structlog

from tabayyun import __version__
from tabayyun.db import guard_schema, make_engine
from tabayyun.jobs import app, runtime
from tabayyun.settings import get_settings

log = structlog.get_logger()


async def main() -> None:
    """Guard the schema, then serve jobs until the process is stopped."""
    settings = get_settings()
    engine = make_engine(settings)
    try:
        revision = await guard_schema(engine)
    finally:
        await engine.dispose()
    if revision is None:
        raise SystemExit("worker: database unreachable, not starting")
    log.info(
        "worker.start",
        version=__version__,
        commit=settings.commit,
        revision=revision,
        concurrency=settings.worker_concurrency,
    )
    try:
        async with app.open_async():
            await app.run_worker_async(concurrency=settings.worker_concurrency)
    finally:
        await runtime.dispose()
        log.info("worker.stop")


if __name__ == "__main__":
    asyncio.run(main())
