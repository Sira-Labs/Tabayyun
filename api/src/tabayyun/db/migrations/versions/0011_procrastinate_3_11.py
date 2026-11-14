"""Procrastinate 3.11: `procrastinate_fetch_job_v2` skips the lock subquery for unlocked jobs.

Procrastinate 3.11.0 ships one schema migration since the 3.9.0 snapshot of revision 0002,
`03.11.00_01_pre_fetch_job_skip_null_lock.sql`. It replaces the function workers fetch jobs
with: a job without a lock is accepted before the "same lock" subquery is considered, which
kept Postgres from planning a nested loop over every other job. Same signature and result,
so workers of either version run against either definition and the order of deploy and
migration does not matter.

`sql/procrastinate_3.11.0_fetch_job_skip_null_lock.sql` is a copy of that file. The downgrade
puts back the 3.9.0 definition, read from the snapshot revision 0002 applies.

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-14 22:30:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"
MIGRATION_3_11 = SQL_DIR / "procrastinate_3.11.0_fetch_job_skip_null_lock.sql"
SCHEMA_3_9 = SQL_DIR / "procrastinate_schema_3.9.0.sql"

FETCH_JOB_HEADER = "CREATE FUNCTION procrastinate_fetch_job_v2("
FUNCTION_END = "$$;"


def function_block(schema: str, header: str = FETCH_JOB_HEADER) -> str:
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
    """Replace `procrastinate_fetch_job_v2` with Procrastinate 3.11's definition."""
    _execute_raw(MIGRATION_3_11.read_text())


def downgrade() -> None:
    """Put back the 3.9.0 definition of `procrastinate_fetch_job_v2`."""
    old = function_block(SCHEMA_3_9.read_text())
    _execute_raw(old.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1))
