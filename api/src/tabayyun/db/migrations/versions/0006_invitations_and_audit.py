"""Invitations and the audit log (spec 014).

- `invitations`: bound to an email, at most one open per org and email. Accepted when that
  verified email signs in; RLS by `org_id`, no DELETE for `tabayyun_app` (revoked, not removed).
- `audit_events`: append-only. RLS by `org_id`; `tabayyun_app` may only SELECT and INSERT.
- `tabayyun_accept_invitations(user)`: SECURITY DEFINER, run as the owner because a user
  without a membership has no org context yet. It grants the invited roles (keeping a higher
  existing one), marks the invitations accepted, writes a `member.joined` event each, and
  returns the orgs joined (`/api/auth/me` moves a session without access into one).
- `tabayyun_login(...)`: unchanged signature and result; it now accepts invitations before
  picking the org it returns.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-03 19:00:00+00:00
"""

from __future__ import annotations

import importlib
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "tabayyun_app"
CURRENT_ORG = "nullif(current_setting('app.org_id', true), '')::uuid"
LOGIN_SIGNATURE = "tabayyun_login(text, text, text, boolean, text, text)"

# Only module constants are interpolated.
ACCEPT_FUNCTION = f"""
CREATE FUNCTION tabayyun_accept_invitations(p_user uuid) RETURNS SETOF uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    v_email text;
    r record;
BEGIN
    SELECT u.email INTO v_email FROM users u WHERE u.id = p_user AND u.disabled_at IS NULL;
    IF v_email IS NULL THEN
        RETURN;
    END IF;
    FOR r IN
        SELECT i.id, i.org_id, i.org_role, i.workspace_id, i.workspace_role, i.invited_by
          FROM invitations i
         WHERE i.email = v_email AND i.accepted_at IS NULL AND i.revoked_at IS NULL
           AND i.expires_at > now()
         ORDER BY i.created_at
           FOR UPDATE
    LOOP
        -- An existing membership keeps its role unless the invitation's is higher.
        INSERT INTO org_memberships AS m (org_id, user_id, role)
        VALUES (r.org_id, p_user, r.org_role)
        ON CONFLICT ON CONSTRAINT pk_org_memberships DO UPDATE SET role = EXCLUDED.role
         WHERE array_position(ARRAY['member', 'admin', 'owner'], m.role)
             < array_position(ARRAY['member', 'admin', 'owner'], EXCLUDED.role);
        IF r.workspace_id IS NOT NULL THEN
            INSERT INTO workspace_memberships AS w (org_id, workspace_id, user_id, role)
            VALUES (r.org_id, r.workspace_id, p_user, r.workspace_role)
            ON CONFLICT ON CONSTRAINT pk_workspace_memberships DO UPDATE SET role = EXCLUDED.role
             WHERE array_position(ARRAY['viewer', 'editor', 'admin'], w.role)
                 < array_position(ARRAY['viewer', 'editor', 'admin'], EXCLUDED.role);
        END IF;
        UPDATE invitations SET accepted_at = now(), accepted_user_id = p_user WHERE id = r.id;
        INSERT INTO audit_events (id, org_id, workspace_id, actor_user_id, action, target_type,
                                  target_id, details)
        VALUES (gen_random_uuid(), r.org_id, r.workspace_id, NULL, 'member.joined', 'user',
                p_user::text, jsonb_build_object(
                    'email', v_email, 'invitation_id', r.id, 'invited_by', r.invited_by,
                    'org_role', r.org_role, 'workspace_role', r.workspace_role));
        RETURN NEXT r.org_id;
    END LOOP;
END
$$;
REVOKE ALL ON FUNCTION tabayyun_accept_invitations(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION tabayyun_accept_invitations(uuid) TO {APP_ROLE};
"""  # noqa: S608


def _login_function_0005() -> str:
    """Spec 013's login function, as migration 0005 created it (the downgrade restores it)."""
    module = importlib.import_module("tabayyun.db.migrations.versions.0005_logins_and_sessions")
    return str(module.LOGIN_FUNCTION)


