"""Invitations (spec 014): bound to an email, accepted when that verified email signs in.

Creating or resending one queues its email when SMTP is configured (`email_status` `queued`);
the worker's `deliver` sends it and records `sent` or, after the last attempt, `failed`. An
invitation is valid whether or not its email goes out.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tabayyun.authz import OrgRole, WorkspaceRole
from tabayyun.db.models import Invitation, Org, OrgMembership, User, Workspace
from tabayyun.jobs.names import MAIL_QUEUE, SEND_INVITATION_TASK
from tabayyun.mail import InvitationMail, MailError, SmtpConfig, invitation_message, send
from tabayyun.services.admin import audit
from tabayyun.services.admin.errors import ConflictError, InvalidError, NotFoundError
from tabayyun.services.runs import DEFER_JOB_SQL

log = structlog.get_logger()

MAX_EMAIL = 254
# Deliberately loose: one @, no spaces, a dot in the domain. The sign-in proves the address.
EMAIL_PATTERN = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s.]+")
INVITABLE = (OrgRole.ADMIN, OrgRole.MEMBER)


class Status(StrEnum):
    """Where an invitation stands; `expired` is derived from `expires_at`, not stored."""

    PENDING = "pending"
    ACCEPTED = "accepted"
    REVOKED = "revoked"
    EXPIRED = "expired"


@dataclass(frozen=True)
class Grant:
    """What an invitation gives: an org role and optionally a role in one workspace."""

    org_role: str
    workspace_id: uuid.UUID | None = None
    workspace_role: str | None = None


@dataclass(frozen=True)
class InvitationView:
    """An invitation with the names the admin panel shows."""

    invitation: Invitation
    inviter: User | None
    workspace: Workspace | None
    status: Status


def normalise_email(raw: str) -> str:
    """Trimmed and lower-cased; InvalidError("invalid_email") unless it looks like an address."""
    email = raw.strip().lower()
    if len(email) > MAX_EMAIL or not EMAIL_PATTERN.fullmatch(email):
        raise InvalidError("invalid_email")
    return email


def status_of(invitation: Invitation, now: datetime) -> Status:
    if invitation.accepted_at is not None:
        return Status.ACCEPTED
    if invitation.revoked_at is not None:
        return Status.REVOKED
    if invitation.expires_at <= now:
        return Status.EXPIRED
    return Status.PENDING


def check_grant(grant: Grant) -> Grant:
    """Owner cannot be invited; a workspace role needs a workspace and the reverse (422)."""
    try:
        org_role = OrgRole(grant.org_role)
    except ValueError as exc:
        raise InvalidError("invalid_role") from exc
    if org_role not in INVITABLE:
        raise InvalidError("invalid_role")
    if (grant.workspace_id is None) != (grant.workspace_role is None):
        raise InvalidError("invalid_workspace_grant")
    if grant.workspace_role is not None:
        try:
            WorkspaceRole(grant.workspace_role)
        except ValueError as exc:
            raise InvalidError("invalid_role") from exc
    return grant


def _now() -> datetime:
    return datetime.now(UTC)


async def _view(session: AsyncSession, invitation: Invitation) -> InvitationView:
    inviter = await session.scalar(select(User).where(User.id == invitation.invited_by))
    workspace = (
        None
        if invitation.workspace_id is None
        else await session.scalar(select(Workspace).where(Workspace.id == invitation.workspace_id))
    )
    return InvitationView(invitation, inviter, workspace, status_of(invitation, _now()))


async def list_invitations(
    session: AsyncSession, org_id: uuid.UUID, *, pending_only: bool
) -> list[InvitationView]:
    """Invitations newest first; with `pending_only`, open and unexpired ones."""
    stmt = (
        select(Invitation, User, Workspace)
        .outerjoin(User, User.id == Invitation.invited_by)
        .outerjoin(Workspace, Workspace.id == Invitation.workspace_id)
        .where(Invitation.org_id == org_id)
    )
    if pending_only:
        stmt = stmt.where(
            Invitation.accepted_at.is_(None),
            Invitation.revoked_at.is_(None),
            Invitation.expires_at > func.now(),
        )
    rows = (await session.execute(stmt.order_by(Invitation.created_at.desc(), Invitation.id))).tuples().all()
    now = _now()
    return [InvitationView(i, u, w, status_of(i, now)) for i, u, w in rows]


async def _get(session: AsyncSession, org_id: uuid.UUID, invitation_id: uuid.UUID) -> Invitation:
    invitation = await session.scalar(
        select(Invitation).where(Invitation.org_id == org_id, Invitation.id == invitation_id)
    )
    if invitation is None:
        raise NotFoundError
    return invitation


async def _queue_email(session: AsyncSession, invitation: Invitation, *, mail_on: bool, defer: bool) -> None:
    """Mark the email queued (or not configured) and, with `defer`, add the job to this transaction."""
    invitation.email_status = "queued" if mail_on else "not_configured"
    invitation.email_error = None
    await session.flush()
    if mail_on and defer:
        args = {"invitation_id": str(invitation.id), "org_id": str(invitation.org_id)}
        await session.execute(
            DEFER_JOB_SQL, {"queue": MAIL_QUEUE, "task": SEND_INVITATION_TASK, "args": json.dumps(args)}
        )


def _details(email: str, grant: Grant, workspace: Workspace | None) -> dict[str, object]:
    return {
        "email": email,
        "org_role": grant.org_role,
        "workspace": None if workspace is None else workspace.name,
        "workspace_role": grant.workspace_role,
    }


async def create_invitation(
    session: AsyncSession,
    actor: audit.Actor,
    email: str,
    grant: Grant,
    *,
    ttl: timedelta,
    mail_on: bool,
    defer: bool,
) -> InvitationView:
    """Open an invitation; 409 `already_member` or `invitation_pending`; an expired one is revoked."""
    email = normalise_email(email)
    grant = check_grant(grant)
    workspace = None
    if grant.workspace_id is not None:
        workspace = await session.scalar(
            select(Workspace).where(Workspace.org_id == actor.org_id, Workspace.id == grant.workspace_id)
        )
        if workspace is None:
            raise NotFoundError
    member = await session.scalar(
        select(OrgMembership.user_id)
        .join(User, User.id == OrgMembership.user_id)
        .where(OrgMembership.org_id == actor.org_id, User.email == email)
    )
    if member is not None:
        raise ConflictError("already_member")
    open_one = await session.scalar(
        select(Invitation).where(
            Invitation.org_id == actor.org_id,
            Invitation.email == email,
            Invitation.accepted_at.is_(None),
            Invitation.revoked_at.is_(None),
        )
    )
    if open_one is not None:
        if status_of(open_one, _now()) is Status.PENDING:
            raise ConflictError("invitation_pending")
        open_one.revoked_at = func.now()  # expired: make room for the new one
        await session.flush()
    invitation = Invitation(
        id=uuid.uuid4(),
        org_id=actor.org_id,
        email=email,
        org_role=grant.org_role,
        workspace_id=grant.workspace_id,
        workspace_role=grant.workspace_role,
        invited_by=actor.user_id,
        expires_at=_now() + ttl,
    )
    session.add(invitation)
    await _queue_email(session, invitation, mail_on=mail_on, defer=defer)
    await audit.record(
        session,
        actor,
        "invitation.created",
        "invitation",
        invitation.id,
        workspace_id=grant.workspace_id,
        details=_details(email, grant, workspace),
    )
    await session.refresh(invitation)
    return await _view(session, invitation)


async def resend_invitation(
    session: AsyncSession,
    actor: audit.Actor,
    invitation_id: uuid.UUID,
    *,
    ttl: timedelta,
    mail_on: bool,
    defer: bool,
) -> InvitationView:
    """A new expiry and the email again, for a pending or expired invitation (409 `not_pending`)."""
    invitation = await _get(session, actor.org_id, invitation_id)
    if status_of(invitation, _now()) not in (Status.PENDING, Status.EXPIRED):
        raise ConflictError("not_pending")
    invitation.expires_at = _now() + ttl
    await _queue_email(session, invitation, mail_on=mail_on, defer=defer)
    await audit.record(
        session,
        actor,
        "invitation.resent",
        "invitation",
        invitation.id,
        workspace_id=invitation.workspace_id,
        details={"email": invitation.email},
    )
    await session.refresh(invitation)
    return await _view(session, invitation)


async def revoke_invitation(session: AsyncSession, actor: audit.Actor, invitation_id: uuid.UUID) -> None:
    """Revoke a pending invitation (409 `not_pending` for any other)."""
    invitation = await _get(session, actor.org_id, invitation_id)
    if status_of(invitation, _now()) is not Status.PENDING:
        raise ConflictError("not_pending")
    invitation.revoked_at = func.now()
    await session.flush()
    await audit.record(
        session,
        actor,
        "invitation.revoked",
        "invitation",
        invitation.id,
        workspace_id=invitation.workspace_id,
        details={"email": invitation.email},
    )


# Delivery (the worker, or the API with inline jobs)


async def _mail_of(
    factory: async_sessionmaker[AsyncSession], invitation_id: uuid.UUID, login_url: str | None
) -> InvitationMail | None:
    """The message to send, or None when the invitation is no longer pending."""
    async with factory() as session, session.begin():
        row = (
            (
                await session.execute(
                    select(Invitation, Org, User, Workspace)
                    .join(Org, Org.id == Invitation.org_id)
                    .outerjoin(User, User.id == Invitation.invited_by)
                    .outerjoin(Workspace, Workspace.id == Invitation.workspace_id)
                    .where(Invitation.id == invitation_id)
                )
            )
            .tuples()
            .one_or_none()
        )
    if row is None:
        return None
    invitation, org, inviter, workspace = row
    if status_of(invitation, _now()) is not Status.PENDING:
        return None
    role = invitation.workspace_role or invitation.org_role
    return InvitationMail(
        to=invitation.email,
        org_name=org.name,
        inviter=inviter.display_name if inviter else "An administrator",
        role=f"a{'n' if role[0] in 'aeiou' else ''} {role}",
        workspace=None if workspace is None else workspace.name,
        expires_at=invitation.expires_at,
        login_url=login_url,
    )


async def _set_status(
    factory: async_sessionmaker[AsyncSession], invitation_id: uuid.UUID, status: str, error: str | None
) -> None:
    async with factory() as session, session.begin():
        await session.execute(
            update(Invitation)
            .where(Invitation.id == invitation_id)
            .values(email_status=status, email_error=error)
        )


async def deliver(
    factory: async_sessionmaker[AsyncSession],
    config: SmtpConfig,
    invitation_id: uuid.UUID,
    *,
    public_url: str | None,
    final_attempt: bool,
) -> None:
    """Send one invitation's email. `factory` must be in the invitation's org (`for_org`).

    A failure before the last attempt raises MailError so the queue retries; the last one is
    recorded as `failed` with the server's reply and not raised.
    """
    login_url = f"{public_url.rstrip('/')}/login" if public_url else None
    mail = await _mail_of(factory, invitation_id, login_url)
    if mail is None:
        log.info("mail.skipped", invitation_id=str(invitation_id), reason="not pending")
        return
    try:
        await send(config, invitation_message(mail, config.sender))
    except MailError as exc:
        if not final_attempt:
            log.warning("mail.retry", invitation_id=str(invitation_id), error=str(exc))
            raise
        log.error("mail.failed", invitation_id=str(invitation_id), error=str(exc))
        await _set_status(factory, invitation_id, "failed", str(exc))
        return
    await _set_status(factory, invitation_id, "sent", None)
    log.info("mail.sent", invitation_id=str(invitation_id))
