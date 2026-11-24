"""Corrections (spec 035).

- `corrections`: one proposed repair of a window of a series, its version of the corrected
  layer, what computing it changed and who decided on it;
- `corrected_ranges`: the runs of samples a correction touched. The values before are the
  base layer's and the values after the version's in the cache; the range names both.

Both carry row-level security by `org_id`. `tabayyun_app` gets DML on them through the
default privileges of 0004.

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-24 10:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_ORG = "nullif(current_setting('app.org_id', true), '')::uuid"
TABLES = ("corrections", "corrected_ranges")


def upgrade() -> None:
    """Both tables with their policies."""
    op.create_table(
        "corrections",
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("series_id", sa.UUID(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), server_default="proposed", nullable=False),
        sa.Column("method", sa.Text(), nullable=False),
        sa.Column("params", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("window_start_ns", sa.BigInteger(), nullable=False),
        sa.Column("window_end_ns", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("finding_id", sa.UUID(), nullable=True),
        sa.Column("base_version", sa.Integer(), nullable=True),
        sa.Column("stats", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("proposed_by", sa.UUID(), nullable=True),
        sa.Column("decided_by", sa.UUID(), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('proposed', 'computed', 'failed', 'approved', 'rejected')",
            name=op.f("ck_corrections_status"),
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_corrections_version")),
        sa.CheckConstraint("window_end_ns > window_start_ns", name=op.f("ck_corrections_window")),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], name=op.f("fk_corrections_org_id_orgs")),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_corrections_workspace_id_workspaces")
        ),
        sa.ForeignKeyConstraint(
            ["series_id"], ["series.id"], name=op.f("fk_corrections_series_id_series"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["proposed_by"], ["users.id"], name=op.f("fk_corrections_proposed_by_users"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["decided_by"], ["users.id"], name=op.f("fk_corrections_decided_by_users"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_corrections")),
        sa.UniqueConstraint("series_id", "version", name=op.f("uq_corrections_series_id_version")),
    )
    op.create_index("ix_corrections_series_id_version", "corrections", ["series_id", sa.text("version DESC")])
    op.create_table(
        "corrected_ranges",
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("correction_id", sa.UUID(), nullable=False),
        sa.Column("range_start_ns", sa.BigInteger(), nullable=False),
        sa.Column("range_end_ns", sa.BigInteger(), nullable=False),
        sa.Column("changed", sa.Integer(), server_default="0", nullable=False),
        sa.Column("added", sa.Integer(), server_default="0", nullable=False),
        sa.Column("removed", sa.Integer(), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], name=op.f("fk_corrected_ranges_org_id_orgs")),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_corrected_ranges_workspace_id_workspaces")
        ),
        sa.ForeignKeyConstraint(
            ["correction_id"],
            ["corrections.id"],
            name=op.f("fk_corrected_ranges_correction_id_corrections"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_corrected_ranges")),
    )
    op.create_index(
        "ix_corrected_ranges_correction_id_range_start_ns",
        "corrected_ranges",
        ["correction_id", "range_start_ns"],
    )
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant ON {table} USING (org_id = {CURRENT_ORG}) "
            f"WITH CHECK (org_id = {CURRENT_ORG})"
        )


def downgrade() -> None:
    """Drop both tables: the corrections' records are gone, their cache files stay."""
    op.drop_table("corrected_ranges")
    op.drop_table("corrections")
