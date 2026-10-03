"""Org and members (spec 014): list, search, paging, role changes under the owner rules, removal."""

from __future__ import annotations

import uuid

from admin_support import ADAM, ALICE, BOB, MIA, NED
from tabayyun.db.models import BOOTSTRAP_USER_ID, DEFAULT_ORG_ID, DEFAULT_WORKSPACE_ID


async def test_org_read_and_rename(admin_env):
    """Admins read the org; only owners rename it, and an unchanged name writes no event."""
    async with admin_env.client(ADAM) as adam, admin_env.client(ALICE) as alice:
        assert (await adam.get("/api/admin/org")).json()["role"] == "admin"
        assert (await adam.patch("/api/admin/org", json={"name": "Acme"})).status_code == 403
        r = await alice.patch("/api/admin/org", json={"name": "  Acme   Energy "})
        assert (r.status_code, r.json()["name"]) == (200, "Acme Energy")
        await alice.patch("/api/admin/org", json={"name": "Acme Energy"})
        assert (await alice.patch("/api/admin/org", json={"name": "  "})).status_code == 422
    [event] = admin_env.events("org.renamed")
    assert event.details == {"before": "default", "after": "Acme Energy"}


async def test_members_list_search_and_paging(admin_env):
    """Members by email without the bootstrap user; `q` searches email and name; pages chain."""
    async with admin_env.client() as c:
        body = (await c.get("/api/admin/members")).json()
        assert [m["email"] for m in body["items"]] == [
            "adam@example.test",
            "alice@example.test",
            "mia@example.test",
            "ned@example.test",
        ]
        assert body["next"] is None
        assert [m["role"] for m in body["items"]] == ["admin", "owner", "member", "member"]
        found = (await c.get("/api/admin/members", params={"q": "MI"})).json()["items"]
        assert [m["user_id"] for m in found] == [str(MIA)]
        assert (await c.get("/api/admin/members", params={"q": "%"})).json()["items"] == []
        first = (await c.get("/api/admin/members", params={"limit": 3})).json()
        second = (await c.get("/api/admin/members", params={"limit": 3, "cursor": first["next"]})).json()
        assert [m["email"] for m in second["items"]] == ["ned@example.test"] and second["next"] is None
        assert (await c.get("/api/admin/members", params={"limit": 201})).status_code == 422


async def test_members_need_an_admin(admin_env):
    """Org admins and workspace admins read the list; a member without an admin role cannot,
    and only org admins change it."""
    admin_env.sql("UPDATE workspace_memberships SET role = 'admin' WHERE user_id = :u", u=MIA)
    async with admin_env.client(NED) as ned, admin_env.client(MIA) as mia:
        r = await ned.get("/api/admin/members")
        assert (r.status_code, r.json()["detail"]) == (403, "forbidden")
        assert len((await mia.get("/api/admin/members")).json()["items"]) == 4
        assert (await mia.get("/api/admin/teams")).status_code == 200
        assert (await mia.patch(f"/api/admin/members/{NED}", json={"role": "admin"})).status_code == 403


async def test_role_changes_follow_the_owner_rules(admin_env):
    """Admins promote members to admin but never touch owners; owners do; a no-op writes nothing."""
    async with admin_env.client(ADAM) as adam, admin_env.client(ALICE) as alice:
        assert (await adam.patch(f"/api/admin/members/{MIA}", json={"role": "admin"})).json()[
            "role"
        ] == "admin"
        assert (await adam.patch(f"/api/admin/members/{MIA}", json={"role": "admin"})).status_code == 200
        assert (await adam.patch(f"/api/admin/members/{NED}", json={"role": "owner"})).status_code == 403
        assert (await adam.patch(f"/api/admin/members/{ALICE}", json={"role": "member"})).status_code == 403
        assert (await alice.patch(f"/api/admin/members/{NED}", json={"role": "owner"})).json()[
            "role"
        ] == "owner"
        assert (await alice.patch(f"/api/admin/members/{NED}", json={"role": "nobody"})).status_code == 422
    changes = [
        (e.target_id, e.details["before"], e.details["after"])
        for e in admin_env.events("member.role_changed")
    ]
    assert changes == [(str(MIA), "member", "admin"), (str(NED), "member", "owner")]


