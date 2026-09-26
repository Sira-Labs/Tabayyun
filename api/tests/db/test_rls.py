"""Row-level security and authorization across tenants (spec 007).

One module-scoped seed fills every tenant table for the default org (A) through the API,
adds a second org (B) with its own data, and memberships for users with lower roles. The app
runs as the non-owner test login throughout; the owner connection only seeds and inspects.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import DBAPIError

from synth_csv import faulty_csv
from tabayyun.authz import Principal, get_principal, get_workspace_id, visible_workspaces
from tabayyun.db import Base, for_org, make_engine, make_session_factory, migrate
from tabayyun.db.models import BOOTSTRAP_USER_ID, DEFAULT_ORG_ID, DEFAULT_WORKSPACE_ID
from tabayyun.main import create_app
from tenancy import app_settings, app_url

ORG_B = uuid.UUID("00000000-0000-0000-0000-00000000000b")
WORKSPACE_B = uuid.UUID("00000000-0000-0000-0000-0000000000b2")
USER_B = uuid.UUID("00000000-0000-0000-0000-0000000000b3")
VIEWER = uuid.UUID("00000000-0000-0000-0000-0000000000a4")  # direct viewer in workspace A
TEAM_EDITOR = uuid.UUID("00000000-0000-0000-0000-0000000000a5")  # editor through a team
BYSTANDER = uuid.UUID("00000000-0000-0000-0000-0000000000a6")  # org member, no workspace grant
DISABLED = uuid.UUID("00000000-0000-0000-0000-0000000000a7")  # org admin, but disabled
TEAM_A = uuid.UUID("00000000-0000-0000-0000-0000000000a8")
HOUR0 = datetime(2026, 1, 1, tzinfo=UTC)
# `sessions` has `org_id` but no row-level security: it is looked up before any org is known
# (spec 013), and only by the HMAC of the cookie's token.
NOT_TENANT = {"sessions"}
TENANT_TABLES = sorted(
    ["orgs", *(t.name for t in Base.metadata.sorted_tables if "org_id" in t.c and t.name not in NOT_TENANT)],
    key=str,
)
INSUFFICIENT_PRIVILEGE = "42501"
UNIQUE_VIOLATION = "23505"


@dataclass
class Seed:
    """Ids of the seeded rows, per org."""

    owner_url: str
    a: dict[str, str] = field(default_factory=dict)
    b: dict[str, str] = field(default_factory=dict)


def _hourly_csv(hours: int = 24) -> bytes:
    rows = ["ts,value"] + [f"{(HOUR0 + timedelta(hours=h)).isoformat()},{h % 7}" for h in range(hours)]
    return ("\n".join(rows) + "\n").encode()


async def _upload(client: AsyncClient, series: str, csv: bytes) -> dict[str, Any]:
    r = await client.post("/api/runs", files={"file": ("f.csv", csv, "text/csv")}, data={"series_id": series})
    assert r.status_code == 202, r.text
    return (await client.get(f"/api/runs/{r.json()['id']}")).json()


async def _seed_org(app: Any, prefix: str) -> dict[str, str]:
    """Series, runs, findings, metrics, scores, coverage, a group and a dataset through the API."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers={"X-Tabayyun-Request": "1"}
    ) as c:
        run = await _upload(c, f"{prefix}-1", faulty_csv(["gap", "flatline"]))
        assert run["status"] == "succeeded", run
        other = await _upload(c, f"{prefix}-2", _hourly_csv())
        s1, s2 = run["series"][0]["id"], other["series"][0]["id"]
        finding = (await c.get("/api/findings", params={"series_id": s1})).json()["items"][0]["id"]
        group = await c.post(
            "/api/series-groups",
            json={
                "name": f"{prefix}-group",
                "kind": "related",
                "members": [{"series_id": s1, "role": "member"}, {"series_id": s2, "role": "member"}],
            },
        )
        assert group.status_code == 201, group.text
        dataset = await c.post(
            "/api/datasets", json={"name": f"{prefix}-set", "series_ids": [s1, s2], "window": {"last": "24h"}}
        )
        assert dataset.status_code == 201, dataset.text
    return {
        "run": run["id"],
        "series": s1,
        "finding": finding,
        "group": group.json()["id"],
        "dataset": dataset.json()["id"],
    }