def _login_function_0006() -> str:
    """The 0005 function, accepting invitations after the user is resolved."""
    marker = "    RETURN QUERY\n"
    body = _login_function_0005()
    if body.count(marker) != 1:
        raise RuntimeError("tabayyun_login of 0005 changed shape; update migration 0006")
    return body.replace(marker, "    PERFORM * FROM tabayyun_accept_invitations(v_user);\n\n" + marker)


def _replace_login_function(sql: str) -> None:
    op.execute(f"DROP FUNCTION {LOGIN_SIGNATURE}")
    op.execute(sql)


def _enable_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant ON {table} USING (org_id = {CURRENT_ORG}) WITH CHECK (org_id = {CURRENT_ORG})"
    )


def upgrade() -> None:
    """Tables, RLS, grants and the two functions."""
    op.create_table(
        "invitations",
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("org_role", sa.Text(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=True),
        sa.Column("workspace_role", sa.Text(), nullable=True),
        sa.Column("invited_by", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_user_id", sa.UUID(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("email_status", sa.Text(), server_default="not_configured", nullable=False),
        sa.Column("email_error", sa.Text(), nullable=True),
        sa.CheckConstraint("org_role IN ('admin', 'member')", name=op.f("ck_invitations_org_role")),
        sa.CheckConstraint(
            "workspace_role IS NULL OR workspace_role IN ('admin', 'editor', 'viewer')",
            name=op.f("ck_invitations_workspace_role"),
        ),
        sa.CheckConstraint(
            "(workspace_id IS NULL) = (workspace_role IS NULL)", name=op.f("ck_invitations_workspace_grant")
        ),
        sa.CheckConstraint(
            "email_status IN ('not_configured', 'queued', 'sent', 'failed')",
            name=op.f("ck_invitations_email_status"),
        ),
        sa.CheckConstraint("email = lower(email)", name=op.f("ck_invitations_email_lower")),
        sa.ForeignKeyConstraint(
            ["accepted_user_id"],
            ["users.id"],
            name=op.f("fk_invitations_accepted_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["invited_by"], ["users.id"], name=op.f("fk_invitations_invited_by_users"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], name=op.f("fk_invitations_org_id_orgs")),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_invitations_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_invitations")),
    )
    op.create_index("ix_invitations_email", "invitations", ["email"])
    op.create_index(
        "uq_invitations_open_email",
        "invitations",
        ["org_id", "email"],
        unique=True,
        postgresql_where=sa.text("accepted_at IS NULL AND revoked_at IS NULL"),
    )
    op.create_table(
        "audit_events",
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=True),
        sa.Column("actor_user_id", sa.UUID(), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("target_type", sa.Text(), nullable=False),
        sa.Column("target_id", sa.Text(), nullable=False),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("ip_address", postgresql.INET(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name=op.f("fk_audit_events_actor_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], name=op.f("fk_audit_events_org_id_orgs")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_events")),
    )
    op.create_index(
        "ix_audit_events_org_created",
        "audit_events",
        ["org_id", sa.text("created_at DESC"), sa.text("id DESC")],
    )
    op.create_index(
        "ix_audit_events_workspace_created",
        "audit_events",
        ["org_id", "workspace_id", sa.text("created_at DESC")],
    )
    _enable_rls("invitations")
    _enable_rls("audit_events")
    # Default privileges (0004) granted full DML; invitations are revoked rather than deleted,
    # and audit events are never changed.
    op.execute(f"REVOKE DELETE ON invitations FROM {APP_ROLE}")
    op.execute(f"REVOKE UPDATE, DELETE, TRUNCATE ON audit_events FROM {APP_ROLE}")
    op.execute(ACCEPT_FUNCTION)
    _replace_login_function(_login_function_0006())


def downgrade() -> None:
    """Reverse order."""
    _replace_login_function(_login_function_0005())
    op.execute("DROP FUNCTION tabayyun_accept_invitations(uuid)")
    op.drop_index("ix_audit_events_workspace_created", table_name="audit_events")
    op.drop_index("ix_audit_events_org_created", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_index("uq_invitations_open_email", table_name="invitations")
    op.drop_index("ix_invitations_email", table_name="invitations")
    op.drop_table("invitations")
