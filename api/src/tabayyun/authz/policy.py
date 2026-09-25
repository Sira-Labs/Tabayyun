"""`authorize()` and `visible_workspaces()`: the one authorization path (ADR-0007, spec 007).

Both read the membership tables inside the caller's tenant transaction, so row-level security
already limits them to the principal's org. `authorize` raises `NotVisibleError` when the principal
has no role in the workspace (the caller answers 404, as for a row that does not exist) and
`ForbiddenError` when the role is too low for the action (403).
"""

from __future__ import annotations

import uuid

from sqlalchemy import Select, and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from tabayyun.authz.roles import ORG_ADMINS, Action, OrgRole, WorkspaceRole, allows, effective_role
from tabayyun.authz.scope import Principal
from tabayyun.db.models import (
    OrgMembership,
    TeamMember,
    User,
    Workspace,
    WorkspaceMembership,
    WorkspaceTeamRole,
)


class NotVisibleError(Exception):
    """The principal has no role in the workspace: it does not exist for them."""


class ForbiddenError(Exception):
    """The principal sees the workspace, but their role does not allow the action."""


def _active_member(principal: Principal) -> Select[tuple[str]]:
    """The principal's org role, only while their user is not disabled."""
    return (
        select(OrgMembership.role)
        .join(User, User.id == OrgMembership.user_id)
        .where(
            OrgMembership.org_id == principal.org_id,
            OrgMembership.user_id == principal.user_id,
            User.disabled_at.is_(None),
        )
    )


async def workspace_role(
    session: AsyncSession, principal: Principal, workspace_id: uuid.UUID
) -> WorkspaceRole | None:
    """The principal's effective role in the workspace, or None when they have none."""
    in_org = await session.scalar(
        select(Workspace.id).where(Workspace.id == workspace_id, Workspace.org_id == principal.org_id)
    )
    if in_org is None:
        return None
    org_role = await session.scalar(_active_member(principal))
    direct = await session.scalar(
        select(WorkspaceMembership.role).where(
            WorkspaceMembership.workspace_id == workspace_id,
            WorkspaceMembership.user_id == principal.user_id,
        )
    )
    team_roles = await session.scalars(
        select(WorkspaceTeamRole.role)
        .join(TeamMember, TeamMember.team_id == WorkspaceTeamRole.team_id)
        .where(WorkspaceTeamRole.workspace_id == workspace_id, TeamMember.user_id == principal.user_id)
    )
    return effective_role(
        None if org_role is None else OrgRole(org_role),
        None if direct is None else WorkspaceRole(direct),
        [WorkspaceRole(role) for role in team_roles],
    )


async def authorize(
    session: AsyncSession, principal: Principal, action: Action, workspace_id: uuid.UUID
) -> WorkspaceRole:
    """The principal's role when it allows `action`; raises `NotVisibleError` or `ForbiddenError`."""
    role = await workspace_role(session, principal, workspace_id)
    if role is None:
        raise NotVisibleError(str(workspace_id))
    if not allows(role, action):
        raise ForbiddenError(f"{role} may not {action}")
    return role


def visible_workspaces(principal: Principal) -> Select[tuple[uuid.UUID]]:
    """Ids of the workspaces in which the principal has any role (the `visible_ids()` of ADR-0007)."""
    member = _active_member(principal).exists()
    admin_roles = [role.value for role in ORG_ADMINS]
    org_admin = _active_member(principal).where(OrgMembership.role.in_(admin_roles)).exists()
    direct = exists().where(
        WorkspaceMembership.workspace_id == Workspace.id, WorkspaceMembership.user_id == principal.user_id
    )
    via_team = exists().where(
        WorkspaceTeamRole.workspace_id == Workspace.id,
        TeamMember.team_id == WorkspaceTeamRole.team_id,
        TeamMember.user_id == principal.user_id,
    )
    return select(Workspace.id).where(
        Workspace.org_id == principal.org_id, and_(member, or_(org_admin, direct, via_team))
    )
