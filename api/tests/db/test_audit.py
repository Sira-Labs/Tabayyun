"""The audit log (spec 014): one event per mutation with actor, IP and agent; none for a refused
change or a no-op; filters and keyset paging; who may read which events."""

from __future__ import annotations

from admin_support import ADAM, ALICE, MIA, NED
from tabayyun.db.models import DEFAULT_WORKSPACE_ID

CLIENT = {"X-Real-IP": "203.0.113.9", "User-Agent": "audit-test/1.0"}


async def test_an_event_records_actor_ip_and_agent(admin_env):
    async with admin_env.client(ADAM, headers=CLIENT) as adam:
        await adam.post("/api/admin/teams", json={"name": "ops"})
        body = (await adam.get("/api/admin/audit")).json()
    [event] = body["items"]
    assert event["actor"] == {"user_id": str(ADAM), "email": "adam@example.test", "display_name": "adam"}
    assert (event["action"], event["target_type"], event["ip_address"]) == (
        "team.created",
        "team",
        "203.0.113.9",
    )
    assert admin_env.events()[0].user_agent == "audit-test/1.0"


async def test_refused_changes_and_no_ops_write_nothing(admin_env):
    async with admin_env.client(ADAM) as adam:
        await adam.post("/api/admin/teams", json={"name": "ops"})
        assert (await adam.post("/api/admin/teams", json={"name": "ops"})).status_code == 409
        assert (await adam.patch(f"/api/admin/members/{ALICE}", json={"role": "member"})).status_code == 403
        assert (await adam.patch(f"/api/admin/members/{MIA}", json={"role": "member"})).status_code == 200
        assert (
            await adam.patch("/api/admin/workspaces/" + str(DEFAULT_WORKSPACE_ID), json={})
        ).status_code == 200
    assert [e.action for e in admin_env.events()] == ["team.created"]


async def test_filters_and_paging(admin_env):
    """Action prefix, workspace and actor filters; pages of two chain without gaps or repeats."""
    async with admin_env.client(ALICE) as alice, admin_env.client(ADAM) as adam:
        north = (await alice.post("/api/admin/workspaces", json={"name": "North"})).json()["id"]
        await alice.post("/api/admin/teams", json={"name": "ops"})
        await adam.put(f"/api/admin/workspaces/{north}/members/{NED}", json={"role": "viewer"})
        await adam.patch(f"/api/admin/members/{MIA}", json={"role": "admin"})
        await alice.patch("/api/admin/org", json={"name": "Acme"})

        async def actions(**params):
            return [e["action"] for e in (await alice.get("/api/admin/audit", params=params)).json()["items"]]

        assert await actions() == [
            "org.renamed",
            "member.role_changed",
            "workspace.member_set",
            "team.created",
            "workspace.created",
        ]
        assert await actions(action="workspace.") == ["workspace.member_set", "workspace.created"]
        assert await actions(action="%") == []
        assert await actions(workspace_id=north) == ["workspace.member_set", "workspace.created"]
        assert await actions(actor=str(ADAM)) == ["member.role_changed", "workspace.member_set"]
        seen, cursor = [], None
        while True:
            page = (
                await alice.get(
                    "/api/admin/audit", params={"limit": 2} | ({"before": cursor} if cursor else {})
                )
            ).json()
            seen += [e["id"] for e in page["items"]]
            cursor = page["next"]
            if cursor is None:
                break
        assert len(seen) == len(set(seen)) == 5
        assert (await alice.get("/api/admin/audit", params={"before": "nonsense"})).status_code == 422


async def test_workspace_admins_read_their_workspace_only(admin_env):
    async with admin_env.client(ALICE) as alice:
        north = (await alice.post("/api/admin/workspaces", json={"name": "North"})).json()["id"]
        await alice.put(f"/api/admin/workspaces/{north}/members/{NED}", json={"role": "admin"})
        await alice.post("/api/admin/teams", json={"name": "ops"})
    async with admin_env.client(NED) as ned, admin_env.client(MIA) as mia:
        assert (await ned.get("/api/admin/audit")).status_code == 403
        events = (await ned.get("/api/admin/audit", params={"workspace_id": north})).json()["items"]
        assert [e["action"] for e in events] == ["workspace.member_set", "workspace.created"]
        assert (
            await ned.get("/api/admin/audit", params={"workspace_id": str(DEFAULT_WORKSPACE_ID)})
        ).status_code == 404
        assert (
            await mia.get("/api/admin/audit", params={"workspace_id": str(DEFAULT_WORKSPACE_ID)})
        ).status_code == 403
