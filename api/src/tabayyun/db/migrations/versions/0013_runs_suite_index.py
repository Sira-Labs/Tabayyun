"""The runs' suite foreign key validated and their history index, without blocking writers
(spec 027).

`runs` can be large, so the two scans run outside a transaction, after 0012 has committed:
`VALIDATE CONSTRAINT` and `CREATE INDEX CONCURRENTLY` let writes to `runs` continue.

Nothing here is transactional, so every step can be repeated: an upgrade that was interrupted
(a cancelled index build leaves an invalid index behind) is simply run again.

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-15 23:58:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX = "ix_runs_suite_id_created_at"
INVALID_INDEX = sa.text(
    "SELECT 1 FROM pg_index JOIN pg_class ON pg_class.oid = pg_index.indexrelid "
    "WHERE pg_class.relname = :name AND pg_class.relnamespace = current_schema()::regnamespace "
    "AND NOT pg_index.indisvalid"
)


def upgrade() -> None:
    """Validate `fk_runs_suite_id_check_suites` and build the history index concurrently."""
    with op.get_context().autocommit_block():
        # Validating a constraint that is already valid does nothing.
        op.execute("ALTER TABLE runs VALIDATE CONSTRAINT fk_runs_suite_id_check_suites")
        # What an interrupted concurrent build leaves is invalid and would satisfy IF NOT EXISTS.
        if op.get_bind().execute(INVALID_INDEX, {"name": INDEX}).first() is not None:
            op.drop_index(INDEX, table_name="runs", postgresql_concurrently=True)
        op.create_index(
            INDEX,
            "runs",
            ["suite_id", sa.text("created_at DESC")],
            postgresql_where=sa.text("suite_id IS NOT NULL"),
            postgresql_concurrently=True,
            if_not_exists=True,
        )


def downgrade() -> None:
    """Drop the index. The foreign key stays validated until 0012's downgrade drops it."""
    with op.get_context().autocommit_block():
        op.drop_index(INDEX, table_name="runs", postgresql_concurrently=True, if_exists=True)
