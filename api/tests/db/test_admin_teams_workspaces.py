"""Teams, workspaces and access lists (spec 014), and the user story end to end."""

from __future__ import annotations

import uuid

from admin_support import ADAM, ALICE, BOB, MIA, NED
from tabayyun.db.models import DEFAULT_ORG_ID, DEFAULT_WORKSPACE_ID

CSV = b"ts,value\n2026-01-01T00:00:00Z,1\n2026-01-01T01:00:00Z,2\n2026-01-01T02:00:00Z,3\n"


# Teams


async def test_team_lifecycle(admin_env):
    """Create, rename, add and remove members, delete; names are unique and trimmed."""
    async with admin_env.client(ADAM) as adam:
        r = await adam.post("/api/admin/teams", json={"name": " Night  shift "})
        assert (r.status_code, r.json()["name"], r.json()["members"]) == (201, "Night shift", [])
        team = r.json()["id"]
        assert (await adam.post("/api/admin/teams", json={"name": "Night shift"})).json()[
            "detail"
        ] == "name_taken"
        assert (await adam.post("/api/admin/teams", json={"name": "x" * 101})).json()[
            "detail"
        ] == "invalid_name"
        assert (await adam.put(f"/api/admin/teams/{team}/members/{MIA}")).status_code == 204
        assert (await adam.put(f"/api/admin/teams/{team}/members/{MIA}")).status_code == 204  # no-op
        assert (await adam.put(f"/api/admin/teams/{team}/members/{BOB}")).status_code == 404  # other org
        listed = (await adam.get("/api/admin/teams")).json()
        assert [(t["name"], [m["email"] for m in t["members"]]) for t in listed] == [
            ("Night shift", ["mia@example.test"])
        ]
        assert (await adam.patch(f"/api/admin/teams/{team}", json={"name": "Nights"})).json()[
            "name"
        ] == "Nights"
        assert (await adam.delete(f"/api/admin/teams/{team}/members/{MIA}")).status_code == 204
        assert (await adam.delete(f"/api/admin/teams/{team}/members/{MIA}")).status_code == 404
        assert (await adam.delete(f"/api/admin/teams/{team}")).status_code == 204
        assert (await adam.get("/api/admin/teams")).json() == []
    assert [e.action for e in admin_env.events()] == [
        "team.created",
        "team.member_added",
        "team.renamed",
        "team.member_removed",
        "team.deleted",
    ]


async def test_teams_need_an_org_admin(admin_env):
    async with admin_env.client(MIA) as mia:
        assert (await mia.post("/api/admin/teams", json={"name": "x"})).status_code == 403


# Workspaces


async def test_workspace_create_update_and_validation(admin_env):
    async with admin_env.client(ADAM) as adam:
        r = await adam.post(
            "/api/admin/workspaces", json={"name": "Plant North", "timezone": "Europe/Berlin"}
        )
        assert r.status_code == 201, r.text
        ws = r.json()
        assert (ws["name"], ws["timezone"], ws["role"]) == ("Plant North", "Europe/Berlin", "admin")
        dup = await adam.post("/api/admin/workspaces", json={"name": "Plant North"})
        assert (dup.status_code, dup.json()["detail"]) == (409, "name_taken")
        for tz in ("Mars/Olympus", "../etc/passwd", ""):
            bad = await adam.post("/api/admin/workspaces", json={"name": "x", "timezone": tz})
            assert (bad.status_code, bad.json()["detail"]) == (422, "invalid_timezone"), tz
        r = await adam.patch(f"/api/admin/workspaces/{ws['id']}", json={"timezone": "Asia/Riyadh"})
        assert r.json()["timezone"] == "Asia/Riyadh"
        await adam.patch(f"/api/admin/workspaces/{ws['id']}", json={"timezone": "Asia/Riyadh"})  # no-op
        assert (
            await adam.patch(f"/api/admin/workspaces/{ws['id']}", json={"name": "default"})
        ).status_code == 409
    [created, updated] = admin_env.events()
    assert (created.action, created.workspace_id) == ("workspace.created", uuid.UUID(ws["id"]))
    assert updated.details == {"timezone": {"before": "Europe/Berlin", "after": "Asia/Riyadh"}}


async def test_workspace_roles_decide_who_manages(admin_env):
    """A workspace admin (not an org admin) manages their workspace only; a viewer gets 403, a
    member without a role 404; only org admins create workspaces."""
    async with admin_env.client(ALICE) as alice:
        north = (await alice.post("/api/admin/workspaces", json={"name": "North"})).json()["id"]
        await alice.put(f"/api/admin/workspaces/{north}/members/{NED}", json={"role": "admin"})
    async with admin_env.client(NED) as ned, admin_env.client(MIA) as mia:
        assert [w["name"] for w in (await ned.get("/api/admin/workspaces")).json()] == ["North"]
        assert (await ned.patch(f"/api/admin/workspaces/{north}", json={"name": "N"})).status_code == 200
        assert (await ned.get(f"/api/admin/workspaces/{DEFAULT_WORKSPACE_ID}/access")).status_code == 404
        assert (await ned.post("/api/admin/workspaces", json={"name": "Mine"})).status_code == 403
        assert (await mia.get(f"/api/admin/workspaces/{DEFAULT_WORKSPACE_ID}/access")).status_code == 403
        assert (await mia.get(f"/api/admin/workspaces/{north}/access")).status_code == 404
        assert (await mia.get("/api/admin/workspaces")).status_code == 403


