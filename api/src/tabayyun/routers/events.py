"""The live event stream of a workspace (spec 030): `GET /api/events`.

The route checks the caller's role in a short transaction of its own and then streams without
a database connection: a request-scoped session would stay open for as long as the stream.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tabayyun.authz import Principal, get_principal
from tabayyun.authz.deps import get_workspace_id
from tabayyun.authz.policy import ForbiddenError, NotVisibleError, authorize
from tabayyun.authz.roles import Action
from tabayyun.authz.scope import Scope
from tabayyun.db import for_org
from tabayyun.events.broker import Broker, TooManyStreamsError
from tabayyun.events.listener import Listener
from tabayyun.events.stream import GRACE_S, EventStreamResponse, stream
from tabayyun.settings import Settings

router = APIRouter(prefix="/api/events", tags=["events"])

RETRY_AFTER_S = 30
# The stream must reach the browser as it is written: no cache, no proxy buffering.
STREAM_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


async def _scope(request: Request, principal: Principal, workspace: str | None) -> Scope:
    """The workspace the stream watches, authorized for reading: the `workspace` parameter
    (`EventSource` cannot send the header), else as on every other route."""
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with for_org(factory, principal.org_id)() as session, session.begin():
        if workspace is None:
            workspace_id = await get_workspace_id(request, session, principal)
        else:
            try:
                workspace_id = uuid.UUID(workspace.strip())
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="invalid_workspace") from exc
        try:
            await authorize(session, principal, Action.READ, workspace_id)
        except NotVisibleError as exc:
            raise HTTPException(status_code=404, detail="not found") from exc
        except ForbiddenError as exc:
            raise HTTPException(status_code=403, detail=f"not allowed to {Action.READ.value}") from exc
    return Scope(org_id=principal.org_id, workspace_id=workspace_id)


# Documented as a plain streaming response: FastAPI reads the status of an OpenAPI
# operation from the response class's signature, which the subclass narrows.
@router.get("", response_class=StreamingResponse)
async def events(
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
    workspace: Annotated[str | None, Query(max_length=64)] = None,
) -> EventStreamResponse:
    """`text/event-stream` of the workspace's events: `run.completed`, `finding.created` and
    `resync` (reload: events may have been missed). 404 without a role in the workspace, 503
    `too_many_streams` when the process serves as many streams as it may."""
    scope = await _scope(request, principal, workspace)
    broker: Broker = request.app.state.event_broker
    listener: Listener = request.app.state.event_listener
    try:
        subscription = broker.subscribe(scope)
    except TooManyStreamsError as exc:
        raise HTTPException(
            status_code=503, detail="too_many_streams", headers={"Retry-After": str(RETRY_AFTER_S)}
        ) from exc
    listener.ensure_started()
    settings: Settings = request.app.state.settings
    frames = stream(
        subscription, heartbeat_s=settings.events_heartbeat_s, max_age_s=settings.events_max_age_s
    )
    # The stream's place is given back however the response ends, also when a client that
    # stopped reading has to be cut off after the maximum age.
    return EventStreamResponse(
        frames,
        deadline_s=settings.events_max_age_s + GRACE_S,
        on_close=lambda: broker.unsubscribe(subscription),
        headers=STREAM_HEADERS,
    )
