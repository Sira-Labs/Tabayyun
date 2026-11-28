"""The corrected layer's score (spec 036).

`scores.baseline`: on a row of the layer `corrected`, the raw layer scored the same way
(`{overall, dimensions, n_findings, version}`), so the pair "raw 61 → corrected 94" is one
row. Null on raw rows; the column has no default, so no row is rewritten.

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-27 10:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable column."""
    op.add_column("scores", sa.Column("baseline", postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    """Drop the column and the corrected layer's scores: only this revision's code writes
    them, and the code before it reads `scores` without naming a layer in places."""
    op.execute("DELETE FROM score_days WHERE layer = 'corrected'")
    op.execute("DELETE FROM scores WHERE layer = 'corrected'")
    op.drop_column("scores", "baseline")
