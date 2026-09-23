"""Series groups and dataset deletion rules (spec 008).

Adds `series_groups` and `series_group_members`. A deleted dataset keeps its past runs
(`runs.dataset_id` becomes `ON DELETE SET NULL`) and takes its series list with it
(`dataset_series.dataset_id` becomes `ON DELETE CASCADE`).

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-23 08:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUNS_FK = "fk_runs_dataset_id_datasets"
DATASET_SERIES_FK = "fk_dataset_series_dataset_id_datasets"


def _replace_fk(name: str, table: str, column: str, ondelete: str | None) -> None:
    """Recreate a foreign key to `datasets.id` with another ON DELETE rule."""
    op.drop_constraint(name, table, type_="foreignkey")
    op.create_foreign_key(name, table, "datasets", [column], ["id"], ondelete=ondelete)


def upgrade() -> None:
    op.create_table(
        "series_groups",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("params", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.CheckConstraint("kind IN ('related', 'redundant', 'balance')", name=op.f("ck_series_groups_kind")),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], name=op.f("fk_series_groups_org_id_orgs")),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_series_groups_workspace_id_workspaces")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_series_groups")),
        sa.UniqueConstraint("workspace_id", "name", name=op.f("uq_series_groups_workspace_id_name")),
    )
    op.create_table(
        "series_group_members",
        sa.Column("group_id", sa.UUID(), nullable=False),
        sa.Column("series_id", sa.UUID(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "role IN ('member', 'input', 'output')", name=op.f("ck_series_group_members_role")
        ),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["series_groups.id"],
            name=op.f("fk_series_group_members_group_id_series_groups"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["series_id"], ["series.id"], name=op.f("fk_series_group_members_series_id_series")
        ),
        sa.PrimaryKeyConstraint("group_id", "series_id", name=op.f("pk_series_group_members")),
    )
    op.create_index("ix_series_group_members_series_id", "series_group_members", ["series_id"])
    _replace_fk(RUNS_FK, "runs", "dataset_id", "SET NULL")
    _replace_fk(DATASET_SERIES_FK, "dataset_series", "dataset_id", "CASCADE")


def downgrade() -> None:
    _replace_fk(DATASET_SERIES_FK, "dataset_series", "dataset_id", None)
    _replace_fk(RUNS_FK, "runs", "dataset_id", None)
    op.drop_index("ix_series_group_members_series_id", table_name="series_group_members")
    op.drop_table("series_group_members")
    op.drop_table("series_groups")