def _owner_sql(owner: Engine, sql: str, **params: Any) -> None:
    with owner.begin() as conn:
        conn.execute(text(sql), params)


def _seed_memberships(owner: Engine) -> None:
    """Org B with its owner, and users of org A with lower or no roles (as the owner: no RLS)."""
    _owner_sql(owner, "INSERT INTO orgs (id, name) VALUES (:id, 'org-b')", id=ORG_B)
    _owner_sql(
        owner,
        "INSERT INTO workspaces (id, org_id, name) VALUES (:id, :org, 'ws-b')",
        id=WORKSPACE_B,
        org=ORG_B,
    )
    for user, email in [
        (USER_B, "b@example.test"),
        (VIEWER, "viewer@example.test"),
        (TEAM_EDITOR, "editor@example.test"),
        (BYSTANDER, "bystander@example.test"),
        (DISABLED, "disabled@example.test"),
    ]:
        _owner_sql(
            owner,
            "INSERT INTO users (id, email, display_name) VALUES (:id, :email, :email)",
            id=user,
            email=email,
        )
    _owner_sql(owner, "UPDATE users SET disabled_at = now() WHERE id = :id", id=DISABLED)
    memberships = [(ORG_B, USER_B, "owner")] + [
        (DEFAULT_ORG_ID, u, r)
        for u, r in [(VIEWER, "member"), (TEAM_EDITOR, "member"), (BYSTANDER, "member"), (DISABLED, "admin")]
    ]
    for org, user, role in memberships:
        _owner_sql(
            owner,
            "INSERT INTO org_memberships (org_id, user_id, role) VALUES (:org, :user, :role)",
            org=org,
            user=user,
            role=role,
        )
    _owner_sql(
        owner,
        "INSERT INTO workspace_memberships (org_id, workspace_id, user_id, role) "
        "VALUES (:org, :ws, :user, 'viewer')",
        org=DEFAULT_ORG_ID,
        ws=DEFAULT_WORKSPACE_ID,
        user=VIEWER,
    )
    _owner_sql(
        owner, "INSERT INTO teams (id, org_id, name) VALUES (:id, :org, 'ops')", id=TEAM_A, org=DEFAULT_ORG_ID
    )
    _owner_sql(
        owner,
        "INSERT INTO team_members (org_id, team_id, user_id) VALUES (:org, :team, :user)",
        org=DEFAULT_ORG_ID,
        team=TEAM_A,
        user=TEAM_EDITOR,
    )
    _owner_sql(
        owner,
        "INSERT INTO workspace_team_roles (org_id, workspace_id, team_id, role) "
        "VALUES (:org, :ws, :team, 'editor')",
        org=DEFAULT_ORG_ID,
        ws=DEFAULT_WORKSPACE_ID,
        team=TEAM_A,
    )


def _as(app: Any, user: uuid.UUID, org: uuid.UUID, workspace: uuid.UUID) -> Any:
    """Make every request of `app` act as `user` in `org` and `workspace`."""
    app.dependency_overrides[get_principal] = lambda: Principal(user_id=user, org_id=org)
    app.dependency_overrides[get_workspace_id] = lambda: workspace
    return app


async def _seed(owner_url: str, cache_dir: str) -> Seed:
    seed = Seed(owner_url=owner_url)
    owner = create_engine(owner_url)
    try:
        _seed_memberships(owner)
    finally:
        owner.dispose()
    app_a = create_app(app_settings(owner_url, inline_jobs=True, cache_url=cache_dir))
    app_b = _as(
        create_app(app_settings(owner_url, inline_jobs=True, cache_url=cache_dir)), USER_B, ORG_B, WORKSPACE_B
    )
    queued = create_app(app_settings(owner_url, inline_jobs=False))
    try:
        seed.a = await _seed_org(app_a, "a")
        seed.b = await _seed_org(app_b, "b")
        # A queued run keeps its upload row (and its job): the uploads table gets a row.
        async with AsyncClient(
            transport=ASGITransport(app=queued), base_url="http://test", headers={"X-Tabayyun-Request": "1"}
        ) as c:
            seed.a["queued_run"] = (await _upload(c, "a-queued", _hourly_csv()))["id"]
    finally:
        for app in (app_a, app_b, queued):
            await app.state.engine.dispose()
    return seed


