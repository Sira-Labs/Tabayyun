"""Admin API (spec 014): the org, members, teams, workspaces with their access lists, and the
audit log. Invitations live in `routers/invitations.py` under the same prefix and gate.

Every route needs, in this order: a session (401), a passkey sign-in younger than
`TABAYYUN_PASSKEY_FRESH` (403 `second-factor-required`), and the role the route names (403
`forbidden`, or 404 for a workspace the caller cannot see). Service errors carry their own
status and code (`AdminError`, mapped in `create_app`).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from tabayyun.auth import client_ip, require_recent_passkey
from tabayyun.authz import (
    Action,
    ForbiddenError,
    NotVisibleError,
    OrgRole,
    Principal,
    WorkspaceRole,
    authorize,
    get_principal,
    get_session,
    workspace_roles,
)
from tabayyun.authz.roles import ORG_ADMINS
from tabayyun.db.models import User
from tabayyun.services.admin import audit, members, org, teams, workspaces
from tabayyun.services.admin import errors as admin_errors
from tabayyun.services.pagination import decode_keyset, encode_keyset

MAX_PAGE = 200


@dataclass(frozen=True)
class Admin:
    """The caller of an admin route: their session, principal, org role and audit actor."""

    session: AsyncSession
    principal: Principal
    role: OrgRole | None
    actor: audit.Actor

    @property
    def is_org_admin(self) -> bool:
        return self.role in ORG_ADMINS


async def admin(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    principal: Annotated[Principal, Depends(get_principal)],
) -> Admin:
    """The caller with their org role; routes then require what they need."""
    actor = audit.Actor(
        user_id=principal.user_id,
        org_id=principal.org_id,
        ip_address=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    return Admin(session, principal, await members.org_role(session, principal), actor)


async def org_admin(caller: Annotated[Admin, Depends(admin)]) -> Admin:
    """Org owner or admin, else 403 `forbidden`."""
    if not caller.is_org_admin:
        raise admin_errors.ForbiddenError
    return caller


async def org_owner(caller: Annotated[Admin, Depends(admin)]) -> Admin:
    """Org owner, else 403 `forbidden`."""
    if caller.role is not OrgRole.OWNER:
        raise admin_errors.ForbiddenError
    return caller


async def workspace_admin(workspace_id: uuid.UUID, caller: Annotated[Admin, Depends(admin)]) -> Admin:
    """Effective `admin` in the path's workspace: 404 without any role in it, 403 below admin."""
    try:
        await authorize(caller.session, caller.principal, Action.MANAGE, workspace_id)
    except NotVisibleError as exc:
        raise admin_errors.NotFoundError from exc
    except ForbiddenError as exc:
        raise admin_errors.ForbiddenError from exc
    return caller


OrgAdmin = Annotated[Admin, Depends(org_admin)]
OrgOwner = Annotated[Admin, Depends(org_owner)]
WorkspaceAdmin = Annotated[Admin, Depends(workspace_admin)]
AnyAdmin = Annotated[Admin, Depends(admin)]

# The passkey gate runs right after the session check, before any role check (spec 014).
router = APIRouter(
    prefix="/api/admin",
    tags=["admin"],
    dependencies=[Depends(get_principal), Depends(require_recent_passkey)],
)


# Shapes


class Person(BaseModel):
    user_id: str
    email: str
    display_name: str

    @classmethod
    def of(cls, user: User) -> Person:
        return cls(user_id=str(user.id), email=user.email, display_name=user.display_name)


class OrgOut(BaseModel):
    id: str
    name: str
    created_at: datetime
    role: OrgRole


class OrgIn(BaseModel):
    name: str = Field(max_length=500)


class MemberOut(BaseModel):
    user_id: str
    email: str
    display_name: str
    role: OrgRole
    joined_at: datetime
    disabled: bool

    @classmethod
    def of(cls, m: members.Member) -> MemberOut:
        return cls(
            user_id=str(m.user_id),
            email=m.email,
            display_name=m.display_name,
            role=m.role,
            joined_at=m.joined_at,
            disabled=m.disabled,
        )


