"""Procrastinate job queue schema (spec 002, ADR-0004).

Applies the snapshot `sql/procrastinate_schema_3.9.0.sql`, copied from Procrastinate 3.9.0's
`schema.sql`, so that one `migrate upgrade head` creates the queue tables next to ours and
this revision keeps producing the same schema whatever Procrastinate version is installed
later. The file contains literal percent signs and dollar-quoted function bodies, so it runs
through the driver cursor without parameter formatting. Autogenerate ignores
`procrastinate_*` tables (see env.py).

Upgrading Procrastinate later: add a new revision that executes the SQL files it ships under
`procrastinate/sql/migrations/` between 3.9.0 and the new version, and update the snapshot
test in `tests/test_migration_helpers.py`.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-22 08:05:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA_SNAPSHOT = Path(__file__).resolve().parent.parent / "sql" / "procrastinate_schema_3.9.0.sql"

# Drops every table, function and type Procrastinate created, whatever its version.
DROP_PROCRASTINATE = """
DO $$
DECLARE r record;
BEGIN
    FOR r IN SELECT tablename FROM pg_tables
             WHERE schemaname = 'public' AND tablename LIKE 'procrastinate\\_%' LOOP
        EXECUTE format('DROP TABLE IF EXISTS %I CASCADE', r.tablename);
    END LOOP;
    FOR r IN SELECT p.oid::regprocedure AS signature
             FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
             WHERE n.nspname = 'public' AND p.proname LIKE 'procrastinate\\_%' LOOP
        EXECUTE format('DROP FUNCTION IF EXISTS %s CASCADE', r.signature);
    END LOOP;
    FOR r IN SELECT t.typname FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
             WHERE n.nspname = 'public' AND t.typname LIKE 'procrastinate\\_%'
               AND t.typtype IN ('e', 'c') LOOP
        EXECUTE format('DROP TYPE IF EXISTS %I CASCADE', r.typname);
    END LOOP;
END
$$;
"""


def _execute_raw(sql: str) -> None:
    """Run a multi-statement script on the migration connection without bind processing."""
    dbapi: Any = op.get_bind().connection.dbapi_connection
    with dbapi.cursor() as cursor:
        cursor.execute(sql)


def upgrade() -> None:
    """Create the Procrastinate tables, types, functions and triggers."""
    _execute_raw(SCHEMA_SNAPSHOT.read_text())


def downgrade() -> None:
    """Remove every Procrastinate object; queued jobs are lost."""
    _execute_raw(DROP_PROCRASTINATE)
