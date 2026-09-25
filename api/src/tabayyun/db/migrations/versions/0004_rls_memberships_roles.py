"""Row-level security, memberships and roles (spec 007, ADR-0007).

- Users, org and workspace memberships, teams, team members and workspace team roles.
- `org_id` on the child tables that lacked it (uploads, metrics, scores, coverage,
  dataset_series, series_group_members), backfilled from the parent row before NOT NULL.
- The `tabayyun_app` role (NOLOGIN; logins are made members of it by the migrate command)
  with DML on the schema and default privileges for later migrations; `alembic_version` and
  `users` (no RLS, it spans orgs) are read-only to it.
- One RLS policy per tenant table keyed by `app.org_id`. RLS is enabled, not forced: the
  owner runs migrations and the reaper function, and the app login must not be the owner
  (the startup check in `tabayyun.db.roles` reports it when it is).
- `tabayyun_reap_stale_runs(interval)`: the reaper is the one cross-org job, so it runs as
  the owner through this SECURITY DEFINER function.
- TimescaleDB chunks: a query through a hypertable applies the hypertable's policy, but a
  chunk named directly would bypass it. With TimescaleDB installed, RLS is enabled (with no
  policy, so nothing is visible) on every existing chunk and, through the event trigger
  `tabayyun_chunk_rls`, on every chunk created later.
- The bootstrap user, owner of the default org, as whom requests act until spec 013.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-25 13:02:20+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "tabayyun_app"
DEFAULT_ORG_ID = "00000000-0000-0000-0000-000000000001"
BOOTSTRAP_USER_ID = "00000000-0000-0000-0000-000000000003"
BOOTSTRAP_EMAIL = "bootstrap@tabayyun.invalid"

# Child table → (parent table, column referencing the parent's id).
CHILDREN = {
    "uploads": ("runs", "run_id"),
    "metrics": ("series", "series_id"),
    "scores": ("series", "series_id"),
    "coverage": ("series", "series_id"),
    "dataset_series": ("datasets", "dataset_id"),
    "series_group_members": ("series_groups", "group_id"),
}
NEW_TENANT_TABLES = (
    "org_memberships",
    "workspace_memberships",
    "teams",
    "team_members",
    "workspace_team_roles",
)
# Every table whose rows belong to one org, keyed by `org_id`; `orgs` is keyed by `id`.
RLS_TABLES = (
    "workspaces",
    "sources",
    "series",
    "datasets",
    "series_groups",
    "runs",
    "findings",
    *CHILDREN,
    *NEW_TENANT_TABLES,
)
CURRENT_ORG = "nullif(current_setting('app.org_id', true), '')::uuid"

REAPER_FUNCTION = """
CREATE FUNCTION tabayyun_reap_stale_runs(older_than interval) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    reaped integer;
BEGIN
    UPDATE runs SET status = 'failed', error = 'worker lost', finished_at = now()
     WHERE status = 'running' AND started_at < now() - older_than;
    GET DIAGNOSTICS reaped = ROW_COUNT;
    DELETE FROM uploads u USING runs r
     WHERE u.run_id = r.id AND r.status = 'failed' AND r.error = 'worker lost';
    RETURN reaped;
END
$$;
REVOKE ALL ON FUNCTION tabayyun_reap_stale_runs(interval) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION tabayyun_reap_stale_runs(interval) TO tabayyun_app;
"""


CHUNK_RLS_FUNCTION = """
CREATE FUNCTION tabayyun_chunk_rls() RETURNS event_trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    r record;
BEGIN
    FOR r IN SELECT c.oid::regclass AS rel
               FROM pg_event_trigger_ddl_commands() d
               JOIN pg_class c ON c.oid = d.objid
               JOIN pg_namespace n ON n.oid = c.relnamespace
              WHERE d.classid = 'pg_class'::regclass AND c.relkind = 'r'
                AND NOT c.relrowsecurity AND n.nspname = '_timescaledb_internal' LOOP
        EXECUTE format('ALTER TABLE %s ENABLE ROW LEVEL SECURITY', r.rel);
    END LOOP;
END
$$;
CREATE EVENT TRIGGER tabayyun_chunk_rls ON ddl_command_end EXECUTE FUNCTION tabayyun_chunk_rls();
"""
EXISTING_CHUNKS_RLS = """
DO $$
DECLARE
    r record;
BEGIN
    FOR r IN SELECT format('%I.%I', chunk_schema, chunk_name) AS rel
               FROM timescaledb_information.chunks LOOP
        EXECUTE format('ALTER TABLE %s ENABLE ROW LEVEL SECURITY', r.rel);
    END LOOP;
