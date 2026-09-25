"""Roles, actions and how they combine (spec 007, ADR-0007). Pure: no database access.

Org roles: `owner` > `admin` > `member`. Workspace roles: `admin` > `editor` > `viewer`.
A principal's effective role in a workspace is the highest of their direct workspace role,
the roles of their teams in that workspace, and `admin` when their org role is `owner` or
`admin`. Someone who is not a member of the org has no role anywhere in it, whatever grants
are left over. Actions need at least: `read` viewer, `write` editor, `manage` admin.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum


class OrgRole(StrEnum):
    """A user's role in an org."""

    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"


class WorkspaceRole(StrEnum):
    """A user's or a team's role in a workspace."""

    VIEWER = "viewer"
    EDITOR = "editor"
    ADMIN = "admin"


class Action(StrEnum):
    """What a request wants to do in a workspace."""

    READ = "read"
    WRITE = "write"
    MANAGE = "manage"


RANK = {WorkspaceRole.VIEWER: 1, WorkspaceRole.EDITOR: 2, WorkspaceRole.ADMIN: 3}
REQUIRED = {
    Action.READ: WorkspaceRole.VIEWER,
    Action.WRITE: WorkspaceRole.EDITOR,
    Action.MANAGE: WorkspaceRole.ADMIN,
}
ORG_ADMINS = (OrgRole.OWNER, OrgRole.ADMIN)


def effective_role(
    org_role: OrgRole | None, direct: WorkspaceRole | None, team_roles: Iterable[WorkspaceRole]
) -> WorkspaceRole | None:
    """The highest workspace role the grants add up to, or None when there is none."""
    if org_role is None:
        return None
    candidates = [role for role in (direct, *team_roles) if role is not None]
    if org_role in ORG_ADMINS:
        candidates.append(WorkspaceRole.ADMIN)
    return max(candidates, key=RANK.__getitem__, default=None)


def allows(role: WorkspaceRole | None, action: Action) -> bool:
    """Whether `role` is enough for `action`; no role allows nothing."""
    return role is not None and RANK[role] >= RANK[REQUIRED[action]]