async def test_the_last_owner_cannot_leave(admin_env):
    """Alice is the only owner (the bootstrap user does not count): she can neither step down nor
    leave until another owner exists."""
    async with admin_env.client(ALICE) as alice:
        r = await alice.patch(f"/api/admin/members/{ALICE}", json={"role": "admin"})
        assert (r.status_code, r.json()["detail"]) == (409, "last_owner")
        assert (await alice.delete(f"/api/admin/members/{ALICE}")).json()["detail"] == "last_owner"
        await alice.patch(f"/api/admin/members/{ADAM}", json={"role": "owner"})
        assert (await alice.delete(f"/api/admin/members/{ALICE}")).status_code == 204


async def test_a_disabled_owner_does_not_count(admin_env):
    """With the second owner disabled, Alice is still the last active one."""
    admin_env.sql("UPDATE org_memberships SET role = 'owner' WHERE user_id = :u", u=ADAM)
    admin_env.sql("UPDATE users SET disabled_at = now() WHERE id = :u", u=ADAM)
    async with admin_env.client(ALICE) as alice:
        assert (await alice.delete(f"/api/admin/members/{ALICE}")).status_code == 409


async def test_removal_takes_grants_and_sessions(admin_env):
    """Removing Mia deletes her membership, workspace grant and team membership, and revokes her
    sessions in the org; one event records it."""
    team = uuid.uuid4()
    admin_env.sql("INSERT INTO teams (id, org_id, name) VALUES (:t, :o, 'ops')", t=team, o=DEFAULT_ORG_ID)
    admin_env.sql(
        "INSERT INTO team_members (org_id, team_id, user_id) VALUES (:o, :t, :u)",
        o=DEFAULT_ORG_ID,
        t=team,
        u=MIA,
    )
    admin_env.sql(
        "INSERT INTO sessions (id, id_hash, user_id, org_id, sign_in_method, expires_at) "
        "VALUES (gen_random_uuid(), 'x', :u, :o, 'google', now() + interval '1 day')",
        u=MIA,
        o=DEFAULT_ORG_ID,
    )
    async with admin_env.client(ADAM) as adam:
        assert (await adam.delete(f"/api/admin/members/{MIA}")).status_code == 204
        assert (await adam.delete(f"/api/admin/members/{MIA}")).status_code == 404
    for table in ("org_memberships", "workspace_memberships", "team_members"):
        assert admin_env.scalar(f"SELECT count(*) FROM {table} WHERE user_id = :u", u=MIA) == 0, table  # noqa: S608
    assert (
        admin_env.scalar("SELECT count(*) FROM sessions WHERE user_id = :u AND revoked_at IS NULL", u=MIA)
        == 0
    )
    [event] = admin_env.events("member.removed")
    assert event.details == {"email": "mia@example.test", "role": "member"}
    assert event.actor_user_id == ADAM


async def test_unknown_foreign_and_bootstrap_users_are_404(admin_env):
    async with admin_env.client(ALICE) as alice:
        for user in (uuid.uuid4(), BOB, BOOTSTRAP_USER_ID):
            assert (
                await alice.patch(f"/api/admin/members/{user}", json={"role": "admin"})
            ).status_code == 404
            assert (await alice.delete(f"/api/admin/members/{user}")).status_code == 404
    assert admin_env.events() == []


async def test_removal_keeps_other_grants_of_the_workspace(admin_env):
    """Only the removed member's rows go."""
    admin_env.sql(
        "INSERT INTO workspace_memberships (org_id, workspace_id, user_id, role) "
        "VALUES (:o, :w, :u, 'editor')",
        o=DEFAULT_ORG_ID,
        w=DEFAULT_WORKSPACE_ID,
        u=NED,
    )
    async with admin_env.client(ALICE) as alice:
        await alice.delete(f"/api/admin/members/{MIA}")
    assert admin_env.scalar("SELECT role FROM workspace_memberships WHERE user_id = :u", u=NED) == "editor"