class MemberPage(BaseModel):
    items: list[MemberOut]
    next: str | None


class RoleIn(BaseModel):
    role: OrgRole


class WorkspaceRoleIn(BaseModel):
    role: WorkspaceRole


class TeamWorkspace(BaseModel):
    workspace_id: str
    name: str
    role: WorkspaceRole


class TeamOut(BaseModel):
    id: str
    name: str
    members: list[Person]
    workspaces: list[TeamWorkspace]


class NameIn(BaseModel):
    name: str = Field(max_length=500)


class AdminWorkspaceOut(BaseModel):
    id: str
    name: str
    timezone: str
    created_at: datetime
    role: WorkspaceRole


class WorkspaceIn(BaseModel):
    name: str = Field(max_length=500)
    timezone: str = Field(default="UTC", max_length=100)


class WorkspacePatch(BaseModel):
    name: str | None = Field(default=None, max_length=500)
    timezone: str | None = Field(default=None, max_length=100)


class AccessMember(Person):
    role: WorkspaceRole


class AccessTeam(BaseModel):
    team_id: str
    name: str
    role: WorkspaceRole


class OrgAdminOut(Person):
    role: OrgRole


class AccessOut(BaseModel):
    members: list[AccessMember]
    teams: list[AccessTeam]
    org_admins: list[OrgAdminOut]


class AuditEventOut(BaseModel):
    id: str
    created_at: datetime
    actor: Person | None
    action: str
    target_type: str
    target_id: str
    workspace_id: str | None
    details: dict[str, Any]
    ip_address: str | None


class AuditPage(BaseModel):
    items: list[AuditEventOut]
    next: str | None


def _team_out(team: teams.TeamView) -> TeamOut:
    return TeamOut(
        id=str(team.id),
        name=team.name,
        members=[Person.of(u) for u in team.members],
        workspaces=[
            TeamWorkspace(workspace_id=str(w.id), name=w.name, role=WorkspaceRole(role))
            for w, role in team.workspaces
        ],
    )


# Org


@router.get("/org", response_model=OrgOut)
async def get_org(caller: OrgAdmin) -> OrgOut:
    """The caller's org and their role in it."""
    found = await org.get_org(caller.session, caller.principal.org_id)
    assert caller.role is not None  # org_admin guarantees it
    return OrgOut(id=str(found.id), name=found.name, created_at=found.created_at, role=caller.role)


@router.patch("/org", response_model=OrgOut)
async def rename_org(body: OrgIn, caller: OrgOwner) -> OrgOut:
    """Rename the org (owners only)."""
    found = await org.rename_org(caller.session, caller.actor, body.name)
    return OrgOut(id=str(found.id), name=found.name, created_at=found.created_at, role=OrgRole.OWNER)


# Members


