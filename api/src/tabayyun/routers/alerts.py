"""Alert rule endpoints (spec 031): rules, their notifications and a test send.

Reading needs any role in the workspace; changing needs its admin role, because a rule sends
data out of the workspace.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from tabayyun.alerts import delivery, evaluate, rules
from tabayyun.auth.deps import client_ip
from tabayyun.authz import (
    ManageScope,
    Principal,
    ReadScope,
    Scope,
    get_principal,
    get_session,
    workspace_role,
)
from tabayyun.authz.roles import Action, allows
from tabayyun.db import for_org
from tabayyun.db.models import AlertRule
from tabayyun.mail import SmtpConfig
from tabayyun.secrets import CredentialsUnavailableError
from tabayyun.services.admin import audit

router = APIRouter(prefix="/api/alerts", tags=["alerts"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
ActorDep = Annotated[Principal, Depends(get_principal)]


class RuleCreate(BaseModel):
    """Body of `POST /api/alerts`."""

    model_config = ConfigDict(extra="forbid")

    name: str
    kind: str
    condition: dict[str, Any]
    channel: str
    recipients: list[str] = []
    url: str | None = None
    secret: str | None = None
    throttle_minutes: int = rules.DEFAULT_THROTTLE_MINUTES
    enabled: bool = True


class RulePatch(BaseModel):
    """Body of `PATCH /api/alerts/{id}`: any of the fields; `secret: null` removes the secret."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    kind: str | None = None
    condition: dict[str, Any] | None = None
    channel: str | None = None
    recipients: list[str] | None = None
    url: str | None = None
    secret: str | None = None
    throttle_minutes: int | None = None
    enabled: bool | None = None


def _actor(request: Request, principal: Principal) -> audit.Actor:
    return audit.Actor(
        user_id=principal.user_id,
        org_id=principal.org_id,
        ip_address=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )


def _refused(exc: rules.RuleError) -> HTTPException:
    return HTTPException(status_code=422, detail=exc.errors)


async def _rule(session: AsyncSession, scope: Scope, rule_id: str) -> AlertRule:
    """The workspace's rule, or 404 (also for a malformed id)."""
    try:
        parsed = uuid.UUID(rule_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="not found") from exc
    rule = await rules.get_rule(session, scope, parsed)
    if rule is None:
        raise HTTPException(status_code=404, detail="not found")
    return rule


async def _may_manage(session: AsyncSession, principal: Principal, scope: Scope) -> bool:
    """Whether the caller is admin of the workspace: only then the webhook URL is returned."""
    role = await workspace_role(session, principal, scope.workspace_id)
    return role is not None and allows(role, Action.MANAGE)


@router.get("")
async def list_rules(session: SessionDep, principal: ActorDep, scope: ReadScope) -> dict[str, Any]:
    """The workspace's rules by name, each with its newest notification."""
    found = await rules.list_rules(session, scope)
    last = await rules.last_notifications(session, [r.id for r in found])
    with_url = await _may_manage(session, principal, scope)
    return {
        "items": [rules.rule_to_dict(r, last.get(r.id), with_url=with_url) for r in found],
        "next_cursor": None,
    }


@router.get("/channels")
async def channels(request: Request, scope: ReadScope) -> dict[str, Any]:
    """Which channels this server can use: email needs SMTP settings, a signing secret the
    master key."""
    state = request.app.state
    return {
        "email": {"available": SmtpConfig.from_settings(state.settings) is not None},
        "webhook": {"available": True, "signing": state.keyring is not None},
    }


@router.post("", status_code=201)
async def create_rule(
    body: RuleCreate, request: Request, session: SessionDep, principal: ActorDep, scope: ManageScope
) -> dict[str, Any]:
    """Create a rule. 422 with the wrong fields, 409 `name_taken`, 409
    `credentials_unavailable` for a secret without the master key."""
    data = rules.RuleData(
        name=body.name,
        kind=body.kind,
        condition=body.condition,
        channel=body.channel,
        recipients=body.recipients,
        url=body.url,
        throttle_minutes=body.throttle_minutes,
        enabled=body.enabled,
    )
    try:
        rule = await rules.create_rule(
            session, scope, principal.user_id, data, secret=body.secret, keyring=request.app.state.keyring
        )
    except rules.RuleError as exc:
        raise _refused(exc) from exc
    except rules.RuleNameTakenError as exc:
        raise HTTPException(status_code=409, detail="name_taken") from exc
    except CredentialsUnavailableError as exc:
        raise HTTPException(status_code=409, detail="credentials_unavailable") from exc
    await audit.record(
        session,
        _actor(request, principal),
        "alert_rule.created",
        "alert_rule",
        rule.id,
        workspace_id=scope.workspace_id,
        details=rules.audit_details(rule),
    )
    return rules.rule_to_dict(rule, None, with_url=True)


