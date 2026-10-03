"""Invitations end to end (spec 014): create, conflicts, expiry, resend, revoke; the email by
the queue or inline against the fake SMTP server; joining at sign-in and on `/api/auth/me`."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from admin_support import ALICE, MIA, WORKSPACE_B
from db.test_auth_flow import OTHER, browser, sign_in, sql
from tabayyun.db import for_org
from tabayyun.db.models import BOOTSTRAP_USER_ID, DEFAULT_ORG_ID, DEFAULT_WORKSPACE_ID
from tabayyun.mail import MailError, SmtpConfig
from tabayyun.services.admin import invitations

ADA = "ada@example.org"


def mail_settings(inbox) -> dict[str, object]:
    return {
        "smtp_host": inbox.host,
        "smtp_port": inbox.port,
        "smtp_starttls": False,
        "smtp_from": "Tabayyun <tabayyun@example.org>",
        "public_url": "https://tabayyun.test",
    }


async def test_create_list_and_audit(admin_env):
    async with admin_env.client(ALICE) as alice:
        r = await alice.post(
            "/api/admin/invitations",
            json={
                "email": " Ada@Example.org ",
                "workspace_id": str(DEFAULT_WORKSPACE_ID),
                "workspace_role": "editor",
            },
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert (body["email"], body["org_role"], body["status"], body["email_status"]) == (
            ADA,
            "member",
            "pending",
            "not_configured",
        )
        assert body["workspace"] == {"id": str(DEFAULT_WORKSPACE_ID), "name": "default"}
        assert body["invited_by"]["email"] == "alice@example.test"
        assert [i["email"] for i in (await alice.get("/api/admin/invitations")).json()] == [ADA]
    [event] = admin_env.events("invitation.created")
    assert event.details == {
        "email": ADA,
        "org_role": "member",
        "workspace": "default",
        "workspace_role": "editor",
    }


@pytest.mark.parametrize(
    ("body", "status", "detail"),
    [
        ({"email": "mia@example.test"}, 409, "already_member"),
        ({"email": "not-an-email"}, 422, "invalid_email"),
        ({"email": ADA, "org_role": "owner"}, 422, "invalid_role"),
        ({"email": ADA, "workspace_role": "viewer"}, 422, "invalid_workspace_grant"),
        ({"email": ADA, "workspace_id": str(WORKSPACE_B), "workspace_role": "viewer"}, 404, "not found"),
    ],
)
async def test_refusals(admin_env, body, status, detail):
    async with admin_env.client(ALICE) as alice:
        r = await alice.post("/api/admin/invitations", json=body)
    assert (r.status_code, r.json()["detail"]) == (status, detail)
    assert admin_env.events() == []


async def test_one_pending_per_email_and_expiry(admin_env):
    """A second invitation for a pending email is 409; once expired, a new one revokes it."""
    async with admin_env.client(ALICE) as alice:
        first = (await alice.post("/api/admin/invitations", json={"email": ADA})).json()["id"]
        r = await alice.post("/api/admin/invitations", json={"email": ADA})
        assert (r.status_code, r.json()["detail"]) == (409, "invitation_pending")
        admin_env.sql(
            "UPDATE invitations SET expires_at = now() - interval '1 minute' WHERE id = :i", i=first
        )
        assert (await alice.get("/api/admin/invitations")).json() == []
        second = (await alice.post("/api/admin/invitations", json={"email": ADA})).json()["id"]
        statuses = {
            i["id"]: i["status"] for i in (await alice.get("/api/admin/invitations?status=all")).json()
        }
        assert statuses == {first: "revoked", second: "pending"}


async def test_resend_and_revoke(admin_env):
    async with admin_env.client(ALICE) as alice:
        created = (await alice.post("/api/admin/invitations", json={"email": ADA})).json()
        admin_env.sql(
            "UPDATE invitations SET expires_at = now() + interval '1 hour' WHERE id = :i", i=created["id"]
        )
        resent = await alice.post(f"/api/admin/invitations/{created['id']}/resend")
        assert resent.status_code == 200
        # Shortened to an hour above; resending opens it for the full TTL (14 days) again.
        assert datetime.fromisoformat(resent.json()["expires_at"]) > datetime.now(UTC) + timedelta(days=13)
        assert (await alice.delete(f"/api/admin/invitations/{created['id']}")).status_code == 204
        again = await alice.delete(f"/api/admin/invitations/{created['id']}")
        assert (again.status_code, again.json()["detail"]) == (409, "not_pending")
        assert (await alice.post(f"/api/admin/invitations/{created['id']}/resend")).status_code == 409
        assert (await alice.delete(f"/api/admin/invitations/{uuid.uuid4()}")).status_code == 404
    assert [e.action for e in admin_env.events()] == [
        "invitation.created",
        "invitation.resent",
        "invitation.revoked",
    ]


async def test_members_cannot_invite(admin_env):
    async with admin_env.client(MIA) as mia:
        assert (await mia.post("/api/admin/invitations", json={"email": ADA})).status_code == 403
        assert (await mia.get("/api/admin/invitations")).status_code == 403


async def test_inline_email_is_sent(admin_env, smtp):
    """With SMTP set and inline jobs, the email goes out after the response and is marked sent."""
    async with admin_env.client(ALICE, **mail_settings(smtp)) as alice:
        r = await alice.post("/api/admin/invitations", json={"email": ADA})
        assert r.json()["email_status"] == "queued"
        listed = (await alice.get("/api/admin/invitations")).json()
    [message] = smtp.messages
    assert message["To"] == ADA and "alice invited you to default" in message["Subject"]
    assert "https://tabayyun.test/login" in message.get_payload(decode=True).decode()
    assert listed[0]["email_status"] == "sent"


async def test_queued_email_and_worker_delivery(admin_env, smtp):
    """Without inline jobs the job is deferred in the same transaction; the worker's delivery
    retries before the last attempt and records the failure on it."""
    async with admin_env.client(ALICE, inline_jobs=False, **mail_settings(smtp)) as alice:
        created = (await alice.post("/api/admin/invitations", json={"email": ADA})).json()
    [(task, args)] = admin_env.sql("SELECT task_name, args FROM procrastinate_jobs WHERE queue_name = 'mail'")
    assert task == "tabayyun.send_invitation_email"
    assert args == {"invitation_id": created["id"], "org_id": str(DEFAULT_ORG_ID)}
    factory = for_org(admin_env.apps[-1].state.session_factory, DEFAULT_ORG_ID)
    config = SmtpConfig(
        host=smtp.host, port=smtp.port, starttls=False, username=None, password=None, sender="t@example.org"
    )
    smtp.refuse.add(ADA)
    with pytest.raises(MailError):
        await invitations.deliver(
            factory, config, uuid.UUID(created["id"]), public_url=None, final_attempt=False
        )
    assert admin_env.scalar("SELECT email_status FROM invitations") == "queued"
    await invitations.deliver(factory, config, uuid.UUID(created["id"]), public_url=None, final_attempt=True)
    status, error = admin_env.sql("SELECT email_status, email_error FROM invitations")[0]
    assert status == "failed" and error.startswith("550 ")
    smtp.refuse.clear()
    admin_env.sql("UPDATE invitations SET revoked_at = now()")
    await invitations.deliver(factory, config, uuid.UUID(created["id"]), public_url=None, final_attempt=True)
    assert smtp.messages == []  # revoked: skipped


# Joining (oidc mode, the fake IdP of spec 013)


def _invite(env, email: str, *, workspace_role: str | None = "viewer") -> None:
    sql(
        env,
        "INSERT INTO invitations (id, org_id, email, org_role, workspace_id, workspace_role, invited_by, "
        "expires_at) VALUES (gen_random_uuid(), :o, :e, 'member', :w, :r, :boot, now() + interval '1 day')",
        boot=BOOTSTRAP_USER_ID,
        o=DEFAULT_ORG_ID,
        e=email,
        w=DEFAULT_WORKSPACE_ID if workspace_role else None,
        r=workspace_role,
    )


async def test_invited_email_joins_at_sign_in(env):
    """The invitee signs in with Google and lands in the org with the invited workspace role."""
    _invite(env, OTHER)
    async with browser(env.app) as c:
        await sign_in(env, c, "google", email=OTHER)
        me = await c.get("/api/auth/me")
        assert (me.status_code, me.json()["role"]) == (200, "member")
        assert [(w["name"], w["role"]) for w in (await c.get("/api/workspaces")).json()] == [
            ("default", "viewer")
        ]
        assert (await c.get("/api/sources")).status_code == 200


async def test_signed_in_without_access_joins_on_me(env):
    """Signed in before the invitation: `/me` says no access, then joins once it exists."""
    async with browser(env.app) as c:
        await sign_in(env, c, "github", email=OTHER)
        assert (await c.get("/api/auth/me")).status_code == 403
        assert (await c.get("/api/sources")).json() == {"detail": "no_access"}
        _invite(env, OTHER)
        me = await c.get("/api/auth/me")
        assert (me.status_code, me.json()["org"]["id"]) == (200, str(DEFAULT_ORG_ID))
        assert (await c.get("/api/sources")).status_code == 200
    [(action, actor)] = sql(env, "SELECT action, actor_user_id FROM audit_events")
    assert (action, actor) == ("member.joined", None)
