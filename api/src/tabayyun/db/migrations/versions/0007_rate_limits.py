"""Rate limits (spec 015).

`rate_limits` holds one row per bucket, key and fixed window. It is not tenant data: no
`org_id` and no row-level security, because sign-in is limited before any org is known.
`tabayyun_app` gets DML through the default privileges of 0004.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-05 09:30:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """The table and its prune index."""
    op.create_table(
        "rate_limits",
        sa.Column("bucket", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("hits", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("bucket", "key", "window_start", name=op.f("pk_rate_limits")),
    )
    op.create_index("ix_rate_limits_window_start", "rate_limits", ["window_start"])


def downgrade() -> None:
    """Drop the table."""
    op.drop_index("ix_rate_limits_window_start", table_name="rate_limits")
    op.drop_table("rate_limits")
