"""The overview's daily scores (spec 032).

- `score_days`: the last score of each series on each UTC day, RLS by `org_id`, filled from
  `scores`; the overview reads it instead of `scores`, which holds a row per run;
- `findings` gets an index on `(workspace_id, created_at)` for the new findings per day.

`tabayyun_app` gets DML on the table through the default privileges of 0004.

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-21 22:40:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_ORG = "nullif(current_setting('app.org_id', true), '')::uuid"
DIMENSIONS = (
    "completeness",
    "timeliness",
    "validity",
    "accuracy",
    "consistency",
    "plausibility",
    "integrity",
)
# The newest score of each series, layer and UTC day.
BACKFILL = """
INSERT INTO score_days (org_id, workspace_id, series_id, layer, day, overall, {columns},
                        n_findings, computed_at)
SELECT DISTINCT ON (c.series_id, c.layer, (c.computed_at AT TIME ZONE 'UTC')::date)
       s.org_id, s.workspace_id, c.series_id, c.layer, (c.computed_at AT TIME ZONE 'UTC')::date,
       c.overall, {values}, c.n_findings, c.computed_at
  FROM scores c
  JOIN series s ON s.id = c.series_id
 ORDER BY c.series_id, c.layer, (c.computed_at AT TIME ZONE 'UTC')::date, c.computed_at DESC
""".format(
    columns=", ".join(DIMENSIONS),
    values=", ".join(f"(c.dimensions ->> '{d}')::double precision" for d in DIMENSIONS),
)


def upgrade() -> None:
    """The table with its policy and index, filled from the scores, and the findings index."""
    op.create_table(
        "score_days",
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("series_id", sa.UUID(), nullable=False),
        sa.Column("layer", sa.Text(), server_default="raw", nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("overall", sa.Double(), nullable=False),
        *(sa.Column(d, sa.Double(), nullable=True) for d in DIMENSIONS),
        sa.Column("n_findings", sa.Integer(), server_default="0", nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], name=op.f("fk_score_days_org_id_orgs")),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_score_days_workspace_id_workspaces")
        ),
        sa.ForeignKeyConstraint(
            ["series_id"], ["series.id"], name=op.f("fk_score_days_series_id_series"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("series_id", "layer", "day", name=op.f("pk_score_days")),
    )
    op.create_index("ix_score_days_workspace_id_layer_day", "score_days", ["workspace_id", "layer", "day"])
    op.execute(BACKFILL)
    op.execute("ALTER TABLE score_days ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant ON score_days USING (org_id = {CURRENT_ORG}) "
        f"WITH CHECK (org_id = {CURRENT_ORG})"
    )
    op.create_index("ix_findings_workspace_id_created_at", "findings", ["workspace_id", "created_at"])


def downgrade() -> None:
    """Drop the index and the table; the scores they were taken from stay."""
    op.drop_index("ix_findings_workspace_id_created_at", table_name="findings")
    op.drop_table("score_days")
