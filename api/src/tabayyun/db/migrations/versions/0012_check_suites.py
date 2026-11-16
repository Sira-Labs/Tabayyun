"""Check suites (spec 027).

- `check_suites`: a dataset, the checks to run on it and optionally a cron schedule, RLS by
  `org_id`;
- `runs.suite_id` and `runs.checks`: the suite that started a run and its checks as they
  were then;
- `tabayyun_claim_due_suites(now)`: the scheduler crosses orgs, so it claims due suites
  through a SECURITY DEFINER function, like the one that claims due sources (0008). A claim
  moves `next_run_at` ten minutes ahead as a lease; the scheduler then sets the real next
  firing. A worker lost in between leaves the lease, and the suite fires again.

`tabayyun_app` gets DML on the table through the default privileges of 0004.

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-15 22:45:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_ORG = "nullif(current_setting('app.org_id', true), '')::uuid"
CLAIM_FUNCTION = """
CREATE FUNCTION tabayyun_claim_due_suites(p_now timestamptz)
RETURNS TABLE (suite_id uuid, org_id uuid, workspace_id uuid)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
    UPDATE check_suites s SET next_run_at = p_now + interval '10 minutes'
     WHERE s.enabled AND s.schedule IS NOT NULL AND s.dataset_id IS NOT NULL
       AND s.next_run_at <= p_now
    RETURNING s.id, s.org_id, s.workspace_id
$$;
REVOKE ALL ON FUNCTION tabayyun_claim_due_suites(timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION tabayyun_claim_due_suites(timestamptz) TO tabayyun_app;
"""


def upgrade() -> None:
    """The table with its policy, the run columns and the claim function."""
    op.create_table(
        "check_suites",
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("dataset_id", sa.UUID(), nullable=True),
        sa.Column("schedule", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("checks", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], name=op.f("fk_check_suites_org_id_orgs")),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_check_suites_workspace_id_workspaces")
        ),
        sa.ForeignKeyConstraint(
            ["dataset_id"],
            ["datasets.id"],
            name=op.f("fk_check_suites_dataset_id_datasets"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_check_suites")),
        sa.UniqueConstraint("workspace_id", "name", name=op.f("uq_check_suites_workspace_id_name")),
    )
    op.create_index(
        "ix_check_suites_next_run_at", "check_suites", ["next_run_at"], postgresql_where=sa.text("enabled")
    )
    op.execute("ALTER TABLE check_suites ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant ON check_suites USING (org_id = {CURRENT_ORG}) "
        f"WITH CHECK (org_id = {CURRENT_ORG})"
    )
    op.add_column("runs", sa.Column("suite_id", sa.UUID(), nullable=True))
    op.add_column("runs", sa.Column("checks", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.create_foreign_key(
        op.f("fk_runs_suite_id_check_suites"),
        "runs",
        "check_suites",
        ["suite_id"],
        ["id"],
        ondelete="SET NULL",
        # Validated in 0013, outside a transaction: a validating ADD CONSTRAINT would scan
        # `runs` while the lock of the column changes blocks its writers.
        postgresql_not_valid=True,
    )
    op.execute(CLAIM_FUNCTION)


def downgrade() -> None:
    """Drop the function, the run columns and the table. Suite runs stay, as plain runs."""
    op.execute("DROP FUNCTION tabayyun_claim_due_suites(timestamptz)")
    op.drop_constraint(op.f("fk_runs_suite_id_check_suites"), "runs", type_="foreignkey")
    op.drop_column("runs", "checks")
    op.drop_column("runs", "suite_id")
    op.drop_index("ix_check_suites_next_run_at", table_name="check_suites")
    op.drop_table("check_suites")
