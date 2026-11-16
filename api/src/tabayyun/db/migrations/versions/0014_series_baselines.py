"""Stored baseline profiles (spec 028).

- `series_baselines`: the versions of a series' baseline profile, RLS by `org_id`;
- `series.baseline_checked_at`: when the scheduler last queued the series;
- `tabayyun_claim_series_for_baseline(now, limit)`: the scheduler crosses orgs, so it claims
  series through a SECURITY DEFINER function, like the ones for sources (0008) and suites
  (0012). A claim stamps the series, so it comes up again a day later.

`tabayyun_app` gets DML on the table through the default privileges of 0004.

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-16 22:50:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_ORG = "nullif(current_setting('app.org_id', true), '')::uuid"
CLAIM_FUNCTION = """
CREATE FUNCTION tabayyun_claim_series_for_baseline(p_now timestamptz, p_limit integer)
RETURNS TABLE (series_id uuid, org_id uuid)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
    UPDATE series s SET baseline_checked_at = p_now
     WHERE s.id IN (
        SELECT c.id FROM series c
         WHERE (c.baseline_checked_at IS NULL OR c.baseline_checked_at <= p_now - interval '24 hours')
           AND EXISTS (SELECT 1 FROM coverage v WHERE v.series_id = c.id AND v.layer = 'raw')
         ORDER BY c.baseline_checked_at NULLS FIRST, c.id
         LIMIT p_limit
           FOR UPDATE SKIP LOCKED)
    RETURNING s.id, s.org_id
$$;
REVOKE ALL ON FUNCTION tabayyun_claim_series_for_baseline(timestamptz, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION tabayyun_claim_series_for_baseline(timestamptz, integer) TO tabayyun_app;
"""


def upgrade() -> None:
    """The table with its policy, the series column with its index and the claim function."""
    op.create_table(
        "series_baselines",
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("series_id", sa.UUID(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("n_samples", sa.Integer(), nullable=False),
        sa.Column("excluded", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("profile", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("trigger", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("trigger IN ('schedule', 'manual')", name=op.f("ck_series_baselines_trigger")),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], name=op.f("fk_series_baselines_org_id_orgs")),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_series_baselines_workspace_id_workspaces")
        ),
        sa.ForeignKeyConstraint(
            ["series_id"],
            ["series.id"],
            name=op.f("fk_series_baselines_series_id_series"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_series_baselines")),
        sa.UniqueConstraint("series_id", "version", name=op.f("uq_series_baselines_series_id_version")),
    )
    op.execute("ALTER TABLE series_baselines ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant ON series_baselines USING (org_id = {CURRENT_ORG}) "
        f"WITH CHECK (org_id = {CURRENT_ORG})"
    )
    # A nullable column without a default and an index on the small `series` table: both quick.
    op.add_column("series", sa.Column("baseline_checked_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_series_baseline_checked_at", "series", [sa.text("baseline_checked_at NULLS FIRST")])
    op.execute(CLAIM_FUNCTION)


def downgrade() -> None:
    """Drop the function, the series column and the table with every stored baseline."""
    op.execute("DROP FUNCTION tabayyun_claim_series_for_baseline(timestamptz, integer)")
    op.drop_index("ix_series_baseline_checked_at", table_name="series")
    op.drop_column("series", "baseline_checked_at")
    op.drop_table("series_baselines")
