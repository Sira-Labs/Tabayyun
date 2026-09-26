"""Logins and sessions (spec 013).

- `user_identities`: an identity-provider login (issuer, subject) and the user it belongs to.
  Read-only to `tabayyun_app`, like `users` (spec 007).
- `sessions`: signed-in browsers; the cookie holds a token and the table only its HMAC. No
  row-level security: a request looks its session up before any org is known.
- `login_flows`: logins between `/api/auth/login` and the callback.
- `tabayyun_login(...)`: the one write path to `users` and `user_identities`, run as the owner
  (SECURITY DEFINER) like the reaper. It links or creates the user, and makes the configured
  admin email owner of the default org while that org has no owner besides the bootstrap
  user (Arqam's admin bootstrap rule), serialised by an advisory lock.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-26 15:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "tabayyun_app"
DEFAULT_ORG_ID = "00000000-0000-0000-0000-000000000001"
BOOTSTRAP_USER_ID = "00000000-0000-0000-0000-000000000003"

# Only module constants are interpolated.
LOGIN_FUNCTION = f"""
CREATE FUNCTION tabayyun_login(
    p_issuer text, p_subject text, p_email text, p_email_verified boolean,
    p_display_name text, p_admin_email text
) RETURNS TABLE (user_id uuid, org_id uuid, role text, disabled boolean)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    v_email text := lower(btrim(p_email));
    v_user uuid;
BEGIN
    IF NOT coalesce(p_email_verified, false) OR coalesce(v_email, '') = '' THEN
        RAISE EXCEPTION 'tabayyun_login: a verified email is required' USING ERRCODE = '22023';
    END IF;
    -- One login at a time: user creation by email and the admin bootstrap must not race.
    PERFORM pg_advisory_xact_lock(hashtext('tabayyun.admin_bootstrap'));

    SELECT i.user_id INTO v_user FROM user_identities i
     WHERE i.issuer = p_issuer AND i.subject = p_subject;
    IF v_user IS NULL THEN
        SELECT u.id INTO v_user FROM users u WHERE u.email = v_email;
        IF v_user IS NULL THEN
            INSERT INTO users (id, email, display_name)
            VALUES (gen_random_uuid(), v_email, coalesce(nullif(btrim(p_display_name), ''), v_email))
            RETURNING id INTO v_user;
        END IF;
        INSERT INTO user_identities (issuer, subject, user_id, email_at_login)
        VALUES (p_issuer, p_subject, v_user, v_email);
    ELSE
        UPDATE user_identities SET last_login_at = now(), email_at_login = v_email
         WHERE issuer = p_issuer AND subject = p_subject;
    END IF;

    IF coalesce(lower(btrim(p_admin_email)), '') = v_email AND NOT EXISTS (
        SELECT 1 FROM org_memberships m
         WHERE m.org_id = '{DEFAULT_ORG_ID}' AND m.role = 'owner'
           AND m.user_id <> '{BOOTSTRAP_USER_ID}'
    ) THEN
        INSERT INTO org_memberships (org_id, user_id, role)
        VALUES ('{DEFAULT_ORG_ID}', v_user, 'owner')
        ON CONFLICT ON CONSTRAINT pk_org_memberships DO UPDATE SET role = 'owner';
    END IF;

    RETURN QUERY
        SELECT v_user, m.org_id, m.role, u.disabled_at IS NOT NULL
          FROM users u
          LEFT JOIN LATERAL (
              SELECT om.org_id, om.role FROM org_memberships om
               WHERE om.user_id = v_user
               ORDER BY om.org_id = '{DEFAULT_ORG_ID}' DESC, om.created_at
               LIMIT 1
          ) m ON true
         WHERE u.id = v_user;
END
$$;
REVOKE ALL ON FUNCTION tabayyun_login(text, text, text, boolean, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION tabayyun_login(text, text, text, boolean, text, text) TO {APP_ROLE};
"""  # noqa: S608


def upgrade() -> None:
    """Tables, grants and the login function."""
    op.create_table(
        "user_identities",
        sa.Column("issuer", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("email_at_login", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column(
            "last_login_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_user_identities_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("issuer", "subject", name=op.f("pk_user_identities")),
    )
    op.create_index("ix_user_identities_user_id", "user_identities", ["user_id"])
    op.create_table(
        "sessions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("id_hash", sa.LargeBinary(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=True),
        sa.Column("sign_in_method", sa.Text(), nullable=False),
        sa.Column("idp_sid", sa.Text(), nullable=True),
        sa.Column("id_token", sa.Text(), nullable=True),
        sa.Column("ip_address", postgresql.INET(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column(
            "last_seen_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "sign_in_method IN ('google', 'github', 'passkey')", name=op.f("ck_sessions_sign_in_method")
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_sessions_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_sessions_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sessions")),
        sa.UniqueConstraint("id_hash", name=op.f("uq_sessions_id_hash")),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])
    op.create_index("ix_sessions_idp_sid", "sessions", ["idp_sid"])
    op.create_table(
        "login_flows",
        sa.Column("id_hash", sa.LargeBinary(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("nonce", sa.Text(), nullable=False),
        sa.Column("code_verifier", sa.Text(), nullable=False),
        sa.Column("method", sa.Text(), nullable=False),
        sa.Column("next", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id_hash", name=op.f("pk_login_flows")),
    )
    # Default privileges (0004) granted DML on the new tables; identities are written only by
    # tabayyun_login(), as users are.
    op.execute(f"REVOKE INSERT, UPDATE, DELETE ON user_identities FROM {APP_ROLE}")
    op.execute(LOGIN_FUNCTION)


def downgrade() -> None:
    """Reverse order."""
    op.execute("DROP FUNCTION tabayyun_login(text, text, text, boolean, text, text)")
    op.drop_table("login_flows")
    op.drop_index("ix_sessions_idp_sid", table_name="sessions")
    op.drop_index("ix_sessions_user_id", table_name="sessions")
    op.drop_table("sessions")
    op.drop_index("ix_user_identities_user_id", table_name="user_identities")
    op.drop_table("user_identities")
