"""Shared fixtures of the admin tests (spec 014): people with fixed ids and clients acting as them.

The app runs in dev auth mode with the principal overridden, so the passkey gate passes; the gate
itself is tested in oidc mode (`tests/db/test_admin_gate.py`).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection

from tabayyun.authz import BOOTSTRAP_PRINCIPAL, Principal, get_principal
from tabayyun.db.models import DEFAULT_ORG_ID, DEFAULT_WORKSPACE_ID
from tabayyun.main import create_app
from tenancy import app_settings

ALICE = uuid.UUID("00000000-0000-0000-0000-0000000000d1")  # owner
ADAM = uuid.UUID("00000000-0000-0000-0000-0000000000d2")  # admin
MIA = uuid.UUID("00000000-0000-0000-0000-0000000000d3")  # member, viewer of the default workspace
NED = uuid.UUID("00000000-0000-0000-0000-0000000000d4")  # member without any grant
ORG_B = uuid.UUID("00000000-0000-0000-0000-00000000000b")
WORKSPACE_B = uuid.UUID("00000000-0000-0000-0000-0000000000b2")
BOB = uuid.UUID("00000000-0000-0000-0000-0000000000b3")  # owner of org B
EMAILS = {
    ALICE: "alice@example.test",
    ADAM: "adam@example.test",
    MIA: "mia@example.test",
    NED: "ned@example.test",
}
CSRF = {"X-Tabayyun-Request": "1"}


@dataclass
class Env:
    """The owner connection (no RLS) and a factory of clients acting as a user."""

    owner: Connection
    client: Callable[..., AsyncClient]
    apps: list[Any]

    def sql(self, statement: str, **params: Any) -> Any:
        result = self.owner.execute(text(statement), params)
        return result.all() if result.returns_rows else None

    def scalar(self, statement: str, **params: Any) -> Any:
        return self.owner.execute(text(statement), params).scalar()

    def events(self, action: str | None = None) -> list[Any]:
        """Audit events oldest first, optionally of one action."""
        rows = self.sql("SELECT * FROM audit_events ORDER BY created_at, id")
        return [r for r in rows if action is None or r.action == action]


def seed_people(owner: Connection) -> None:
    """Alice (owner), Adam (admin), Mia (member, viewer of the default workspace) and Ned (member)
    in the default org; Bob owns org B with its own workspace."""
    run = lambda sql, **p: owner.execute(text(sql), p)  # noqa: E731
    for user, email in [*EMAILS.items(), (BOB, "bob@example.test")]:
        run(
            "INSERT INTO users (id, email, display_name) VALUES (:id, :e, :n)",
            id=user,
            e=email,
            n=email.split("@")[0],
        )
    for user, role in ((ALICE, "owner"), (ADAM, "admin"), (MIA, "member"), (NED, "member")):
        run(
            "INSERT INTO org_memberships (org_id, user_id, role) VALUES (:o, :u, :r)",
            o=DEFAULT_ORG_ID,
            u=user,
            r=role,
        )
    run(
        "INSERT INTO workspace_memberships (org_id, workspace_id, user_id, role) "
        "VALUES (:o, :w, :u, 'viewer')",
        o=DEFAULT_ORG_ID,
        w=DEFAULT_WORKSPACE_ID,
        u=MIA,
    )
    run("INSERT INTO orgs (id, name) VALUES (:id, 'org-b')", id=ORG_B)
    run("INSERT INTO workspaces (id, org_id, name) VALUES (:id, :o, 'ws-b')", id=WORKSPACE_B, o=ORG_B)
    run("INSERT INTO org_memberships (org_id, user_id, role) VALUES (:o, :u, 'owner')", o=ORG_B, u=BOB)


@pytest.fixture
async def admin_env(db_url, fresh_schema) -> AsyncIterator[Env]:
    """A fresh schema with `seed_people`; clients act as a user (the bootstrap owner by default)."""
    fresh_schema("auto")
    engine = create_engine(db_url, isolation_level="AUTOCOMMIT")
    owner = engine.connect()
    seed_people(owner)
    apps = []

    def client(
        user: uuid.UUID | None = None,
        *,
        org: uuid.UUID = DEFAULT_ORG_ID,
        headers: dict[str, str] | None = None,
        **settings: Any,
    ) -> AsyncClient:
        """A client acting as `user`; `settings` override the app's (e.g. SMTP, inline jobs)."""
        app = create_app(app_settings(db_url, **({"inline_jobs": True} | settings)))
        principal = BOOTSTRAP_PRINCIPAL if user is None else Principal(user_id=user, org_id=org)
        app.dependency_overrides[get_principal] = lambda: principal
        apps.append(app)
        return AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers=CSRF | (headers or {})
        )

    yield Env(owner=owner, client=client, apps=apps)
    for app in apps:
        await app.state.engine.dispose()
    owner.close()
    engine.dispose()
