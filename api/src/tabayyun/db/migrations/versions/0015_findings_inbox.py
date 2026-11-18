"""Findings inbox (spec 029).

- `findings.muted_until`, `assignee_id`, `feedback`, `feedback_at`: the end of a mute, who
  works on a finding, and the user's verdict on it;
- `finding_filters`: a user's saved filters of the findings page, RLS by `org_id`;
- `tabayyun_reopen_expired_mutes(now)`: the periodic task crosses orgs, so it opens expired
  mutes through a SECURITY DEFINER function, like the claim functions of 0008, 0012 and 0014.

`tabayyun_app` gets DML on the table through the default privileges of 0004.

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-17 22:40:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_ORG = "nullif(current_setting('app.org_id', true), '')::uuid"
REOPEN_FUNCTION = """
CREATE FUNCTION tabayyun_reopen_expired_mutes(p_now timestamptz)
RETURNS integer
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
    WITH reopened AS (
        UPDATE findings
           SET status = 'open', status_reason = 'mute expired', status_at = p_now,
               muted_until = NULL, updated_at = p_now
         WHERE status = 'muted' AND muted_until IS NOT NULL AND muted_until <= p_now
        RETURNING 1)
    SELECT count(*)::integer FROM reopened
$$;
REVOKE ALL ON FUNCTION tabayyun_reopen_expired_mutes(timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION tabayyun_reopen_expired_mutes(timestamptz) TO tabayyun_app;
"""


def upgrade() -> None:
    """The finding columns with their index, the filters table with its policy, the function."""
    # Nullable columns without defaults: no rewrite of `findings`. The check and the foreign
    # key scan the table once to validate; every existing value is null. `findings` may be a
    # hypertable, where constraints cannot be added unvalidated.
    op.add_column("findings", sa.Column("muted_until", sa.DateTime(timezone=True), nullable=True))
    op.add_column("findings", sa.Column("assignee_id", sa.UUID(), nullable=True))
    op.add_column("findings", sa.Column("feedback", sa.Text(), nullable=True))
    op.add_column("findings", sa.Column("feedback_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(op.f("ck_findings_feedback"), "findings", "feedback IN ('false_positive')")
    op.create_foreign_key(
        op.f("fk_findings_assignee_id_users"),
        "findings",
        "users",
        ["assignee_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_findings_muted_until",
        "findings",
        ["muted_until"],
        postgresql_where=sa.text("status = 'muted' AND muted_until IS NOT NULL"),
    )
    op.create_table(
        "finding_filters",
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], name=op.f("fk_finding_filters_org_id_orgs")),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_finding_filters_workspace_id_workspaces")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_finding_filters_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_finding_filters")),
        sa.UniqueConstraint(
            "workspace_id", "user_id", "name", name=op.f("uq_finding_filters_workspace_id_user_id_name")
        ),
    )
    op.execute("ALTER TABLE finding_filters ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant ON finding_filters USING (org_id = {CURRENT_ORG}) "
        f"WITH CHECK (org_id = {CURRENT_ORG})"
    )
    op.execute(REOPEN_FUNCTION)


def downgrade() -> None:
    """Drop the function, the filters and the finding columns: mutes lose their end, and
    assignments and feedback are gone."""
    op.execute("DROP FUNCTION tabayyun_reopen_expired_mutes(timestamptz)")
    op.drop_table("finding_filters")
    op.drop_index("ix_findings_muted_until", table_name="findings")
    op.drop_constraint(op.f("fk_findings_assignee_id_users"), "findings", type_="foreignkey")
    op.drop_constraint(op.f("ck_findings_feedback"), "findings", type_="check")
    op.drop_column("findings", "feedback_at")
    op.drop_column("findings", "feedback")
    op.drop_column("findings", "assignee_id")
    op.drop_column("findings", "muted_until")
