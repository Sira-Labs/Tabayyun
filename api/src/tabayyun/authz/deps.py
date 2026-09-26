"""FastAPI dependencies: who the request acts for, its tenant session, and its scope.

With the OIDC login (spec 013) the principal is the session's user in the org it signed in to;
in `dev` mode every request acts as the bootstrap user, owner of the default org. Until the
workspace picker exists (spec 014, S8-5) the workspace is the default one. Tests override
`get_principal` and `get_workspace_id`.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from tabayyun.auth.deps import require_session, settings_of
from tabayyun.authz.policy import ForbiddenError, NotVisibleError, authorize
from tabayyun.authz.roles import Action
from tabayyun.authz.scope import Principal, Scope
from tabayyun.db import open_session
from tabayyun.db.models import BOOTSTRAP_USER_ID, DEFAULT_ORG_ID, DEFAULT_WORKSPACE_ID

BOOTSTRAP_PRINCIPAL = Principal(user_id=BOOTSTRAP_USER_ID, org_id=DEFAULT_ORG_ID)


async def get_principal(request: Request) -> Principal:
    """The user the request acts for: 401 without a live session, 403 `no_access` for a session
    without an org; the bootstrap user in dev mode."""
    if settings_of(request).resolved_auth_mode == "dev":
        return BOOTSTRAP_PRINCIPAL
    session = await require_session(request)
    if session.org_id is None:
        raise HTTPException(status_code=403, detail="no_access")
    return Principal(user_id=session.user_id, org_id=session.org_id)


async def get_workspace_id() -> uuid.UUID:
    """The workspace the request acts in; the default one until spec 014 lets clients choose."""
    return DEFAULT_WORKSPACE_ID


async def get_session(
    request: Request, principal: Annotated[Principal, Depends(get_principal)]
) -> AsyncIterator[AsyncSession]:
    """One transaction per request in the principal's org, committed on success."""
    async for session in open_session(request, principal.org_id):
        yield session


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
