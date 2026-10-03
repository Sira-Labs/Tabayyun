"""`tabayyun_accept_invitations()` and the login that calls it (migration 0006, spec 014).

Invitations are seeded as the owner; the functions run as the app login, as the API does.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError

from tabayyun.db.models import BOOTSTRAP_USER_ID, DEFAULT_ORG_ID, DEFAULT_WORKSPACE_ID
from tenancy import app_url

ISSUER = "https://keycloak.test/realms/tabayyun"
INSUFFICIENT_PRIVILEGE = "42501"


@pytest.fixture
def conns(db_url, fresh_schema):
    """(owner, app) connections on a freshly migrated schema, each committing every statement."""
    fresh_schema("auto")
    owner = create_engine(db_url, isolation_level="AUTOCOMMIT")
    app = create_engine(app_url(db_url), isolation_level="AUTOCOMMIT")
    with owner.connect() as o, app.connect() as a:
        yield o, a
    owner.dispose()
    app.dispose()


def invite(owner: Connection, email: str, **fields: Any) -> uuid.UUID:
    """An open invitation in the default org, by the bootstrap user, valid for a day unless told."""
    row = {
        "id": uuid.uuid4(),
        "email": email,
        "org_role": "member",
        "workspace_id": None,
        "workspace_role": None,
        "expires": "1 day",
        "accepted_at": None,
        "revoked_at": None,
    } | fields
    owner.execute(
        text(
            "INSERT INTO invitations (id, org_id, email, org_role, workspace_id, workspace_role, "
            "invited_by, expires_at, accepted_at, revoked_at) VALUES (:id, :org, :email, :org_role, "
            ":workspace_id, :workspace_role, :boot, now() + CAST(:expires AS interval), "
            ":accepted_at, :revoked_at)"
        ),
        row | {"org": DEFAULT_ORG_ID, "boot": BOOTSTRAP_USER_ID},
    )
    return row["id"]


def login(app: Connection, subject: str, email: str):
    """Spec 013's login; returns (user_id, org_id, role, disabled)."""
    return app.execute(
        text("SELECT * FROM tabayyun_login(:iss, :sub, :email, true, '', NULL)"),
        {"iss": ISSUER, "sub": subject, "email": email},
    ).one()


def roles(owner: Connection, user_id: uuid.UUID) -> tuple[str | None, str | None]:
    """The user's (org role, direct role in the default workspace)."""
    org_role = owner.execute(
        text("SELECT role FROM org_memberships WHERE org_id = :org AND user_id = :u"),
        {"org": DEFAULT_ORG_ID, "u": user_id},
    ).scalar()
    ws_role = owner.execute(
        text("SELECT role FROM workspace_memberships WHERE workspace_id = :ws AND user_id = :u"),
        {"ws": DEFAULT_WORKSPACE_ID, "u": user_id},
    ).scalar()
    return org_role, ws_role


def test_login_accepts_a_matching_invitation(conns):
    """The invited email signs in and joins with the invited org and workspace roles; the
    invitation is accepted and a `member.joined` event names it."""
    owner, app = conns
    invitation = invite(owner, "ada@example.org", workspace_id=DEFAULT_WORKSPACE_ID, workspace_role="editor")
    user_id, org_id, role, _ = login(app, "sub-ada", "Ada@Example.org")
    assert (org_id, role) == (DEFAULT_ORG_ID, "member")
    assert roles(owner, user_id) == ("member", "editor")
    accepted = owner.execute(
        text("SELECT accepted_user_id, accepted_at IS NOT NULL FROM invitations WHERE id = :id"),
        {"id": invitation},
    ).one()
    assert tuple(accepted) == (user_id, True)
    event = owner.execute(
        text("SELECT actor_user_id, action, target_id, workspace_id, details FROM audit_events")
    ).one()
    assert event.actor_user_id is None and event.action == "member.joined"
    assert event.target_id == str(user_id) and event.workspace_id == DEFAULT_WORKSPACE_ID
    assert event.details["invitation_id"] == str(invitation)
    assert event.details["email"] == "ada@example.org"