END
$$;
"""


def _timescale_installed() -> bool:
    """Whether the timescaledb extension exists in this database."""
    found = op.get_bind().execute(sa.text("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'"))
    return found.first() is not None


def upgrade() -> None:
    """Tables, backfill, app role and grants, policies, reaper function, bootstrap user."""
    _create_membership_tables()
    for table, (parent, column) in CHILDREN.items():
        op.add_column(table, sa.Column("org_id", sa.UUID(), nullable=True))
        op.execute(
            # Only module constants are interpolated.
            f"UPDATE {table} AS c SET org_id = p.org_id FROM {parent} AS p WHERE p.id = c.{column}"  # noqa: S608
        )
        op.alter_column(table, "org_id", nullable=False)
        op.create_foreign_key(op.f(f"fk_{table}_org_id_orgs"), table, "orgs", ["org_id"], ["id"])
    _create_app_role()
    op.execute("ALTER TABLE orgs ENABLE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY tenant ON orgs USING (id = {CURRENT_ORG}) WITH CHECK (id = {CURRENT_ORG})")
    for table in RLS_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant ON {table} "
            f"USING (org_id = {CURRENT_ORG}) WITH CHECK (org_id = {CURRENT_ORG})"
        )
    op.execute(REAPER_FUNCTION)
    if _timescale_installed():
        op.execute(EXISTING_CHUNKS_RLS)
        op.execute(CHUNK_RLS_FUNCTION)
    _seed_bootstrap_user()


def downgrade() -> None:
    """Reverse order; the role stays because other databases in the cluster may use it."""
    op.execute("DROP EVENT TRIGGER IF EXISTS tabayyun_chunk_rls")
    op.execute("DROP FUNCTION IF EXISTS tabayyun_chunk_rls()")
    if _timescale_installed():
        op.execute(EXISTING_CHUNKS_RLS.replace("ENABLE ROW LEVEL SECURITY", "DISABLE ROW LEVEL SECURITY"))
    op.execute("DROP FUNCTION tabayyun_reap_stale_runs(interval)")
    for table in ("orgs", *RLS_TABLES):
        if table in NEW_TENANT_TABLES:
            continue  # dropped with the table below
        op.execute(f"DROP POLICY tenant ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM {APP_ROLE}")
    op.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM {APP_ROLE}")
    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {APP_ROLE}")
    op.execute(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {APP_ROLE}")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {APP_ROLE}")
    for table in CHILDREN:
        op.drop_constraint(op.f(f"fk_{table}_org_id_orgs"), table, type_="foreignkey")
        op.drop_column(table, "org_id")
    op.drop_table("workspace_team_roles")
    op.drop_index("ix_workspace_memberships_user_id", table_name="workspace_memberships")
    op.drop_table("workspace_memberships")
    op.drop_index("ix_team_members_user_id", table_name="team_members")
    op.drop_table("team_members")
    op.drop_table("teams")
    op.drop_table("org_memberships")
    op.drop_table("users")


def _create_app_role() -> None:
    """`tabayyun_app` with DML everywhere except the migration bookkeeping."""
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                CREATE ROLE {APP_ROLE} NOLOGIN NOSUPERUSER NOBYPASSRLS;
            END IF;
        END
        $$;
        """
    )
    op.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE}")
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}")
    op.execute(f"REVOKE INSERT, UPDATE, DELETE ON alembic_version FROM {APP_ROLE}")
    # `users` spans orgs and has no RLS: the app login reads it only. Spec 013 adds a controlled
    # write path for logins.
    op.execute(f"REVOKE INSERT, UPDATE, DELETE ON users FROM {APP_ROLE}")
    # Tables and sequences later migrations create (as this owner) get the same grants.
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {APP_ROLE}"
    )
    op.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO {APP_ROLE}")


def _seed_bootstrap_user() -> None:
    """The user requests act as until login exists: owner of the default org."""
    op.execute(
        sa.text("INSERT INTO users (id, email, display_name) VALUES (:id, :email, 'Bootstrap')").bindparams(
            sa.bindparam("id", BOOTSTRAP_USER_ID, type_=sa.UUID()),
            sa.bindparam("email", BOOTSTRAP_EMAIL),
        )
    )
    op.execute(
        sa.text(
            "INSERT INTO org_memberships (org_id, user_id, role) VALUES (:org, :user, 'owner')"
        ).bindparams(
            sa.bindparam("org", DEFAULT_ORG_ID, type_=sa.UUID()),
            sa.bindparam("user", BOOTSTRAP_USER_ID, type_=sa.UUID()),
        )
    )


def _create_membership_tables() -> None:
    """Users, memberships and teams (autogenerated from the models, then reviewed)."""
    op.create_table(
        "users",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("email = lower(email)", name=op.f("ck_users_email_lower")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
    )
    op.create_table(
        "org_memberships",
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("role IN ('owner', 'admin', 'member')", name=op.f("ck_org_memberships_role")),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_org_memberships_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_org_memberships_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("org_id", "user_id", name=op.f("pk_org_memberships")),
    )
    op.create_table(
        "teams",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], name=op.f("fk_teams_org_id_orgs")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_teams")),
        sa.UniqueConstraint("org_id", "name", name=op.f("uq_teams_org_id_name")),
    )
    op.create_table(
        "team_members",
        sa.Column("team_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], name=op.f("fk_team_members_org_id_orgs")),
        sa.ForeignKeyConstraint(
            ["team_id"], ["teams.id"], name=op.f("fk_team_members_team_id_teams"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_team_members_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("team_id", "user_id", name=op.f("pk_team_members")),
    )
    op.create_index("ix_team_members_user_id", "team_members", ["user_id"], unique=False)
    op.create_table(
        "workspace_memberships",
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.CheckConstraint(
            "role IN ('admin', 'editor', 'viewer')", name=op.f("ck_workspace_memberships_role")
        ),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], name=op.f("fk_workspace_memberships_org_id_orgs")),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_workspace_memberships_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_workspace_memberships_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("workspace_id", "user_id", name=op.f("pk_workspace_memberships")),
    )
    op.create_index("ix_workspace_memberships_user_id", "workspace_memberships", ["user_id"], unique=False)
    op.create_table(
        "workspace_team_roles",
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("team_id", sa.UUID(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.CheckConstraint(
            "role IN ('admin', 'editor', 'viewer')", name=op.f("ck_workspace_team_roles_role")
        ),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], name=op.f("fk_workspace_team_roles_org_id_orgs")),
        sa.ForeignKeyConstraint(
            ["team_id"], ["teams.id"], name=op.f("fk_workspace_team_roles_team_id_teams"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_workspace_team_roles_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("workspace_id", "team_id", name=op.f("pk_workspace_team_roles")),
    )
