"""The database login the app runs as (spec 007, ADR-0007).

Row-level security only protects anything when the api and the worker log in as a role that
is neither a superuser, nor exempt from RLS, nor the owner of the tables. Migrations run as the
owner (`TABAYYUN_MIGRATION_DATABASE_URL`); `ensure_app_login` then makes the user named in
`TABAYYUN_DATABASE_URL` a `LOGIN` member of `tabayyun_app` with that URL's password, so an
operator only picks the password. `login_problems` is the startup check.
"""

from __future__ import annotations

import psycopg
import structlog
from psycopg import sql
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

log = structlog.get_logger()

APP_ROLE = "tabayyun_app"
# Any tenant table: the owner of one owns them all (the migrations create them).
OWNED_TABLE = "runs"

INSUFFICIENT_PRIVILEGE = "42501"

LOGIN_FACTS = text(
    "SELECT r.rolsuper, r.rolbypassrls, "
    "EXISTS (SELECT FROM pg_tables WHERE schemaname = 'public' AND tablename = :table "
    "AND tableowner = current_user) "
    "FROM pg_roles r WHERE r.rolname = current_user"
)


async def login_problems(engine: AsyncEngine) -> list[str] | None:
    """Why the current login bypasses RLS (empty when it does not), or None when unreachable."""
    try:
        async with engine.connect() as conn:
            row = (await conn.execute(LOGIN_FACTS, {"table": OWNED_TABLE})).one()
    except (SQLAlchemyError, OSError) as exc:
        log.warning("db.login_unverified", error=str(exc).splitlines()[0] if str(exc) else type(exc).__name__)
        return None
    superuser, bypass_rls, owner = row
    problems = []
    if superuser:
        problems.append("superuser")
    if bypass_rls:
        problems.append("bypassrls")
    if owner:
        problems.append("table owner")
    return problems


async def check_login(engine: AsyncEngine, env: str) -> list[str] | None:
    """Log `db.rls_bypassed` (error in prod, warning elsewhere) when the login bypasses RLS.

    Spec 015 turns the prod error into a refusal to start; until then the process keeps
    serving so a release merged before the operator switched logins causes no outage.
    """
    problems = await login_problems(engine)
    if problems:
        emit = log.error if env == "prod" else log.warning
        emit(
            "db.rls_bypassed",
            problems=problems,
            hint="set TABAYYUN_DATABASE_URL to a login in tabayyun_app and "
            "TABAYYUN_MIGRATION_DATABASE_URL to the owner (deploy/README.md)",
        )
    return problems


def ensure_app_login(owner_url: str, app_url: str) -> str | None:
    """Create or update the app URL's user as a LOGIN member of `tabayyun_app` (or, when the
    URL names `tabayyun_app` itself, let that role log in).

    Returns the user name it provisioned, or None when the app URL uses the owner's user (a
    single dev login: nothing to create). Idempotent: an existing user gets the URL's
    password again and the membership; its other attributes are left alone, so a superuser
    named here stays one and the startup check reports it.
    """
    owner, app = make_url(owner_url), make_url(app_url)
    if not app.username or app.username == owner.username:
        return None
    if not app.password:
        raise ValueError("TABAYYUN_DATABASE_URL must carry a password for the app login")
    user = sql.Identifier(app.username)
    password = sql.Literal(str(app.password))
    conninfo = owner.set(drivername="postgresql").render_as_string(hide_password=False)
    with psycopg.connect(conninfo, autocommit=True) as conn:
        if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (APP_ROLE,)).fetchone():
            log.warning("db.app_role_missing", hint="migrate to head first (revision 0004 creates it)")
            return None
        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (app.username,)).fetchone()
        if exists:
            conn.execute(sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(user, password))
        else:
            conn.execute(
                sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD {}").format(user, password)
            )
        if app.username != APP_ROLE:  # the role itself may be the login: no self-membership
            conn.execute(sql.SQL("GRANT {} TO {}").format(sql.Identifier(APP_ROLE), user))
    log.info("db.app_login_ready", user=app.username)
    return app.username


def is_rls_violation(exc: DBAPIError) -> bool:
    """Whether Postgres refused a row because of a row-level security policy."""
    refused = getattr(exc.orig, "sqlstate", None) == INSUFFICIENT_PRIVILEGE
    return refused and "row-level security" in str(exc.orig)
