"""Search and metadata jobs of connector sources (spec 022).

- `source_fetches.trigger` may be `search` or `metadata`;
- `source_fetches.params` (jsonb): what the job was asked (`{query, limit}`, `{overwrite}`);
- `source_fetches.result` (jsonb): what it found (search matches, imported metadata, point
  errors of a fetch).

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-10 08:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TRIGGERS_0008 = "trigger IN ('manual', 'poll', 'run', 'check')"
TRIGGERS_0009 = "trigger IN ('manual', 'poll', 'run', 'check', 'search', 'metadata')"


def _triggers(check: str, *, validate: bool = True) -> None:
    op.drop_constraint(op.f("ck_source_fetches_trigger"), "source_fetches", type_="check")
    suffix = "" if validate else " NOT VALID"
    op.execute(f"ALTER TABLE source_fetches ADD CONSTRAINT ck_source_fetches_trigger CHECK ({check}){suffix}")


def upgrade() -> None:
    """The two triggers and the two columns."""
    _triggers(TRIGGERS_0009)
    op.add_column("source_fetches", sa.Column("params", postgresql.JSONB(), nullable=True))
    op.add_column("source_fetches", sa.Column("result", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    """Drop the columns. Search and metadata rows stay: the old check is added NOT VALID, as in
    0008, so it binds new rows only and the downgrade deletes no history."""
    op.drop_column("source_fetches", "result")
    op.drop_column("source_fetches", "params")
    _triggers(TRIGGERS_0008, validate=False)
