"""Workspaces and who works in them (spec 014): create, update, delete, and the access list of
direct members and team roles. Org owners and admins hold `admin` in every workspace (spec
007), so the access list shows them read-only."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import delete, exists, select, union_all
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tabayyun.authz import WorkspaceRole
from tabayyun.authz.roles import ORG_ADMINS
from tabayyun.db.models import (
    BOOTSTRAP_USER_ID,
    DEFAULT_WORKSPACE_ID,
    Dataset,
    OrgMembership,
    Run,
    Series,
    SeriesGroup,
    Source,
    Team,
    User,
    Workspace,
    WorkspaceMembership,
    WorkspaceTeamRole,
)
from tabayyun.services.admin import audit
from tabayyun.services.admin.errors import ConflictError, NotFoundError
from tabayyun.services.admin.names import clean_name, clean_timezone
from tabayyun.services.admin.teams import get_team, org_user

# Tables whose rows belong to a workspace; any row blocks deleting it (deleting data is later).
CONTENT = (Source, Series, Dataset, SeriesGroup, Run)


@dataclass(frozen=True)
class Access:
    """Who holds a role in a workspace and how."""

    members: list[tuple[User, str]]
    teams: list[tuple[Team, str]]
    org_admins: list[tuple[User, str]]


async def get_workspace(session: AsyncSession, org_id: uuid.UUID, workspace_id: uuid.UUID) -> Workspace:
    workspace = await session.scalar(
        select(Workspace).where(Workspace.org_id == org_id, Workspace.id == workspace_id)
    )
    if workspace is None:
        raise NotFoundError
    return workspace


async def _name_free(
    session: AsyncSession, org_id: uuid.UUID, name: str, *, besides: uuid.UUID | None = None
) -> None:
    taken = await session.scalar(
        select(Workspace.id).where(Workspace.org_id == org_id, Workspace.name == name)
    )
    if taken is not None and taken != besides:
        raise ConflictError("name_taken")


async def _flush(session: AsyncSession) -> None:
    """Flush; a concurrent insert of the same name becomes `name_taken`."""
    try:
        await session.flush()
    except IntegrityError as exc:
        if "name" in str(exc.orig):
            raise ConflictError("name_taken") from exc
        raise


async def create_workspace(session: AsyncSession, actor: audit.Actor, name: str, timezone: str) -> Workspace:
    cleaned, tz = clean_name(name), clean_timezone(timezone)
    await _name_free(session, actor.org_id, cleaned)
    workspace = Workspace(id=uuid.uuid4(), org_id=actor.org_id, name=cleaned, timezone=tz)
    session.add(workspace)
    await _flush(session)
    await session.refresh(workspace)
    await audit.record(
        session,
        actor,
        "workspace.created",
        "workspace",
        workspace.id,
        workspace_id=workspace.id,
        details={"name": cleaned, "timezone": tz},
    )
    return workspace


async def update_workspace(
    session: AsyncSession,
    actor: audit.Actor,
    workspace_id: uuid.UUID,
    *,
    name: str | None = None,
    timezone: str | None = None,
) -> Workspace:
    """Rename or change the timezone; unchanged values write no event."""
    workspace = await get_workspace(session, actor.org_id, workspace_id)
    before = {"name": workspace.name, "timezone": workspace.timezone}
    after = dict(before)
    if name is not None:
        after["name"] = clean_name(name)
    if timezone is not None:
        after["timezone"] = clean_timezone(timezone)
    if after == before:
        return workspace
    if after["name"] != before["name"]:
        await _name_free(session, actor.org_id, after["name"], besides=workspace.id)
    workspace.name, workspace.timezone = after["name"], after["timezone"]
    await _flush(session)
    changed = {k: audit.change(before[k], after[k]) for k in before if before[k] != after[k]}
    await audit.record(
        session,
        actor,
        "workspace.updated",
        "workspace",
        workspace.id,
        workspace_id=workspace.id,
        details=changed,
    )
    return workspace


async def delete_workspace(session: AsyncSession, actor: audit.Actor, workspace_id: uuid.UUID) -> None:
    """Delete an empty workspace; its grants and open invitations go with it (FK cascade)."""
    workspace = await get_workspace(session, actor.org_id, workspace_id)
    if workspace.id == DEFAULT_WORKSPACE_ID:
        raise ConflictError("default_workspace")
    content = union_all(
        *(select(table.id).where(table.workspace_id == workspace.id).limit(1) for table in CONTENT)
    ).subquery()
    if await session.scalar(select(exists().select_from(content))):
        raise ConflictError("workspace_not_empty")
    await session.delete(workspace)
    await session.flush()
    await audit.record(
        session,
        actor,
        "workspace.deleted",
        "workspace",
        workspace_id,
        workspace_id=workspace_id,
        details={"name": workspace.name},
    )


async def get_access(session: AsyncSession, org_id: uuid.UUID, workspace_id: uuid.UUID) -> Access:
    await get_workspace(session, org_id, workspace_id)
    members = await session.execute(
        select(User, WorkspaceMembership.role)
        .join(WorkspaceMembership, WorkspaceMembership.user_id == User.id)
        .where(WorkspaceMembership.workspace_id == workspace_id)
        .order_by(User.email)
    )
    teams = await session.execute(
        select(Team, WorkspaceTeamRole.role)
        .join(WorkspaceTeamRole, WorkspaceTeamRole.team_id == Team.id)
        .where(WorkspaceTeamRole.workspace_id == workspace_id)
        .order_by(Team.name)
    )
    admins = await session.execute(
        select(User, OrgMembership.role)
        .join(OrgMembership, OrgMembership.user_id == User.id)
        .where(
            OrgMembership.org_id == org_id,
            OrgMembership.role.in_([r.value for r in ORG_ADMINS]),
            OrgMembership.user_id != BOOTSTRAP_USER_ID,
            User.disabled_at.is_(None),
        )
        .order_by(User.email)
    )
    return Access(
        members=list(members.tuples().all()),
        teams=list(teams.tuples().all()),
        org_admins=list(admins.tuples().all()),
    )


async def set_member(
    session: AsyncSession,
    actor: audit.Actor,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    role: WorkspaceRole,
) -> None:
    """Give an org member a direct role in the workspace, replacing a previous one."""
    workspace = await get_workspace(session, actor.org_id, workspace_id)
    user = await org_user(session, actor.org_id, user_id)
    grant = await session.scalar(
        select(WorkspaceMembership).where(
            WorkspaceMembership.workspace_id == workspace_id, WorkspaceMembership.user_id == user_id
        )
    )
    if grant is not None and grant.role == role.value:
        return
    before = None if grant is None else grant.role
    if grant is None:
        session.add(
            WorkspaceMembership(
                org_id=actor.org_id, workspace_id=workspace_id, user_id=user_id, role=role.value
            )
        )
    else:
        grant.role = role.value
    await session.flush()
    await audit.record(
        session,
        actor,
        "workspace.member_set",
        "user",
        user_id,
        workspace_id=workspace_id,
        details={"workspace": workspace.name, "email": user.email, **audit.change(before, role.value)},
    )


async def remove_member(
    session: AsyncSession, actor: audit.Actor, workspace_id: uuid.UUID, user_id: uuid.UUID
) -> None:
    workspace = await get_workspace(session, actor.org_id, workspace_id)
    grant = await session.scalar(
        select(WorkspaceMembership).where(
            WorkspaceMembership.workspace_id == workspace_id, WorkspaceMembership.user_id == user_id
        )
    )
    if grant is None:
        raise NotFoundError
    user = await session.scalar(select(User).where(User.id == user_id))
    role = grant.role
    await session.execute(
        delete(WorkspaceMembership).where(
            WorkspaceMembership.workspace_id == workspace_id, WorkspaceMembership.user_id == user_id
        )
    )
    await audit.record(
        session,
        actor,
        "workspace.member_removed",
        "user",
        user_id,
        workspace_id=workspace_id,
        details={"workspace": workspace.name, "email": None if user is None else user.email, "role": role},
    )


async def set_team(
    session: AsyncSession,
    actor: audit.Actor,
    workspace_id: uuid.UUID,
    team_id: uuid.UUID,
    role: WorkspaceRole,
) -> None:
    """Give a team a role in the workspace, replacing a previous one."""
    workspace = await get_workspace(session, actor.org_id, workspace_id)
    team = await get_team(session, actor.org_id, team_id)
    grant = await session.scalar(
        select(WorkspaceTeamRole).where(
            WorkspaceTeamRole.workspace_id == workspace_id, WorkspaceTeamRole.team_id == team_id
        )
    )
    if grant is not None and grant.role == role.value:
        return
    before = None if grant is None else grant.role
    if grant is None:
        session.add(
            WorkspaceTeamRole(
                org_id=actor.org_id, workspace_id=workspace_id, team_id=team_id, role=role.value
            )
        )
    else:
        grant.role = role.value
    await session.flush()
    await audit.record(
        session,
        actor,
        "workspace.team_set",
        "team",
        team_id,
        workspace_id=workspace_id,
        details={"workspace": workspace.name, "team": team.name, **audit.change(before, role.value)},
    )


async def remove_team(
    session: AsyncSession, actor: audit.Actor, workspace_id: uuid.UUID, team_id: uuid.UUID
) -> None:
    workspace = await get_workspace(session, actor.org_id, workspace_id)
    team = await get_team(session, actor.org_id, team_id)
    grant = await session.scalar(
        select(WorkspaceTeamRole).where(
            WorkspaceTeamRole.workspace_id == workspace_id, WorkspaceTeamRole.team_id == team_id
        )
    )
    if grant is None:
        raise NotFoundError
    role = grant.role
    await session.execute(
        delete(WorkspaceTeamRole).where(
            WorkspaceTeamRole.workspace_id == workspace_id, WorkspaceTeamRole.team_id == team_id
        )
    )
    await audit.record(
        session,
        actor,
        "workspace.team_removed",
        "team",
        team_id,
        workspace_id=workspace_id,
        details={"workspace": workspace.name, "team": team.name, "role": role},
    )