@router.get("/members", response_model=MemberPage)
async def list_members(
    caller: OrgAdmin,
    q: Annotated[str | None, Query(max_length=200)] = None,
    cursor: Annotated[str | None, Query(max_length=1000)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 50,
) -> MemberPage:
    """Members by email, searchable by email or name."""
    after = None if cursor is None else members.decode_cursor(cursor)
    page, nxt = await members.list_members(
        caller.session, caller.principal.org_id, q=q, after=after, limit=limit
    )
    return MemberPage(items=[MemberOut.of(m) for m in page], next=nxt)


@router.patch("/members/{user_id}", response_model=MemberOut)
async def change_member_role(user_id: uuid.UUID, body: RoleIn, caller: OrgAdmin) -> MemberOut:
    """Change a member's org role under the owner rules (403 `forbidden`, 409 `last_owner`)."""
    assert caller.role is not None
    return MemberOut.of(
        await members.change_role(caller.session, caller.actor, caller.role, user_id, body.role)
    )


@router.delete("/members/{user_id}", status_code=204)
async def remove_member(user_id: uuid.UUID, caller: OrgAdmin) -> Response:
    """Remove a member, their grants in the org, and their sessions in it."""
    assert caller.role is not None
    await members.remove_member(caller.session, caller.actor, caller.role, user_id)
    return Response(status_code=204)


# Teams


@router.get("/teams", response_model=list[TeamOut])
async def list_teams(caller: OrgAdmin) -> list[TeamOut]:
    """Every team with its members and workspace roles."""
    return [_team_out(t) for t in await teams.list_teams(caller.session, caller.principal.org_id)]


async def _team(caller: Admin, team_id: uuid.UUID) -> TeamOut:
    found = next(
        (t for t in await teams.list_teams(caller.session, caller.principal.org_id) if t.id == team_id), None
    )
    if found is None:
        raise admin_errors.NotFoundError
    return _team_out(found)


@router.post("/teams", response_model=TeamOut, status_code=201)
async def create_team(body: NameIn, caller: OrgAdmin) -> TeamOut:
    team = await teams.create_team(caller.session, caller.actor, body.name)
    return await _team(caller, team.id)


@router.patch("/teams/{team_id}", response_model=TeamOut)
async def rename_team(team_id: uuid.UUID, body: NameIn, caller: OrgAdmin) -> TeamOut:
    await teams.rename_team(caller.session, caller.actor, team_id, body.name)
    return await _team(caller, team_id)


@router.delete("/teams/{team_id}", status_code=204)
async def delete_team(team_id: uuid.UUID, caller: OrgAdmin) -> Response:
    await teams.delete_team(caller.session, caller.actor, team_id)
    return Response(status_code=204)


@router.put("/teams/{team_id}/members/{user_id}", status_code=204)
async def add_team_member(team_id: uuid.UUID, user_id: uuid.UUID, caller: OrgAdmin) -> Response:
    await teams.add_member(caller.session, caller.actor, team_id, user_id)
    return Response(status_code=204)


@router.delete("/teams/{team_id}/members/{user_id}", status_code=204)
async def remove_team_member(team_id: uuid.UUID, user_id: uuid.UUID, caller: OrgAdmin) -> Response:
    await teams.remove_member(caller.session, caller.actor, team_id, user_id)
    return Response(status_code=204)


# Workspaces


@router.get("/workspaces", response_model=list[AdminWorkspaceOut])
async def list_admin_workspaces(caller: AnyAdmin) -> list[AdminWorkspaceOut]:
    """The workspaces the caller administers; 403 when there are none."""
    administered = [
        AdminWorkspaceOut(id=str(w.id), name=w.name, timezone=w.timezone, created_at=w.created_at, role=role)
        for w, role in await workspace_roles(caller.session, caller.principal)
        if role is WorkspaceRole.ADMIN
    ]
    if not administered:
        raise admin_errors.ForbiddenError
    return administered


def _workspace_out(w: Any) -> AdminWorkspaceOut:
    return AdminWorkspaceOut(
        id=str(w.id), name=w.name, timezone=w.timezone, created_at=w.created_at, role=WorkspaceRole.ADMIN
    )


@router.post("/workspaces", response_model=AdminWorkspaceOut, status_code=201)
async def create_workspace(body: WorkspaceIn, caller: OrgAdmin) -> AdminWorkspaceOut:
    return _workspace_out(
        await workspaces.create_workspace(caller.session, caller.actor, body.name, body.timezone)
    )


@router.patch("/workspaces/{workspace_id}", response_model=AdminWorkspaceOut)
async def update_workspace(
    workspace_id: uuid.UUID, body: WorkspacePatch, caller: WorkspaceAdmin
) -> AdminWorkspaceOut:
    updated = await workspaces.update_workspace(
        caller.session, caller.actor, workspace_id, name=body.name, timezone=body.timezone
    )
    return _workspace_out(updated)


@router.delete("/workspaces/{workspace_id}", status_code=204)
async def delete_workspace(workspace_id: uuid.UUID, caller: OrgOwner) -> Response:
    """Delete an empty workspace (owners; 409 `default_workspace`, `workspace_not_empty`)."""
    await workspaces.delete_workspace(caller.session, caller.actor, workspace_id)
    return Response(status_code=204)


@router.get("/workspaces/{workspace_id}/access", response_model=AccessOut)
async def get_access(workspace_id: uuid.UUID, caller: WorkspaceAdmin) -> AccessOut:
    found = await workspaces.get_access(caller.session, caller.principal.org_id, workspace_id)
    return AccessOut(
        members=[
            AccessMember(**Person.of(u).model_dump(), role=WorkspaceRole(role)) for u, role in found.members
        ],
        teams=[
            AccessTeam(team_id=str(t.id), name=t.name, role=WorkspaceRole(role)) for t, role in found.teams
        ],
        org_admins=[
            OrgAdminOut(**Person.of(u).model_dump(), role=OrgRole(role)) for u, role in found.org_admins
        ],
    )


@router.put("/workspaces/{workspace_id}/members/{user_id}", status_code=204)
async def set_workspace_member(
    workspace_id: uuid.UUID, user_id: uuid.UUID, body: WorkspaceRoleIn, caller: WorkspaceAdmin
) -> Response:
    await workspaces.set_member(caller.session, caller.actor, workspace_id, user_id, body.role)
    return Response(status_code=204)


@router.delete("/workspaces/{workspace_id}/members/{user_id}", status_code=204)
async def remove_workspace_member(
    workspace_id: uuid.UUID, user_id: uuid.UUID, caller: WorkspaceAdmin
) -> Response:
    await workspaces.remove_member(caller.session, caller.actor, workspace_id, user_id)
    return Response(status_code=204)


@router.put("/workspaces/{workspace_id}/teams/{team_id}", status_code=204)
async def set_workspace_team(
    workspace_id: uuid.UUID, team_id: uuid.UUID, body: WorkspaceRoleIn, caller: WorkspaceAdmin
) -> Response:
    await workspaces.set_team(caller.session, caller.actor, workspace_id, team_id, body.role)
    return Response(status_code=204)


@router.delete("/workspaces/{workspace_id}/teams/{team_id}", status_code=204)
async def remove_workspace_team(
    workspace_id: uuid.UUID, team_id: uuid.UUID, caller: WorkspaceAdmin
) -> Response:
    await workspaces.remove_team(caller.session, caller.actor, workspace_id, team_id)
    return Response(status_code=204)


# Audit log


@router.get("/audit", response_model=AuditPage)
async def list_audit(
    caller: AnyAdmin,
    workspace_id: uuid.UUID | None = None,
    action: Annotated[str | None, Query(max_length=100)] = None,
    actor: uuid.UUID | None = None,
    before: Annotated[str | None, Query(max_length=1000)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 50,
) -> AuditPage:
    """Events newest first. Org admins see the whole org; a workspace admin names their workspace."""
    if workspace_id is None:
        if not caller.is_org_admin:
            raise admin_errors.ForbiddenError
        scope = None
    else:
        await workspace_admin(workspace_id, caller)
        scope = [workspace_id]
    try:
        cursor = None if before is None else decode_keyset(before)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid_cursor") from exc
    rows, more = await audit.list_events(
        caller.session,
        audit.AuditQuery(
            workspace_ids=scope, action_prefix=action, actor_user_id=actor, before=cursor, limit=limit
        ),
    )
    items = [
        AuditEventOut(
            id=str(e.id),
            created_at=e.created_at,
            actor=None if u is None else Person.of(u),
            action=e.action,
            target_type=e.target_type,
            target_id=e.target_id,
            workspace_id=None if e.workspace_id is None else str(e.workspace_id),
            details=e.details,
            ip_address=None if e.ip_address is None else str(e.ip_address),
        )
        for e, u in rows
    ]
    last = rows[-1][0] if rows else None
    return AuditPage(items=items, next=encode_keyset(last.created_at, last.id) if more and last else None)
