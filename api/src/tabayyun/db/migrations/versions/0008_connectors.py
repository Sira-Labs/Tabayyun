"""Connector framework (spec 021).

- `sources` gains `enabled`, `polled_at` and `updated_at`; its type may be `synthetic`.
- `source_credentials`: one AES-GCM ciphertext per connector source (`tabayyun.secrets`), RLS
  by `org_id`.
- `source_fetches`: the fetch history (manual, poll, run, check), RLS by `org_id`.

`tabayyun_app` gets DML on both through the default privileges of 0004.

Polling and pruning are cross-org jobs, so they run as the owner through two SECURITY
DEFINER functions, as the stale-run reaper does (0004):

- `tabayyun_claim_due_sources(now, types)`: enabled sources of a connector type whose
  `config.poll_interval_s` has passed since `polled_at` and that have no fetch queued or
  running from the last hour (an older one is a lost worker's); sets their `polled_at` and
  returns them.
- `tabayyun_prune_source_fetches(before)`: deletes fetches created before `before`.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-06 08:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_ORG = "nullif(current_setting('app.org_id', true), '')::uuid"
TYPES_0001 = "type IN ('upload', 'csv_dir', 'pi_web_api', 'opc_ua')"
TYPES_0008 = "type IN ('upload', 'csv_dir', 'pi_web_api', 'opc_ua', 'synthetic')"
FUNCTIONS = """
CREATE FUNCTION tabayyun_claim_due_sources(p_now timestamptz, p_types text[])
RETURNS TABLE (source_id uuid, org_id uuid, workspace_id uuid)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
    UPDATE sources s SET polled_at = p_now
     WHERE s.enabled AND s.type = ANY (p_types)
       AND jsonb_typeof(s.config -> 'poll_interval_s') = 'number'
       AND (s.polled_at IS NULL
            OR s.polled_at
               + make_interval(secs => (s.config ->> 'poll_interval_s')::double precision) <= p_now)
       AND NOT EXISTS (
           SELECT 1 FROM source_fetches f
            WHERE f.source_id = s.id AND f.status IN ('queued', 'running')
              AND f.created_at > p_now - interval '1 hour')
    RETURNING s.id, s.org_id, s.workspace_id
$$;
REVOKE ALL ON FUNCTION tabayyun_claim_due_sources(timestamptz, text[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION tabayyun_claim_due_sources(timestamptz, text[]) TO tabayyun_app;

CREATE FUNCTION tabayyun_prune_source_fetches(p_before timestamptz) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    pruned integer;
BEGIN
    DELETE FROM source_fetches WHERE created_at < p_before;
    GET DIAGNOSTICS pruned = ROW_COUNT;
    RETURN pruned;
END
$$;
REVOKE ALL ON FUNCTION tabayyun_prune_source_fetches(timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION tabayyun_prune_source_fetches(timestamptz) TO tabayyun_app;
"""


def _enable_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant ON {table} USING (org_id = {CURRENT_ORG}) WITH CHECK (org_id = {CURRENT_ORG})"
    )


def _source_types(check: str, *, validate: bool = True) -> None:
    op.drop_constraint(op.f("ck_sources_type"), "sources", type_="check")
    suffix = "" if validate else " NOT VALID"
    op.execute(f"ALTER TABLE sources ADD CONSTRAINT ck_sources_type CHECK ({check}){suffix}")


def upgrade() -> None:
    """Columns, the two tables and their policies."""
    _source_types(TYPES_0008)
    op.add_column(
        "sources", sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False)
    )
    op.add_column("sources", sa.Column("polled_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "sources",
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_table(
        "source_credentials",
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("source_id", sa.UUID(), nullable=False),
        sa.Column("key_id", sa.Text(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("updated_by", sa.UUID(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], name=op.f("fk_source_credentials_org_id_orgs")),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["sources.id"],
            name=op.f("fk_source_credentials_source_id_sources"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"],
            ["users.id"],
            name=op.f("fk_source_credentials_updated_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("source_id", name=op.f("pk_source_credentials")),
    )
    op.create_table(
        "source_fetches",
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("source_id", sa.UUID(), nullable=False),
        sa.Column("trigger", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="queued", nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("series_ids", postgresql.ARRAY(sa.UUID()), nullable=True),
        sa.Column("force", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("calls", sa.Integer(), server_default="0", nullable=False),
        sa.Column("rows", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("requested_by", sa.UUID(), nullable=True),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "trigger IN ('manual', 'poll', 'run', 'check')", name=op.f("ck_source_fetches_trigger")
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'partial', 'failed')",
            name=op.f("ck_source_fetches_status"),
        ),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], name=op.f("fk_source_fetches_org_id_orgs")),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_source_fetches_workspace_id_workspaces")
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["sources.id"],
            name=op.f("fk_source_fetches_source_id_sources"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"],
            ["users.id"],
            name=op.f("fk_source_fetches_requested_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_source_fetches")),
    )
    op.create_index(
        "ix_source_fetches_source_created", "source_fetches", ["source_id", sa.text("created_at DESC")]
    )
    _enable_rls("source_credentials")
    _enable_rls("source_fetches")
    op.execute(FUNCTIONS)


def downgrade() -> None:
    """Reverse order. Synthetic sources stay: the old check is added NOT VALID, so it binds new
    rows only and the downgrade loses no data."""
    op.execute("DROP FUNCTION tabayyun_prune_source_fetches(timestamptz)")
    op.execute("DROP FUNCTION tabayyun_claim_due_sources(timestamptz, text[])")
    op.drop_index("ix_source_fetches_source_created", table_name="source_fetches")
    op.drop_table("source_fetches")
    op.drop_table("source_credentials")
    op.drop_column("sources", "updated_at")
    op.drop_column("sources", "polled_at")
    op.drop_column("sources", "enabled")
    _source_types(TYPES_0001, validate=False)