@pytest.fixture(scope="module")
def seed(db_url, tmp_path_factory) -> Iterator[Seed]:
    """Two orgs with data in every tenant table; the schema is rebuilt for this module."""
    migrate.downgrade(db_url, "base", timescale="auto")
    migrate.upgrade(db_url, "head", timescale="auto", app_database_url=app_url(db_url))
    yield asyncio.run(_seed(db_url, str(tmp_path_factory.mktemp("cache"))))


# Policies, table by table


def _sqlstate(exc: DBAPIError) -> str | None:
    return getattr(exc.orig, "sqlstate", None)


def _key(table: str) -> str:
    return "id" if table == "orgs" else "org_id"


def _one_row(seed: Seed, table: str) -> dict[str, Any]:
    """One row of org A in `table`, read as the owner."""
    owner = create_engine(seed.owner_url)
    try:
        with owner.connect() as conn:
            row = conn.execute(
                text(f"SELECT row_to_json(t)::text FROM {table} t WHERE {_key(table)} = :org LIMIT 1"),  # noqa: S608
                {"org": DEFAULT_ORG_ID},
            ).scalar()
    finally:
        owner.dispose()
    assert row is not None, f"the seed left {table} empty for org A"
    return json.loads(row)


def _as_app(seed: Seed, org: uuid.UUID | None, sql: str, **params: Any) -> Any:
    """Run one statement as the app login in `org`'s context (none when None); rolled back."""
    engine = create_engine(app_url(seed.owner_url))
    try:
        with engine.connect() as conn, conn.begin() as tx:
            if org is not None:
                conn.execute(text("SELECT set_config('app.org_id', :org, true)"), {"org": str(org)})
            try:
                result = conn.execute(text(sql), params)
                return result.rowcount if result.returns_rows is False else result.scalar()
            finally:
                tx.rollback()
    finally:
        engine.dispose()


def _insert_error(seed: Seed, org: uuid.UUID | None, table: str, row: dict[str, Any]) -> str | None:
    """SQLSTATE of re-inserting an existing row of org A in `org`'s context."""
    sql = f"INSERT INTO {table} SELECT * FROM json_populate_record(NULL::{table}, CAST(:row AS json))"  # noqa: S608
    try:
        _as_app(seed, org, sql, row=json.dumps(row))
    except DBAPIError as exc:
        return _sqlstate(exc)
    return None


def test_every_tenant_table_has_rls(seed):
    """Every table with `org_id` (and `orgs`) has RLS on; `users` has not. A new tenant table
    without a policy fails here."""
    owner = create_engine(seed.owner_url)
    try:
        with owner.connect() as conn:
            protected = set(conn.execute(text("SELECT relname FROM pg_class WHERE relrowsecurity")).scalars())
    finally:
        owner.dispose()
    assert set(TENANT_TABLES) <= protected
    assert "users" not in protected


@pytest.mark.parametrize("table", TENANT_TABLES)
def test_policy_without_context_hides_and_rejects(seed, table):
    """Without `app.org_id` a tenant table is empty and takes no rows."""
    row = _one_row(seed, table)
    assert _as_app(seed, None, f"SELECT count(*) FROM {table}") == 0  # noqa: S608
    assert _insert_error(seed, None, table, row) == INSUFFICIENT_PRIVILEGE


@pytest.mark.parametrize("table", TENANT_TABLES)
def test_policy_other_org_cannot_see_insert_update_or_delete(seed, table):
    """In org B's context org A's rows are invisible and untouchable."""
    row = _one_row(seed, table)
    key = _key(table)
    where = f"{key} = CAST(:a AS uuid)"
    a = str(DEFAULT_ORG_ID)
    assert _as_app(seed, ORG_B, f"SELECT count(*) FROM {table} WHERE {where}", a=a) == 0  # noqa: S608
    assert _insert_error(seed, ORG_B, table, row) == INSUFFICIENT_PRIVILEGE
    assert _as_app(seed, ORG_B, f"UPDATE {table} SET {key} = {key} WHERE {where}", a=a) == 0  # noqa: S608
    assert _as_app(seed, ORG_B, f"DELETE FROM {table} WHERE {where}", a=a) == 0  # noqa: S608