@router.get("/{rule_id}")
async def get_rule(
    rule_id: str, session: SessionDep, principal: ActorDep, scope: ReadScope
) -> dict[str, Any]:
    """One rule with its newest notification."""
    rule = await _rule(session, scope, rule_id)
    last = await rules.last_notifications(session, [rule.id])
    return rules.rule_to_dict(rule, last.get(rule.id), with_url=await _may_manage(session, principal, scope))


@router.patch("/{rule_id}")
async def patch_rule(
    rule_id: str,
    body: RulePatch,
    request: Request,
    session: SessionDep,
    principal: ActorDep,
    scope: ManageScope,
) -> dict[str, Any]:
    """Change the given fields of a rule; the result is validated as a whole."""
    rule = await _rule(session, scope, rule_id)
    before = rules.audit_details(rule)
    given = body.model_fields_set
    data = rules.data_of(rule)
    for name in ("name", "kind", "condition", "channel", "recipients", "url", "throttle_minutes", "enabled"):
        if name in given:
            setattr(data, name, getattr(body, name))
    # Changing the channel drops the other channel's target unless the request names it.
    if "channel" in given and body.channel == "email" and "url" not in given:
        data.url = None
    if "channel" in given and body.channel == "webhook" and "recipients" not in given:
        data.recipients = []
    try:
        await rules.change_rule(
            session,
            rule,
            data,
            secret=body.secret if "secret" in given else rules.UNSET,
            keyring=request.app.state.keyring,
        )
    except rules.RuleError as exc:
        raise _refused(exc) from exc
    except rules.RuleNameTakenError as exc:
        raise HTTPException(status_code=409, detail="name_taken") from exc
    except CredentialsUnavailableError as exc:
        raise HTTPException(status_code=409, detail="credentials_unavailable") from exc
    after = rules.audit_details(rule)
    if after != before or "secret" in given:
        await audit.record(
            session,
            _actor(request, principal),
            "alert_rule.updated",
            "alert_rule",
            rule.id,
            workspace_id=scope.workspace_id,
            details={**audit.change(before, after), "secret_changed": "secret" in given},
        )
    last = await rules.last_notifications(session, [rule.id])
    return rules.rule_to_dict(rule, last.get(rule.id), with_url=True)


@router.delete("/{rule_id}", status_code=204)
async def delete_rule(
    rule_id: str, request: Request, session: SessionDep, principal: ActorDep, scope: ManageScope
) -> Response:
    """Delete a rule and its notifications."""
    rule = await _rule(session, scope, rule_id)
    details = rules.audit_details(rule)
    await session.delete(rule)
    await audit.record(
        session,
        _actor(request, principal),
        "alert_rule.deleted",
        "alert_rule",
        rule.id,
        workspace_id=scope.workspace_id,
        details=details,
    )
    return Response(status_code=204)


@router.post("/{rule_id}/test", status_code=202)
async def test_rule(
    rule_id: str,
    request: Request,
    background: BackgroundTasks,
    session: SessionDep,
    principal: ActorDep,
    scope: ManageScope,
) -> JSONResponse:
    """Send a test notification now, whatever the throttle says. The worker sends it; with
    inline jobs this process does, after the response."""
    rule = await _rule(session, scope, rule_id)
    state = request.app.state
    inline = state.settings.inline_jobs
    now = datetime.now(UTC)
    notification = await evaluate.start_test(session, rule, now=now, defer=not inline)
    await audit.record(
        session,
        _actor(request, principal),
        "alert_rule.tested",
        "alert_rule",
        rule.id,
        workspace_id=scope.workspace_id,
        details={"name": rule.name, "channel": rule.channel, "notification_id": str(notification.id)},
    )
    if inline:
        # The background task may run before the session dependency commits.
        await session.commit()
        background.add_task(
            delivery.deliver_inline,
            for_org(state.session_factory, scope.org_id),
            [notification],
            delivery.Deps.from_settings(state.settings, net=state.net_policy, keyring=state.keyring),
            now,
        )
    return JSONResponse(status_code=202, content={"notification_id": str(notification.id)})


@router.get("/{rule_id}/notifications")
async def list_notifications(
    rule_id: str,
    session: SessionDep,
    scope: ReadScope,
    limit: Annotated[int, Query(ge=1, le=rules.MAX_HISTORY)] = rules.MAX_HISTORY,
) -> dict[str, Any]:
    """The rule's notifications, newest first."""
    rule = await _rule(session, scope, rule_id)
    found = await rules.list_notifications(session, rule, limit)
    return {"items": [rules.notification_to_dict(n) for n in found]}
