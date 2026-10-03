"""The request's workspace (spec 014): `X-Tabayyun-Workspace`, its fallback, and the picker list.

The app runs as the app login with only the principal overridden, so `get_workspace_id` and
`require()` run as in production.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, text

from tabayyun.authz import BOOTSTRAP_PRINCIPAL, Principal, get_principal
from tabayyun.db.models import DEFAULT_ORG_ID, DEFAULT_WORKSPACE_ID
from tabayyun.main import create_app
from tenancy import app_settings

NORTH = uuid.UUID("00000000-0000-0000-0000-0000000000c1")
SOUTH = uuid.UUID("00000000-0000-0000-0000-0000000000c2")
VIEWER = uuid.UUID("00000000-0000-0000-0000-0000000000c3")  # direct viewer of North only
TEAMMATE = uuid.UUID("00000000-0000-0000-0000-0000000000c4")  # editor of South through a team
NOBODY = uuid.UUID("00000000-0000-0000-0000-0000000000c5")  # member without any grant
TEAM = uuid.UUID("00000000-0000-0000-0000-0000000000c6")
CSV = b"ts,value\n2026-01-01T00:00:00Z,1\n2026-01-01T01:00:00Z,2\n2026-01-01T02:00:00Z,3\n"


@pytest.fixture
def owner_sql(db_url, fresh_schema):
    """A fresh schema with workspaces North (older) and South, and three users of the default org."""
    fresh_schema("auto")
    engine = create_engine(db_url, isolation_level="AUTOCOMMIT")
    conn = engine.connect()

    def run(statement: str, **params: Any) -> None:
        conn.execute(text(statement), params)

    for ws, name, age in ((NORTH, "Plant North", "2 days"), (SOUTH, "Plant South", "1 day")):
        run(
            "INSERT INTO workspaces (id, org_id, name, created_at) "
            "VALUES (:id, :org, :name, now() - CAST(:age AS interval))",
            id=ws,
            org=DEFAULT_ORG_ID,
            name=name,
            age=age,
        )
    for user in (VIEWER, TEAMMATE, NOBODY):
        run(
            "INSERT INTO users (id, email, display_name) VALUES (:id, :e, :e)",
            id=user,
            e=f"{user.hex[-2:]}@x.test",
        )
        run(
            "INSERT INTO org_memberships (org_id, user_id, role) VALUES (:org, :u, 'member')",
            org=DEFAULT_ORG_ID,
            u=user,
        )
    run(
        "INSERT INTO workspace_memberships (org_id, workspace_id, user_id, role) "
        "VALUES (:org, :ws, :u, 'viewer')",
        org=DEFAULT_ORG_ID,
        ws=NORTH,
        u=VIEWER,
    )
    run("INSERT INTO teams (id, org_id, name) VALUES (:id, :org, 'ops')", id=TEAM, org=DEFAULT_ORG_ID)
    run(
        "INSERT INTO team_members (org_id, team_id, user_id) VALUES (:org, :t, :u)",
        org=DEFAULT_ORG_ID,
        t=TEAM,
        u=TEAMMATE,
    )
    run(
        "INSERT INTO workspace_team_roles (org_id, workspace_id, team_id, role) "
        "VALUES (:org, :ws, :t, 'editor')",
        org=DEFAULT_ORG_ID,
        ws=SOUTH,
        t=TEAM,
    )
    yield run
    conn.close()
    engine.dispose()


@pytest.fixture
async def client_for(db_url, owner_sql):
    """Factory of clients acting as a user of the default org (the bootstrap owner by default)."""
    apps = []

    def make(user: uuid.UUID | None = None, workspace: str | None = None) -> AsyncClient:
        app = create_app(app_settings(db_url, inline_jobs=True))
        principal = BOOTSTRAP_PRINCIPAL if user is None else Principal(user_id=user, org_id=DEFAULT_ORG_ID)
        app.dependency_overrides[get_principal] = lambda: principal
        apps.append(app)
        headers = {"X-Tabayyun-Request": "1"}
        if workspace is not None:
            headers["X-Tabayyun-Workspace"] = workspace
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers)

    yield make
    for app in apps:
        await app.state.engine.dispose()


async def _upload(client: AsyncClient, series: str) -> None:
    r = await client.post("/api/runs", files={"file": ("f.csv", CSV, "text/csv")}, data={"series_id": series})
    assert r.status_code == 202, r.text


async def _series(client: AsyncClient) -> Any:
    r = await client.get("/api/series")
    return r.status_code, sorted(s["external_id"] for s in r.json().get("items", []))


async def test_the_header_selects_the_workspace(client_for):
    """Data written with North's header is listed in North only; without a header the owner
    acts in the default workspace."""
    async with client_for(workspace=str(NORTH)) as north, client_for() as default:
        await _upload(north, "north-1")
        await _upload(default, "default-1")
        assert await _series(north) == (200, ["north-1"])
        assert await _series(default) == (200, ["default-1"])


async def test_a_malformed_header_is_400(client_for):
    async with client_for(workspace="not-a-uuid") as c:
        r = await c.get("/api/series")
    assert (r.status_code, r.json()["detail"]) == (400, "invalid_workspace")


async def test_a_workspace_without_a_role_is_404(client_for):
    """The viewer of North names South, or the default workspace: neither exists for them."""
    for ws in (SOUTH, DEFAULT_WORKSPACE_ID, uuid.uuid4()):
        async with client_for(VIEWER, workspace=str(ws)) as c:
            assert (await c.get("/api/series")).status_code == 404, ws


@pytest.mark.parametrize(("user", "expected"), [(VIEWER, NORTH), (TEAMMATE, SOUTH)])
async def test_without_a_header_the_first_visible_workspace_is_used(client_for, user, expected):
    """Not seeing the default workspace, a user acts in the oldest workspace they can see."""
    async with client_for(workspace=str(expected)) as owner:
        await _upload(owner, f"in-{expected.hex[-2:]}")
    async with client_for(user) as c:
        assert await _series(c) == (200, [f"in-{expected.hex[-2:]}"])


async def test_the_fallback_follows_the_data(client_for):
    """A run uploaded without a header by the teammate lands in South, where they are editor."""
    async with client_for(TEAMMATE) as c:
        await _upload(c, "south-1")
    async with client_for(workspace=str(SOUTH)) as south:
        assert await _series(south) == (200, ["south-1"])


async def test_a_member_without_grants_sees_nothing(client_for):
    async with client_for(NOBODY) as c:
        assert (await c.get("/api/workspaces")).json() == []
        assert (await c.get("/api/series")).status_code == 404


@pytest.mark.parametrize(
    ("user", "expected"),
    [
        (None, [("Plant North", "admin"), ("Plant South", "admin"), ("default", "admin")]),
        (VIEWER, [("Plant North", "viewer")]),
        (TEAMMATE, [("Plant South", "editor")]),
    ],
    ids=["owner", "viewer", "team-editor"],
)
async def test_workspaces_lists_the_visible_ones_with_roles(client_for, user, expected):
    async with client_for(user) as c:
        body = (await c.get("/api/workspaces")).json()
    assert [(w["name"], w["role"]) for w in body] == expected
    assert all(set(w) == {"id", "name", "timezone", "role"} for w in body)