@pytest.mark.parametrize(
    "fields",
    [
        {"expires": "-1 minute"},
        {"revoked_at": "2026-10-01T00:00:00Z"},
        {"accepted_at": "2026-10-01T00:00:00Z"},
    ],
    ids=["expired", "revoked", "accepted"],
)
def test_closed_invitations_grant_nothing(conns, fields):
    """Expired, revoked and already accepted invitations are ignored."""
    owner, app = conns
    invite(owner, "ada@example.org", **fields)
    assert login(app, "sub-ada", "ada@example.org")[1] is None
    assert owner.execute(text("SELECT count(*) FROM audit_events")).scalar_one() == 0


def test_another_email_does_not_join(conns):
    """An invitation is bound to its email."""
    owner, app = conns
    invite(owner, "ada@example.org")
    assert login(app, "sub-bo", "bo@example.org")[1] is None


def test_a_higher_existing_role_is_kept_and_a_lower_one_raised(conns):
    """Accepting never lowers a role: an admin invited as member stays admin; a workspace viewer
    invited as editor becomes editor."""
    owner, app = conns
    user_id = login(app, "sub-ada", "ada@example.org")[0]
    owner.execute(
        text("INSERT INTO org_memberships (org_id, user_id, role) VALUES (:org, :u, 'admin')"),
        {"org": DEFAULT_ORG_ID, "u": user_id},
    )
    owner.execute(
        text(
            "INSERT INTO workspace_memberships (org_id, workspace_id, user_id, role) "
            "VALUES (:org, :ws, :u, 'viewer')"
        ),
        {"org": DEFAULT_ORG_ID, "ws": DEFAULT_WORKSPACE_ID, "u": user_id},
    )
    invite(owner, "ada@example.org", workspace_id=DEFAULT_WORKSPACE_ID, workspace_role="editor")
    assert app.execute(text("SELECT tabayyun_accept_invitations(:u)"), {"u": user_id}).scalar_one() == 1
    assert roles(owner, user_id) == ("admin", "editor")


def test_disabled_user_accepts_nothing(conns):
    """A disabled user does not join, and the invitation stays open."""
    owner, app = conns
    user_id = login(app, "sub-ada", "ada@example.org")[0]
    owner.execute(text("UPDATE users SET disabled_at = now() WHERE id = :u"), {"u": user_id})
    invite(owner, "ada@example.org")
    assert app.execute(text("SELECT tabayyun_accept_invitations(:u)"), {"u": user_id}).scalar_one() == 0
    assert owner.execute(text("SELECT count(*) FROM invitations WHERE accepted_at IS NULL")).scalar_one() == 1


def test_audit_events_are_append_only_for_the_app(conns):
    """The app login may insert and read audit events in its org, never change or delete them."""
    owner, app = conns
    invite(owner, "ada@example.org")
    login(app, "sub-ada", "ada@example.org")
    app.execute(text("SELECT set_config('app.org_id', :org, false)"), {"org": str(DEFAULT_ORG_ID)})
    assert app.execute(text("SELECT count(*) FROM audit_events")).scalar_one() == 1
    for statement in (
        "UPDATE audit_events SET action = 'x'",
        "DELETE FROM audit_events",
        "TRUNCATE audit_events",
    ):
        with pytest.raises(DBAPIError) as err:
            app.execute(text(statement))
        assert err.value.orig.sqlstate == INSUFFICIENT_PRIVILEGE, statement


def test_invitations_cannot_be_deleted_by_the_app(conns):
    """Invitations are revoked, not deleted."""
    owner, app = conns
    invite(owner, "ada@example.org")
    app.execute(text("SELECT set_config('app.org_id', :org, false)"), {"org": str(DEFAULT_ORG_ID)})
    with pytest.raises(DBAPIError) as err:
        app.execute(text("DELETE FROM invitations"))
    assert err.value.orig.sqlstate == INSUFFICIENT_PRIVILEGE


def test_one_open_invitation_per_email(conns):
    """A second open invitation for the same org and email is refused; a revoked one is not counted."""
    owner, _ = conns
    invite(owner, "ada@example.org", revoked_at="2026-10-01T00:00:00Z")
    invite(owner, "ada@example.org")
    with pytest.raises(DBAPIError):
        invite(owner, "ada@example.org")
