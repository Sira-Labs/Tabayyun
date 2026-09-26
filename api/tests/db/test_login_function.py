"""`tabayyun_login()` (migration 0005, spec 013): the only write path to users and identities.

Every call runs as the app login, as the API does.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError

from tabayyun.db.models import BOOTSTRAP_USER_ID, DEFAULT_ORG_ID
from tenancy import app_url

ISSUER = "https://keycloak.test/realms/tabayyun"
ADMIN = "owner@example.org"
INSUFFICIENT_PRIVILEGE = "42501"
INVALID_PARAMETER = "22023"


def login(conn: Connection, subject: str, email: str, *, verified: bool = True, admin: str | None = ADMIN):
    """One call of the function; returns (user_id, org_id, role, disabled)."""
    return conn.execute(
        text("SELECT * FROM tabayyun_login(:iss, :sub, :email, :verified, :name, :admin)"),
        {"iss": ISSUER, "sub": subject, "email": email, "verified": verified, "name": "", "admin": admin},
    ).one()


@pytest.fixture
def app_conn(db_url, fresh_schema):
    """A connection as the app login on a freshly migrated schema, committing each statement."""
    fresh_schema("auto")
    engine = create_engine(app_url(db_url), isolation_level="AUTOCOMMIT")
    with engine.connect() as conn:
        yield conn
    engine.dispose()


def test_new_email_creates_user_without_access(app_conn):
    """An unknown verified email gets a user and an identity, but no membership."""
    user_id, org_id, role, disabled = login(app_conn, "sub-bo", "Bo@Example.org")
    assert org_id is None and role is None and disabled is False
    email = app_conn.execute(text("SELECT email FROM users WHERE id = :id"), {"id": user_id}).scalar_one()
    assert email == "bo@example.org"
    # The same identity again is the same user.
    assert login(app_conn, "sub-bo", "bo@example.org")[0] == user_id


def test_admin_email_becomes_owner_once(app_conn):
    """The admin email becomes owner of the default org while it has no owner but the
    bootstrap user; after that, a changed setting grants nothing."""
    user_id, org_id, role, _ = login(app_conn, "sub-owner", ADMIN)
    assert (org_id, role) == (DEFAULT_ORG_ID, "owner")
    # A second identity (e.g. GitHub) with the same email links to the same user.
    assert login(app_conn, "sub-owner-github", ADMIN)[0:3] == (user_id, DEFAULT_ORG_ID, "owner")
    # Pointing the setting at someone else later does not make them owner.
    assert login(app_conn, "sub-other", "other@example.org", admin="other@example.org")[1] is None
    # RLS hides org_memberships without a tenant context; read them in the default org.
    app_conn.execute(text("SELECT set_config('app.org_id', :org, false)"), {"org": str(DEFAULT_ORG_ID)})
    owners = (
        app_conn.execute(
            text("SELECT user_id FROM org_memberships WHERE role = 'owner' AND user_id <> :boot"),
            {"boot": BOOTSTRAP_USER_ID},
        )
        .scalars()
        .all()
    )
    assert owners == [user_id]


def test_no_admin_email_grants_nothing(app_conn):
    """Without a configured admin email nobody becomes owner."""
    assert login(app_conn, "sub-owner", ADMIN, admin=None)[1] is None
    assert login(app_conn, "sub-owner", ADMIN, admin="")[1] is None


def test_unverified_email_is_refused(app_conn):
    """An unverified or empty email never creates or links a user."""
    for email, verified in ((ADMIN, False), ("", True)):
        with pytest.raises(DBAPIError) as err:
            login(app_conn, f"sub-{uuid.uuid4()}", email, verified=verified)
        assert err.value.orig.sqlstate == INVALID_PARAMETER
    assert app_conn.execute(text("SELECT count(*) FROM user_identities")).scalar_one() == 0


def test_disabled_user_is_reported(db_url, app_conn):
    """A disabled user still resolves, flagged, so the API can refuse and revoke."""
    user_id = login(app_conn, "sub-bo", "bo@example.org")[0]
    owner = create_engine(db_url, isolation_level="AUTOCOMMIT")
    with owner.connect() as conn:
        conn.execute(text("UPDATE users SET disabled_at = now() WHERE id = :id"), {"id": user_id})
    owner.dispose()
    assert login(app_conn, "sub-bo", "bo@example.org")[3] is True


def test_function_is_the_only_users_write_path(app_conn):
    """The app login cannot write users or identities directly."""
    for statement in (
        "INSERT INTO users (id, email, display_name) VALUES (gen_random_uuid(), 'x@example.org', 'x')",
        "UPDATE users SET display_name = 'x'",
        f"INSERT INTO user_identities (issuer, subject, user_id, email_at_login) "
        f"VALUES ('i', 's', '{BOOTSTRAP_USER_ID}', 'x@example.org')",
    ):
        with pytest.raises(DBAPIError) as err:
            app_conn.execute(text(statement))
        assert err.value.orig.sqlstate == INSUFFICIENT_PRIVILEGE, statement
