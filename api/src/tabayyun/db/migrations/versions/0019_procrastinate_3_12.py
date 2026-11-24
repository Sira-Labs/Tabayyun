"""Procrastinate 3.12: lock modes, a retry that closes a job it cannot requeue, a lock index.

Procrastinate 3.12.0 ships five schema migrations since 3.11. This revision runs the three
"pre" ones, which old and new code both work with, from copies under `sql/`:

- `01_add_lock_mode`: the `lock_mode` column and type, `procrastinate_defer_jobs_v2`,
  `procrastinate_defer_periodic_job_v3`, and a `procrastinate_fetch_job_v2` that lets a
  waiting 'mutex' job step aside until it is due. The functions 3.11 calls stay.
- `02_retry_job_fail_on_queueing_lock_conflict`: `procrastinate_retry_job_v2` fails a running
  job whose retry collides with a queued one instead of raising and keeping its lock.
- `03_add_todo_doing_lock_index`: an index on `lock` for the lock check of the fetch.

The two "post" migrations (dropping `procrastinate_defer_jobs_v1`, its type,
`procrastinate_defer_periodic_job_v2` and the `(id, lock)` index) are NOT run here. The api
migrates when it starts, while the worker of the previous release may still run and defer
jobs through those functions; they go in a later revision, once every deployment runs 3.12.

The index is built concurrently, outside a transaction, and first: it does not depend on the
other two files, and an interrupted build then leaves nothing but an invalid index, which
the next run drops and builds again. The two scripts and the revision stamp commit together.

The downgrade puts back the 3.11 state: the functions from the 3.9.0 snapshot and the 3.11
migration, without the column, the types and the index. Jobs keep everything but their
lock mode, so downgrade only once no 3.12 code runs.

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-24 22:45:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"
ADD_LOCK_MODE = SQL_DIR / "procrastinate_3.12.0_01_add_lock_mode.sql"
RETRY_JOB = SQL_DIR / "procrastinate_3.12.0_02_retry_job_fail_on_queueing_lock_conflict.sql"
LOCK_INDEX = SQL_DIR / "procrastinate_3.12.0_03_add_todo_doing_lock_index.sql"
MIGRATION_3_11 = SQL_DIR / "procrastinate_3.11.0_fetch_job_skip_null_lock.sql"
SCHEMA_3_9 = SQL_DIR / "procrastinate_schema_3.9.0.sql"

INDEX = "procrastinate_jobs_todo_doing_lock_idx_v1"
# indisvalid of the index in the migration's schema; no row when there is no such index.
INDEX_VALID = sa.text(
    "SELECT pg_index.indisvalid FROM pg_index JOIN pg_class ON pg_class.oid = pg_index.indexrelid "
    "WHERE pg_class.relname = :name AND pg_class.relnamespace = current_schema()::regnamespace"
)
FUNCTION_END = "$$;"
# What the 3.11 schema defines differently, by the line its statement starts with in the
# 3.9.0 snapshot. `procrastinate_fetch_job_v2` comes from the 3.11 migration instead.
RESTORED_FROM_3_9 = (
    "CREATE FUNCTION procrastinate_defer_periodic_job_v2(",
    "CREATE FUNCTION procrastinate_retry_job_v2(",
)
DROP_3_12 = """
DROP FUNCTION procrastinate_defer_periodic_job_v3(
    character varying, character varying, procrastinate_lock_mode, character varying,
    character varying, integer, character varying, bigint, jsonb
);
DROP FUNCTION procrastinate_defer_jobs_v2(procrastinate_job_to_defer_v2[]);
DROP TYPE procrastinate_job_to_defer_v2;
ALTER TABLE procrastinate_jobs DROP COLUMN lock_mode;
DROP TYPE procrastinate_lock_mode;
"""


def function_block(schema: str, header: str) -> str:
    """The `CREATE FUNCTION` statement starting at `header`, up to its closing `$$;` line."""
    lines = schema.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.startswith(header))
        end = next(i for i in range(start, len(lines)) if lines[i].strip() == FUNCTION_END)
    except StopIteration:
        raise ValueError(f"no complete function starting with {header!r} in the schema") from None
    return "\n".join(lines[start : end + 1]) + "\n"


def _execute_raw(sql: str) -> None:
    """Run a script on the migration connection without bind processing (it contains `%`)."""
    dbapi: Any = op.get_bind().connection.dbapi_connection
    with dbapi.cursor() as cursor:
        cursor.execute(sql)


def upgrade() -> None:
    """Build the lock index, then install the lock modes and the new retry function."""
    with op.get_context().autocommit_block():
        valid = op.get_bind().execute(INDEX_VALID, {"name": INDEX}).scalar()
        if valid is False:
            # What an interrupted concurrent build leaves behind.
            op.execute(f"DROP INDEX CONCURRENTLY {INDEX}")
        if not valid:
            _execute_raw(LOCK_INDEX.read_text())
    _execute_raw(ADD_LOCK_MODE.read_text())
    _execute_raw(RETRY_JOB.read_text())


def downgrade() -> None:
    """Put back the 3.11 functions and drop what 3.12 added."""
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}")
    snapshot = SCHEMA_3_9.read_text()
    for header in RESTORED_FROM_3_9:
        old = function_block(snapshot, header)
        _execute_raw(old.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1))
    _execute_raw(MIGRATION_3_11.read_text())
    _execute_raw(DROP_3_12)
