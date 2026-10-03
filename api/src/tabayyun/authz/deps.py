"""FastAPI dependencies: who the request acts for, its tenant session, and its scope.

With the OIDC login (spec 013) the principal is the session's user in the org it signed in to;
in `dev` mode every request acts as the bootstrap user, owner of the default org. The workspace
is the one the client names in `X-Tabayyun-Workspace` (spec 014), else a visible default. Tests
override `get_principal` and `get_workspace_id`.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy import case, select
from sqlalchemy.ext.asyncio import AsyncSession

from tabayyun.auth.deps import require_session, settings_of
from tabayyun.authz.policy import ForbiddenError, NotVisibleError, authorize, visible_workspaces
from tabayyun.authz.roles import Action
from tabayyun.authz.scope import Principal, Scope
from tabayyun.db import open_session
from tabayyun.db.models import BOOTSTRAP_USER_ID, DEFAULT_ORG_ID, DEFAULT_WORKSPACE_ID, Workspace

BOOTSTRAP_PRINCIPAL = Principal(user_id=BOOTSTRAP_USER_ID, org_id=DEFAULT_ORG_ID)
WORKSPACE_HEADER = "X-Tabayyun-Workspace"


async def get_principal(request: Request) -> Principal:
    """The user the request acts for: 401 without a live session, 403 `no_access` for a session
    without an org; the bootstrap user in dev mode."""
    if settings_of(request).resolved_auth_mode == "dev":
        return BOOTSTRAP_PRINCIPAL
    session = await require_session(request)
    if session.org_id is None:
        raise HTTPException(status_code=403, detail="no_access")
    return Principal(user_id=session.user_id, org_id=session.org_id)


async def get_session(
    request: Request, principal: Annotated[Principal, Depends(get_principal)]
) -> AsyncIterator[AsyncSession]:
    """One transaction per request in the principal's org, committed on success."""
    async for session in open_session(request, principal.org_id):
        yield session


async def get_workspace_id(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    principal: Annotated[Principal, Depends(get_principal)],
) -> uuid.UUID:
    """The workspace the request acts in: the `X-Tabayyun-Workspace` header (400
    `invalid_workspace` when malformed), else the default workspace when visible, else the
    oldest visible one. Visibility of a named workspace is `require()`'s job (404)."""
    named = request.headers.get(WORKSPACE_HEADER)
    if named is not None:
        try:
            return uuid.UUID(named.strip())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid_workspace") from exc
    fallback = await session.scalar(
        select(Workspace.id)
        .where(Workspace.id.in_(visible_workspaces(principal)))
        .order_by(
            case((Workspace.id == DEFAULT_WORKSPACE_ID, 0), else_=1), Workspace.created_at, Workspace.id
        )
        .limit(1)
    )
    # Nothing visible: the default id, which `require()` then answers with 404 as before.
    return fallback or DEFAULT_WORKSPACE_ID


def require(action: Action) -> Callable[..., Awaitable[Scope]]:
    """Dependency that authorizes `action` in the request's workspace and returns its scope."""

    async def dependency(
        session: Annotated[AsyncSession, Depends(get_session)],
        principal: Annotated[Principal, Depends(get_principal)],
        workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    ) -> Scope:
        """404 without any role in the workspace, 403 with a role too low for the action."""
        try:
            await authorize(session, principal, action, workspace_id)
        except NotVisibleError as exc:
            raise HTTPException(status_code=404, detail="not found") from exc
        except ForbiddenError as exc:
            raise HTTPException(status_code=403, detail=f"not allowed to {action.value}") from exc
        return Scope(org_id=principal.org_id, workspace_id=workspace_id)

    return dependency


ReadScope = Annotated[Scope, Depends(require(Action.READ))]
WriteScope = Annotated[Scope, Depends(require(Action.WRITE))]
ManageScope = Annotated[Scope, Depends(require(Action.MANAGE))]
