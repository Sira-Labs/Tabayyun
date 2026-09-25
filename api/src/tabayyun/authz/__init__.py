"""Authorization (spec 007, ADR-0007): roles, `authorize()`, `visible_workspaces()` and the
request dependencies that give every route a tenant session and an authorized scope."""

from tabayyun.authz.deps import (
    BOOTSTRAP_PRINCIPAL,
    ManageScope,
    ReadScope,
    WriteScope,
    get_principal,
    get_session,
    get_workspace_id,
    require,
)
from tabayyun.authz.policy import (
    ForbiddenError,
    NotVisibleError,
    authorize,
    visible_workspaces,
    workspace_role,
)
from tabayyun.authz.roles import Action, OrgRole, WorkspaceRole, allows, effective_role
from tabayyun.authz.scope import Principal, Scope

__all__ = [
    "BOOTSTRAP_PRINCIPAL",
    "Action",
    "ForbiddenError",
    "ManageScope",
    "NotVisibleError",
    "OrgRole",
    "Principal",
    "ReadScope",
    "Scope",
    "WorkspaceRole",
    "WriteScope",
    "allows",
    "authorize",
    "effective_role",
    "get_principal",
    "get_session",
    "get_workspace_id",
    "require",
    "visible_workspaces",
    "workspace_role",
]
