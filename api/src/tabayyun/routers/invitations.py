"""Invitation routes (spec 014), under `/api/admin` with the same gate as `routers/admin.py`.

With `TABAYYUN_INLINE_JOBS` the email is sent after the response instead of by the worker.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, Response
from pydantic import BaseModel, Field

from tabayyun.auth import require_recent_passkey
from tabayyun.authz import WorkspaceRole, get_principal
from tabayyun.db import for_org
from tabayyun.limits import limit
from tabayyun.mail import SmtpConfig
from tabayyun.routers.admin import ADMIN_WRITE_LIMIT, OrgAdmin, Person, org_admin
from tabayyun.services.admin import invitations
from tabayyun.settings import Settings

router = APIRouter(
    prefix="/api/admin/invitations",
    tags=["admin"],
    dependencies=[Depends(get_principal), ADMIN_WRITE_LIMIT, Depends(require_recent_passkey)],
)
# Invitation emails per org (spec 015): a guard against using the panel to send mail in bulk.
# The role is checked first, so a member who is refused cannot use up the org's hour.
INVITE_LIMIT = [Depends(org_admin), Depends(limit("admin.invite", "org"))]


class InvitationIn(BaseModel):
    email: str = Field(max_length=500)
    org_role: str = Field(default="member", max_length=20)
    workspace_id: uuid.UUID | None = None
    workspace_role: WorkspaceRole | None = None


class WorkspaceRef(BaseModel):
    id: str
    name: str


class InvitationOut(BaseModel):
    id: str
    email: str
    org_role: str
    workspace: WorkspaceRef | None
    workspace_role: str | None
    invited_by: Person | None
    created_at: datetime
    expires_at: datetime
    status: invitations.Status
    email_status: str
    email_error: str | None

    @classmethod
    def of(cls, view: invitations.InvitationView) -> InvitationOut:
        i = view.invitation
        return cls(
            id=str(i.id),
            email=i.email,
            org_role=i.org_role,
            workspace=None
            if view.workspace is None
            else WorkspaceRef(id=str(view.workspace.id), name=view.workspace.name),
            workspace_role=i.workspace_role,
            invited_by=None if view.inviter is None else Person.of(view.inviter),
            created_at=i.created_at,
            expires_at=i.expires_at,
            status=view.status,
            email_status=i.email_status,
            email_error=i.email_error,
        )


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


async def _send_after(
    request: Request, background: BackgroundTasks, caller: OrgAdmin, view: invitations.InvitationView
) -> None:
    """Inline jobs: commit now, send after the response (the worker's last attempt, no retry)."""
    settings = _settings(request)
    config = SmtpConfig.from_settings(settings)
    if config is None or not settings.inline_jobs:
        return
    await caller.session.commit()
    background.add_task(
        invitations.deliver,
        for_org(request.app.state.session_factory, caller.principal.org_id),
        config,
        view.invitation.id,
        public_url=settings.public_url,
        final_attempt=True,
    )


@router.get("", response_model=list[InvitationOut])
async def list_invitations(
    caller: OrgAdmin, status: Annotated[Literal["pending", "all"], Query()] = "pending"
) -> list[InvitationOut]:
    """Invitations newest first: open ones, or all with `status=all`."""
    found = await invitations.list_invitations(
        caller.session, caller.principal.org_id, pending_only=status == "pending"
    )
    return [InvitationOut.of(v) for v in found]


@router.post("", response_model=InvitationOut, status_code=201, dependencies=INVITE_LIMIT)
async def create_invitation(
    body: InvitationIn, request: Request, background: BackgroundTasks, caller: OrgAdmin
) -> InvitationOut:
    """Invite an email with an org role and optionally a workspace role; the email is queued."""
    settings = _settings(request)
    view = await invitations.create_invitation(
        caller.session,
        caller.actor,
        body.email,
        invitations.Grant(
            org_role=body.org_role,
            workspace_id=body.workspace_id,
            workspace_role=None if body.workspace_role is None else body.workspace_role.value,
        ),
        ttl=settings.invitation_ttl,
        mail_on=settings.mail_enabled,
        defer=not settings.inline_jobs,
    )
    await _send_after(request, background, caller, view)
    return InvitationOut.of(view)


@router.post("/{invitation_id}/resend", response_model=InvitationOut, dependencies=INVITE_LIMIT)
async def resend_invitation(
    invitation_id: uuid.UUID, request: Request, background: BackgroundTasks, caller: OrgAdmin
) -> InvitationOut:
    """A new expiry, and the email queued again."""
    settings = _settings(request)
    view = await invitations.resend_invitation(
        caller.session,
        caller.actor,
        invitation_id,
        ttl=settings.invitation_ttl,
        mail_on=settings.mail_enabled,
        defer=not settings.inline_jobs,
    )
    await _send_after(request, background, caller, view)
    return InvitationOut.of(view)


@router.delete("/{invitation_id}", status_code=204)
async def revoke_invitation(invitation_id: uuid.UUID, caller: OrgAdmin) -> Response:
    """Revoke a pending invitation (409 `not_pending` otherwise)."""
    await invitations.revoke_invitation(caller.session, caller.actor, invitation_id)
    return Response(status_code=204)
