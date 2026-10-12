"""File-drop sources (spec 026).

- `source_fetches.trigger` may be `ingest` (a scan of a `csv_dir` source's folder);
- `source_files`: one row per file path of a source, with what reading it last gave, RLS by
  `org_id`. Rows outlive the scans (pruned after 30 days): `fetch_id` is set null then.

`tabayyun_app` gets DML on the table through the default privileges of 0004.

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-12 08:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_ORG = "nullif(current_setting('app.org_id', true), '')::uuid"
TRIGGERS_0009 = "trigger IN ('manual', 'poll', 'run', 'check', 'search', 'metadata')"
TRIGGERS_0010 = "trigger IN ('manual', 'poll', 'run', 'check', 'search', 'metadata', 'ingest')"


def _triggers(check: str, *, validate: bool = True) -> None:
    op.drop_constraint(op.f("ck_source_fetches_trigger"), "source_fetches", type_="check")
    suffix = "" if validate else " NOT VALID"
    op.execute(f"ALTER TABLE source_fetches ADD CONSTRAINT ck_source_fetches_trigger CHECK ({check}){suffix}")


def upgrade() -> None:
    """The trigger and the table with its policy."""
    _triggers(TRIGGERS_0010)
    op.create_table(
        "source_files",
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("source_id", sa.UUID(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("mtime", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("rows", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("n_series", sa.Integer(), server_default="0", nullable=False),
        sa.Column("n_new_series", sa.Integer(), server_default="0", nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("versions", sa.Integer(), server_default="1", nullable=False),
        sa.Column("fetch_id", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("status IN ('ingested', 'failed')", name=op.f("ck_source_files_status")),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], name=op.f("fk_source_files_org_id_orgs")),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_source_files_workspace_id_workspaces")
        ),
        sa.ForeignKeyConstraint(
            ["source_id"], ["sources.id"], name=op.f("fk_source_files_source_id_sources"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["fetch_id"],
            ["source_fetches.id"],
            name=op.f("fk_source_files_fetch_id_source_fetches"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_source_files")),
        sa.UniqueConstraint("source_id", "path", name=op.f("uq_source_files_source_id_path")),
    )
    op.create_index(
        "ix_source_files_source_updated",
        "source_files",
        ["source_id", sa.text("updated_at DESC"), sa.text("id DESC")],
    )
    op.execute("ALTER TABLE source_files ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant ON source_files "
        f"USING (org_id = {CURRENT_ORG}) WITH CHECK (org_id = {CURRENT_ORG})"
    )


def downgrade() -> None:
    """Drop the table. Ingest rows stay: the old check is added NOT VALID, as in 0009."""
    op.drop_index("ix_source_files_source_updated", table_name="source_files")
    op.drop_table("source_files")
    _triggers(TRIGGERS_0009, validate=False)
