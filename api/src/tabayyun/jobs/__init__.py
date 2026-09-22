"""Procrastinate application (ADR-0004): Postgres-backed queue shared with the API schema.

The API never opens this app; it enqueues by calling Procrastinate's defer function inside
the run's own transaction (see `services.runs.enqueue_run`). The worker process opens it:

    python -m tabayyun.jobs            # schema guard, then a worker on every queue
    procrastinate --app tabayyun.jobs.app worker
"""

from __future__ import annotations

from procrastinate import App, PsycopgConnector
from sqlalchemy.engine import make_url

from tabayyun.jobs.names import MAINTENANCE_QUEUE, REAP_STALE_RUNS_TASK, RUN_CHECKS_TASK, RUNS_QUEUE
from tabayyun.settings import Settings, get_settings

__all__ = [
    "MAINTENANCE_QUEUE",
    "REAP_STALE_RUNS_TASK",
    "RUN_CHECKS_TASK",
    "RUNS_QUEUE",
    "app",
    "libpq_conninfo",
    "make_app",
]


def libpq_conninfo(database_url: str) -> str:
    """Turn the SQLAlchemy URL (`postgresql+psycopg://…`) into the libpq URI Procrastinate wants."""
    return make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)


def make_app(settings: Settings) -> App:
    """Build the Procrastinate app; no connection is opened until the worker starts."""
    return App(connector=PsycopgConnector(conninfo=libpq_conninfo(settings.database_url)))


app = make_app(get_settings())

from tabayyun.jobs import tasks  # noqa: E402, F401  (registers the tasks on `app`)