@pytest.mark.parametrize("table", TENANT_TABLES)
def test_policy_own_org_passes(seed, table):
    """In org A's context its rows are visible, and a duplicate insert gets past the policy to
    the unique constraint."""
    row = _one_row(seed, table)
    assert _as_app(seed, DEFAULT_ORG_ID, f"SELECT count(*) FROM {table}") > 0  # noqa: S608
    assert _insert_error(seed, DEFAULT_ORG_ID, table, row) == UNIQUE_VIOLATION


def test_chunks_are_not_readable_directly(seed, timescale_available):
    """A TimescaleDB chunk named directly shows nothing to the app login, with or without context."""
    if not timescale_available:
        pytest.skip("TimescaleDB not available")
    owner = create_engine(seed.owner_url)
    try:
        with owner.connect() as conn:
            chunks = list(
                conn.execute(
                    text(
                        "SELECT format('%I.%I', chunk_schema, chunk_name) FROM timescaledb_information.chunks"
                    )
                ).scalars()
            )
    finally:
        owner.dispose()
    assert chunks, "the seed created no chunks"
    for chunk in chunks:
        for org in (None, DEFAULT_ORG_ID):
            assert _as_app(seed, org, f"SELECT count(*) FROM {chunk}") == 0, chunk  # noqa: S608


# Routes


ROUTES = [
    ("GET", "/api/runs/{run_id}", "run", None),
    ("GET", "/api/findings/{finding_id}", "finding", None),
    ("PATCH", "/api/findings/{finding_id}", "finding", {"status": "acked"}),
    ("GET", "/api/series/{series_id}", "series", None),
    ("PATCH", "/api/series/{series_id}", "series", {"unit": "bar"}),
    ("GET", "/api/series/{series_id}/metrics", "series", None),
    ("GET", "/api/series/{series_id}/scores", "series", None),
    ("GET", "/api/series-groups/{group_id}", "group", None),
    ("PATCH", "/api/series-groups/{group_id}", "group", {"name": "renamed"}),
    ("DELETE", "/api/series-groups/{group_id}", "group", None),
    ("GET", "/api/datasets/{dataset_id}", "dataset", None),
    ("PATCH", "/api/datasets/{dataset_id}", "dataset", {"name": "renamed"}),
    ("DELETE", "/api/datasets/{dataset_id}", "dataset", None),
]
READ_BACK = {
    "run": "/api/runs/{}",
    "finding": "/api/findings/{}",
    "series": "/api/series/{}",
    "group": "/api/series-groups/{}",
    "dataset": "/api/datasets/{}",
}
# Scoped to the signed-in user, not to a tenant: `test_auth_flow.py` checks that another user's
# session gives 404 (spec 013).
USER_SCOPED_ROUTES = {("DELETE", "/api/auth/sessions/{session_id}")}
LISTS = ["/api/runs", "/api/findings", "/api/series", "/api/sources", "/api/series-groups", "/api/datasets"]


def _url(template: str, value: str) -> str:
    return template.split("{")[0] + value + template.split("}")[1]


async def _call(app: Any, method: str, url: str, body: Any = None) -> Any:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers={"X-Tabayyun-Request": "1"}
    ) as c:
        return await c.request(method, url, json=body)


@pytest.fixture
async def app_for(seed):
    """Factory of apps acting as a given user; engines disposed after the test."""
    apps = []

    def make(user: uuid.UUID, org: uuid.UUID = DEFAULT_ORG_ID, workspace: uuid.UUID = DEFAULT_WORKSPACE_ID):
        app = _as(create_app(app_settings(seed.owner_url, inline_jobs=True)), user, org, workspace)
        apps.append(app)
        return app

    yield make
    for app in apps:
        await app.state.engine.dispose()


def test_every_route_with_a_path_id_is_covered():
    """A new route with a path id must join `ROUTES` so its cross-tenant behaviour is tested."""
    app = create_app(app_settings("postgresql+psycopg://u:p@localhost/x"))
    found = {
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        if "{" in path
        for method in operations
    }
    assert found - USER_SCOPED_ROUTES == {(m, p) for m, p, _, _ in ROUTES}


