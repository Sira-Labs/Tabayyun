"""Role resolution truth table (spec 007): pure, no database."""

from __future__ import annotations

import itertools

import pytest

from tabayyun.authz import Action, OrgRole, WorkspaceRole, allows, effective_role

ORG_ROLES = [None, *OrgRole]
WS_ROLES = [None, *WorkspaceRole]
RANK = {None: 0, WorkspaceRole.VIEWER: 1, WorkspaceRole.EDITOR: 2, WorkspaceRole.ADMIN: 3}
NEEDS = {Action.READ: 1, Action.WRITE: 2, Action.MANAGE: 3}


def _expected(org: OrgRole | None, direct: WorkspaceRole | None, team: WorkspaceRole | None) -> int:
    """The rule of spec 007 written out independently of the implementation."""
    if org is None:
        return 0
    floor = 3 if org in (OrgRole.OWNER, OrgRole.ADMIN) else 0
    return max(floor, RANK[direct], RANK[team])


@pytest.mark.parametrize(("org", "direct", "team"), list(itertools.product(ORG_ROLES, WS_ROLES, WS_ROLES)))
def test_effective_role_and_actions(org, direct, team):
    """Every combination of org role, direct role and team role, against every action."""
    role = effective_role(org, direct, [] if team is None else [team])
    assert RANK[role] == _expected(org, direct, team)
    for action, needed in NEEDS.items():
        assert allows(role, action) is (RANK[role] >= needed)


def test_highest_of_several_team_roles_wins():
    """A user in several teams gets the highest of their roles."""
    teams = [WorkspaceRole.VIEWER, WorkspaceRole.EDITOR, WorkspaceRole.VIEWER]
    assert effective_role(OrgRole.MEMBER, None, teams) is WorkspaceRole.EDITOR


def test_no_role_allows_nothing():
    """Without any role every action is denied."""
    assert not any(allows(None, action) for action in Action)
