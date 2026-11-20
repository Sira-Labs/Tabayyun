"""Alerts (spec 031).

- `alert_rules`: a condition over a run's findings or scores, a channel (email or webhook)
  and a throttle, per workspace; the webhook's signing secret is AES-GCM ciphertext;
- `alert_notifications`: what a rule sent or will send, with its outcome;
- `runs.alerts_evaluated_at`: set by the one evaluation of a run, so a run is never
  evaluated twice;
- `tabayyun_requeue_stuck_alerts(before)`: the periodic task crosses orgs, so it puts
  notifications a dead worker left `sending` back to `pending` through a SECURITY DEFINER
  function, like the functions of 0008, 0012, 0014 and 0015.

Both carry row-level security by `org_id`. `tabayyun_app` gets DML on them through the
default privileges of 0004.

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-20 22:40:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_ORG = "nullif(current_setting('app.org_id', true), '')::uuid"
TABLES = ("alert_rules", "alert_notifications")
REQUEUE_FUNCTION = """
CREATE FUNCTION tabayyun_requeue_stuck_alerts(p_before timestamptz)
RETURNS TABLE (id uuid, org_id uuid)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
    UPDATE alert_notifications n
       SET status = 'pending'
     WHERE n.status = 'sending' AND n.claimed_at <= p_before
    RETURNING n.id, n.org_id
$$;
REVOKE ALL ON FUNCTION tabayyun_requeue_stuck_alerts(timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION tabayyun_requeue_stuck_alerts(timestamptz) TO tabayyun_app;
"""


def upgrade() -> None:
    """Both tables with their policies, the runs' column and the function."""
    # Nullable without a default: no rewrite of `runs`.
    op.add_column("runs", sa.Column("alerts_evaluated_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "alert_rules",
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("condition", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("recipients", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("secret_key_id", sa.Text(), nullable=True),
        sa.Column("secret_nonce", sa.LargeBinary(), nullable=True),
        sa.Column("secret_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("throttle_minutes", sa.Integer(), server_default="60", nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("kind IN ('finding', 'score')", name=op.f("ck_alert_rules_kind")),
        sa.CheckConstraint("channel IN ('email', 'webhook')", name=op.f("ck_alert_rules_channel")),
        sa.CheckConstraint(
            "throttle_minutes BETWEEN 0 AND 1440", name=op.f("ck_alert_rules_throttle_minutes")
        ),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], name=op.f("fk_alert_rules_org_id_orgs")),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_alert_rules_workspace_id_workspaces")
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_alert_rules_created_by_users"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_alert_rules")),
        sa.UniqueConstraint("workspace_id", "name", name=op.f("uq_alert_rules_workspace_id_name")),
    )
    op.create_table(
        "alert_notifications",
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("rule_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.Text(), server_default="alert", nullable=False),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("n_matches", sa.Integer(), server_default="0", nullable=False),
        sa.Column("items", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("kind IN ('alert', 'test')", name=op.f("ck_alert_notifications_kind")),
        sa.CheckConstraint(
            "status IN ('pending', 'sending', 'sent', 'failed', 'skipped')",
            name=op.f("ck_alert_notifications_status"),
        ),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], name=op.f("fk_alert_notifications_org_id_orgs")),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_alert_notifications_workspace_id_workspaces"),
        ),
        sa.ForeignKeyConstraint(
            ["rule_id"],
            ["alert_rules.id"],
            name=op.f("fk_alert_notifications_rule_id_alert_rules"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_alert_notifications")),
    )
    op.create_index(
        "ix_alert_notifications_rule_id_created_at",
        "alert_notifications",
        ["rule_id", sa.text("created_at DESC")],
    )
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant ON {table} USING (org_id = {CURRENT_ORG}) "
            f"WITH CHECK (org_id = {CURRENT_ORG})"
        )
    op.execute(REQUEUE_FUNCTION)


def downgrade() -> None:
    """Drop the function, the column and both tables: the rules and what they sent are gone."""
    op.execute("DROP FUNCTION tabayyun_requeue_stuck_alerts(timestamptz)")
    op.drop_column("runs", "alerts_evaluated_at")
    op.drop_table("alert_notifications")
    op.drop_table("alert_rules")