async def test_workspace_delete_guards(admin_env):
    """Only owners delete; never the default workspace; never one with data; an empty one goes
    with its grants and open invitations."""
    async with admin_env.client(ALICE) as alice, admin_env.client(ADAM) as adam:
        empty = (await alice.post("/api/admin/workspaces", json={"name": "Empty"})).json()["id"]
        full = (await alice.post("/api/admin/workspaces", json={"name": "Full"})).json()["id"]
        await alice.put(f"/api/admin/workspaces/{empty}/members/{MIA}", json={"role": "editor"})
        admin_env.sql(
            "INSERT INTO invitations (id, org_id, email, org_role, workspace_id, workspace_role, invited_by, "
            "expires_at) VALUES (gen_random_uuid(), :o, 'x@example.test', 'member', :w, 'viewer', :u, "
            "now() + interval '1 day')",
            o=DEFAULT_ORG_ID,
            w=uuid.UUID(empty),
            u=ALICE,
        )
        upload = await alice.post(
            "/api/runs",
            files={"file": ("f.csv", CSV, "text/csv")},
            data={"series_id": "s"},
            headers={"X-Tabayyun-Workspace": full},
        )
        assert upload.status_code == 202, upload.text
        assert (await adam.delete(f"/api/admin/workspaces/{empty}")).status_code == 403
        default = await alice.delete(f"/api/admin/workspaces/{DEFAULT_WORKSPACE_ID}")
        assert (default.status_code, default.json()["detail"]) == (409, "default_workspace")
        assert (await alice.delete(f"/api/admin/workspaces/{full}")).json()["detail"] == "workspace_not_empty"
        assert (await alice.delete(f"/api/admin/workspaces/{empty}")).status_code == 204
    assert (
        admin_env.scalar("SELECT count(*) FROM workspace_memberships WHERE workspace_id = :w", w=empty) == 0
    )
    assert admin_env.scalar("SELECT count(*) FROM invitations") == 0
    [deleted] = admin_env.events("workspace.deleted")
    assert deleted.details == {"name": "Empty"}


async def test_access_list_and_grants(admin_env):
    """Direct members, team roles and the org admins (read-only) of a workspace."""
    team = uuid.uuid4()
    admin_env.sql("INSERT INTO teams (id, org_id, name) VALUES (:t, :o, 'ops')", t=team, o=DEFAULT_ORG_ID)
    ws = DEFAULT_WORKSPACE_ID
    async with admin_env.client(ADAM) as adam:
        assert (
            await adam.put(f"/api/admin/workspaces/{ws}/members/{NED}", json={"role": "editor"})
        ).status_code == 204
        assert (
            await adam.put(f"/api/admin/workspaces/{ws}/members/{MIA}", json={"role": "editor"})
        ).status_code == 204
        assert (
            await adam.put(f"/api/admin/workspaces/{ws}/teams/{team}", json={"role": "viewer"})
        ).status_code == 204
        assert (
            await adam.put(f"/api/admin/workspaces/{ws}/members/{BOB}", json={"role": "viewer"})
        ).status_code == 404
        assert (
            await adam.put(f"/api/admin/workspaces/{ws}/teams/{uuid.uuid4()}", json={"role": "viewer"})
        ).status_code == 404
        access = (await adam.get(f"/api/admin/workspaces/{ws}/access")).json()
        assert [(m["email"], m["role"]) for m in access["members"]] == [
            ("mia@example.test", "editor"),
            ("ned@example.test", "editor"),
        ]
        assert [(t["name"], t["role"]) for t in access["teams"]] == [("ops", "viewer")]
        assert [(a["email"], a["role"]) for a in access["org_admins"]] == [
            ("adam@example.test", "admin"),
            ("alice@example.test", "owner"),
        ]
        assert (await adam.delete(f"/api/admin/workspaces/{ws}/members/{NED}")).status_code == 204
        assert (await adam.delete(f"/api/admin/workspaces/{ws}/members/{NED}")).status_code == 404
        assert (await adam.delete(f"/api/admin/workspaces/{ws}/teams/{team}")).status_code == 204
    set_events = admin_env.events("workspace.member_set")
    assert [(e.details["email"], e.details["before"], e.details["after"]) for e in set_events] == [
        ("ned@example.test", None, "editor"),
        ("mia@example.test", "viewer", "editor"),
    ]
    assert len(admin_env.events("workspace.team_set")) == 1


async def test_user_story_plant_north_and_south(admin_env):
    """An editor of Plant North through a team sees North with its data and gets 404 for South."""
    async with admin_env.client(ALICE) as alice:
        north = (await alice.post("/api/admin/workspaces", json={"name": "Plant North"})).json()["id"]
        south = (await alice.post("/api/admin/workspaces", json={"name": "Plant South"})).json()["id"]
        team = (await alice.post("/api/admin/teams", json={"name": "North crew"})).json()["id"]
        await alice.put(f"/api/admin/teams/{team}/members/{NED}")
        await alice.put(f"/api/admin/workspaces/{north}/teams/{team}", json={"role": "editor"})
    async with admin_env.client(NED) as ned:
        assert [(w["name"], w["role"]) for w in (await ned.get("/api/workspaces")).json()] == [
            ("Plant North", "editor")
        ]
        upload = await ned.post(
            "/api/runs",
            files={"file": ("f.csv", CSV, "text/csv")},
            data={"series_id": "flow"},
            headers={"X-Tabayyun-Workspace": north},
        )
        assert upload.status_code == 202
        assert (await ned.get("/api/series", headers={"X-Tabayyun-Workspace": south})).status_code == 404