@pytest.mark.parametrize(("method", "template", "kind", "body"), ROUTES)
async def test_other_org_gets_404_for_every_route(seed, app_for, method, template, kind, body):
    """Org B's owner cannot read, change or delete org A's rows by id; A's rows stay as they were."""
    b = app_for(USER_B, ORG_B, WORKSPACE_B)
    r = await _call(b, method, _url(template, seed.a[kind]), body)
    assert r.status_code == 404, (method, template, r.text)
    owner_a = app_for(BOOTSTRAP_USER_ID)
    assert (await _call(owner_a, "GET", READ_BACK[kind].format(seed.a[kind]))).status_code == 200


@pytest.mark.parametrize("path", LISTS)
async def test_lists_show_only_the_own_org(seed, app_for, path):
    """Lists as org B contain none of org A's ids and some of its own."""
    b = app_for(USER_B, ORG_B, WORKSPACE_B)
    body = (await _call(b, "GET", path)).json()
    listed = {item["id"] for item in body.get("items", [])}
    assert not listed & set(seed.a.values()), path
    assert listed, f"org B sees nothing in {path}"


async def test_dataset_run_of_another_org_is_not_found(seed, app_for):
    """Org B cannot start a run of org A's dataset."""
    b = app_for(USER_B, ORG_B, WORKSPACE_B)
    r = await _call(b, "POST", "/api/runs", {"dataset_id": seed.a["dataset"]})
    assert r.status_code == 404


async def test_viewer_reads_but_cannot_write(seed, app_for):
    """A direct viewer reads (200) and is refused writes (403)."""
    viewer = app_for(VIEWER)
    assert (await _call(viewer, "GET", f"/api/series/{seed.a['series']}")).status_code == 200
    assert (
        await _call(viewer, "PATCH", f"/api/series/{seed.a['series']}", {"unit": "bar"})
    ).status_code == 403
    assert (await _call(viewer, "DELETE", f"/api/datasets/{seed.a['dataset']}")).status_code == 403


async def test_team_editor_writes(seed, app_for):
    """An editor through a team may write."""
    editor = app_for(TEAM_EDITOR)
    r = await _call(editor, "PATCH", f"/api/findings/{seed.a['finding']}", {"status": "acked"})
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("user", [BYSTANDER, DISABLED, USER_B])
async def test_no_role_in_the_workspace_is_404(seed, app_for, user):
    """An org member without a grant, a disabled admin and another org's owner see nothing."""
    app = app_for(user)
    assert (await _call(app, "GET", "/api/runs")).status_code == 404
    assert (await _call(app, "GET", f"/api/runs/{seed.a['run']}")).status_code == 404


@pytest.mark.parametrize(
    ("user", "org", "expected"),
    [
        (BOOTSTRAP_USER_ID, DEFAULT_ORG_ID, {DEFAULT_WORKSPACE_ID}),
        (VIEWER, DEFAULT_ORG_ID, {DEFAULT_WORKSPACE_ID}),
        (TEAM_EDITOR, DEFAULT_ORG_ID, {DEFAULT_WORKSPACE_ID}),
        (BYSTANDER, DEFAULT_ORG_ID, set()),
        (DISABLED, DEFAULT_ORG_ID, set()),
        (USER_B, ORG_B, {WORKSPACE_B}),
        (USER_B, DEFAULT_ORG_ID, set()),
    ],
)
async def test_visible_workspaces(seed, user, org, expected):
    """`visible_workspaces` lists exactly the workspaces in which the principal has a role."""
    engine = make_engine(app_settings(seed.owner_url))
    try:
        async with for_org(make_session_factory(engine), org)() as session:
            stmt = visible_workspaces(Principal(user_id=user, org_id=org))
            ids = set((await session.execute(stmt)).scalars())
    finally:
        await engine.dispose()
    assert ids == expected


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO users (id, email, display_name) VALUES (gen_random_uuid(), 'x@example.test', 'x')",
        "UPDATE users SET display_name = 'renamed'",
        "DELETE FROM users WHERE email = 'b@example.test'",
    ],
)
def test_users_are_read_only_to_the_app_login(seed, sql):
    """`users` spans orgs and has no RLS, so the app login may read it but not write it."""
    with pytest.raises(DBAPIError) as excinfo:
        _as_app(seed, DEFAULT_ORG_ID, sql)
    assert _sqlstate(excinfo.value) == INSUFFICIENT_PRIVILEGE
