"""Teams (spec 014): named sets of org members that hold roles in workspaces."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tabayyun.db.models import (
    BOOTSTRAP_USER_ID,
    OrgMembership,
    Team,
    TeamMember,
    User,
    Workspace,
    WorkspaceTeamRole,
)
from tabayyun.services.admin import audit
from tabayyun.services.admin.errors import ConflictError, NotFoundError
from tabayyun.services.admin.names import clean_name


@dataclass
class TeamView:
    """A team with its members and its workspace roles."""

    id: uuid.UUID
    name: str
    members: list[User] = field(default_factory=list)
    workspaces: list[tuple[Workspace, str]] = field(default_factory=list)


async def list_teams(session: AsyncSession, org_id: uuid.UUID) -> list[TeamView]:
    """Every team of the org by name, in three queries."""
    teams = {
        t.id: TeamView(id=t.id, name=t.name)
        for t in await session.scalars(select(Team).where(Team.org_id == org_id).order_by(Team.name))
    }
    members = await session.execute(
        select(TeamMember.team_id, User)
        .join(User, User.id == TeamMember.user_id)
        .where(TeamMember.org_id == org_id)
        .order_by(User.email)
    )
    for team_id, user in members.tuples():
        teams[team_id].members.append(user)
    roles = await session.execute(
        select(WorkspaceTeamRole.team_id, Workspace, WorkspaceTeamRole.role)
        .join(Workspace, Workspace.id == WorkspaceTeamRole.workspace_id)
        .where(WorkspaceTeamRole.org_id == org_id)
        .order_by(Workspace.name)
    )
    for team_id, workspace, role in roles.tuples():
        teams[team_id].workspaces.append((workspace, role))
    return list(teams.values())


async def get_team(session: AsyncSession, org_id: uuid.UUID, team_id: uuid.UUID) -> Team:
    team = await session.scalar(select(Team).where(Team.org_id == org_id, Team.id == team_id))
    if team is None:
        raise NotFoundError
    return team


async def _name_free(
    session: AsyncSession, org_id: uuid.UUID, name: str, *, besides: uuid.UUID | None = None
) -> None:
    taken = await session.scalar(select(Team.id).where(Team.org_id == org_id, Team.name == name))
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


async def create_team(session: AsyncSession, actor: audit.Actor, name: str) -> Team:
    cleaned = clean_name(name)
    await _name_free(session, actor.org_id, cleaned)
    team = Team(id=uuid.uuid4(), org_id=actor.org_id, name=cleaned)
    session.add(team)
    await _flush(session)
    await audit.record(session, actor, "team.created", "team", team.id, details={"name": cleaned})
    return team


async def rename_team(session: AsyncSession, actor: audit.Actor, team_id: uuid.UUID, name: str) -> Team:
    team = await get_team(session, actor.org_id, team_id)
    cleaned = clean_name(name)
    if cleaned == team.name:
        return team
    await _name_free(session, actor.org_id, cleaned, besides=team.id)
    before, team.name = team.name, cleaned
    await _flush(session)
    await audit.record(session, actor, "team.renamed", "team", team.id, details=audit.change(before, cleaned))
    return team


async def delete_team(session: AsyncSession, actor: audit.Actor, team_id: uuid.UUID) -> None:
    """Delete a team; its memberships and workspace roles go with it (FK cascade)."""
    team = await get_team(session, actor.org_id, team_id)
    await session.delete(team)
    await session.flush()
    await audit.record(session, actor, "team.deleted", "team", team_id, details={"name": team.name})


async def org_user(session: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID) -> User:
    """A member of the org (not the bootstrap user); NotFoundError otherwise."""
    if user_id == BOOTSTRAP_USER_ID:
        raise NotFoundError
    user = await session.scalar(
        select(User)
        .join(OrgMembership, OrgMembership.user_id == User.id)
        .where(OrgMembership.org_id == org_id, User.id == user_id)
    )
    if user is None:
        raise NotFoundError
    return user


async def add_member(
    session: AsyncSession, actor: audit.Actor, team_id: uuid.UUID, user_id: uuid.UUID
) -> None:
    """Add an org member to the team; already in it is a no-op without an event."""
    team = await get_team(session, actor.org_id, team_id)
    user = await org_user(session, actor.org_id, user_id)
    present = await session.scalar(
        select(TeamMember.user_id).where(TeamMember.team_id == team_id, TeamMember.user_id == user_id)
    )
    if present is not None:
        return
    session.add(TeamMember(org_id=actor.org_id, team_id=team_id, user_id=user_id))
    await session.flush()
    await audit.record(
        session, actor, "team.member_added", "team", team_id, details={"team": team.name, "email": user.email}
    )


async def remove_member(
    session: AsyncSession, actor: audit.Actor, team_id: uuid.UUID, user_id: uuid.UUID
) -> None:
    """Remove someone from the team; NotFoundError when they are not in it."""
    team = await get_team(session, actor.org_id, team_id)
    present = await session.scalar(
        select(TeamMember.user_id).where(TeamMember.team_id == team_id, TeamMember.user_id == user_id)
    )
    user = await session.scalar(select(User).where(User.id == user_id))
    if present is None or user is None:
        raise NotFoundError
    await session.execute(
        delete(TeamMember).where(TeamMember.team_id == team_id, TeamMember.user_id == user_id)
    )
    await audit.record(
        session,
        actor,
        "team.member_removed",
        "team",
        team_id,
        details={"team": team.name, "email": user.email},
    )
