"""Sending a notification that is due and recording what became of it (spec 031)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import structlog
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tabayyun import __version__, mail
from tabayyun.alerts import evaluate, messages, rules
from tabayyun.authz.scope import Scope
from tabayyun.connectors import NetPolicy
from tabayyun.connectors.errors import TargetRefusedError
from tabayyun.db.models import AlertNotification, AlertRule, Workspace
from tabayyun.secrets import CredentialsError, CredentialsUnavailableError, Keyring
from tabayyun.settings import Settings

log = structlog.get_logger()

ATTEMPTS = 3
WEBHOOK_TIMEOUT_S = 10.0
# A notification left `sending` this long belongs to a worker that died: it is taken again.
STUCK_AFTER = timedelta(minutes=10)
MAX_ERROR = 300
EMAIL_OFF = "email is not configured on this server"
# Crosses orgs, so it is a SECURITY DEFINER function (migration 0017).
REQUEUE_SQL = text("SELECT id, org_id FROM tabayyun_requeue_stuck_alerts(:before)")


class DeliveryError(Exception):
    """A send that failed and may work later; the job is tried again."""


@dataclass(frozen=True)
class Deps:
    """What sending needs from the process: the SMTP settings, the address policy for
    webhooks, the keyring for their secrets and the public URL for links."""

    smtp: mail.SmtpConfig | None
    net: NetPolicy
    keyring: Keyring | None
    public_url: str | None

    @classmethod
    def from_settings(cls, settings: Settings, *, net: NetPolicy, keyring: Keyring | None) -> Deps:
        return cls(
            smtp=mail.SmtpConfig.from_settings(settings),
            net=net,
            keyring=keyring,
            public_url=settings.public_url,
        )


@dataclass(frozen=True)
class Dispatch:
    """How a process gets notifications delivered: `inline` sends in this process what is due
    at once (single-process development and tests); without it the worker's job does."""

    inline: Deps | None = None


@dataclass(frozen=True)
class _Claimed:
    """A notification taken for sending, with everything the send needs."""

    content: messages.Content
    channel: str
    recipients: list[str]
    url: str | None
    secret: str | None


@dataclass(frozen=True)
class _Outcome:
    status: str  # sent, skipped or failed
    error: str | None = None
    retry: bool = False


def _now() -> datetime:
    return datetime.now(UTC)


def _items_kind(items: list[dict[str, Any]]) -> str | None:
    """The rule kind that matched these items: a score's crossing names its metric."""
    if not items:
        return None
    return "score" if "metric" in items[0] else "finding"


async def _claim(
    session: AsyncSession, notification_id: uuid.UUID, deps: Deps, now: datetime
) -> tuple[int, _Claimed | _Outcome] | None:
    """Mark the notification `sending` and read what it tells. None when there is nothing to
    do (gone, done, not due, or another worker has it). Otherwise the claim's number (the
    notification's count of attempts, which every claim raises) with what to send, or with
    the outcome when it cannot be sent."""
    notification = await session.scalar(
        select(AlertNotification).where(AlertNotification.id == notification_id).with_for_update()
    )
    if notification is None or notification.due_at > now:
        return None
    stuck = (
        notification.status == "sending"
        and notification.claimed_at is not None
        and notification.claimed_at <= now - STUCK_AFTER
    )
    if notification.status != "pending" and not stuck:
        return None
    rule = await session.get(AlertRule, notification.rule_id)
    workspace = await session.get(Workspace, notification.workspace_id)
    if rule is None or workspace is None:
        return None
    notification.status, notification.claimed_at = "sending", now
    notification.attempts += 1
    if _items_kind(notification.items) not in (None, rule.kind):
        # The rule's kind changed while this one was being sent or waited for a retry: its
        # matches are of the other kind and are never told as this one.
        return notification.attempts, _Outcome("skipped", rules.KIND_CHANGED)
    try:
        secret = rules.open_secret(rule, deps.keyring) if rule.channel == "webhook" else None
    except (CredentialsUnavailableError, CredentialsError) as exc:
        # Never sent unsigned to an endpoint that expects a signature.
        return notification.attempts, _Outcome(
            "failed", f"the signing secret cannot be read: {exc}"[:MAX_ERROR]
        )
    content = messages.Content(
        delivery=notification.id,
        event=notification.kind,
        rule_id=rule.id,
        rule_name=rule.name,
        rule_kind=rule.kind,
        condition=dict(rule.condition),
        workspace_id=workspace.id,
        workspace_name=workspace.name,
        n_matches=notification.n_matches,
        items=list(notification.items),
        sent_at=now,
        link=messages.link_for(deps.public_url, rule.kind),
    )
    return notification.attempts, _Claimed(content, rule.channel, list(rule.recipients), rule.url, secret)


async def _send_email(claimed: _Claimed, deps: Deps) -> _Outcome:
    if deps.smtp is None:
        return _Outcome("skipped", EMAIL_OFF)
    message = messages.email_message(claimed.content, sender=deps.smtp.sender, recipients=claimed.recipients)
    try:
        await mail.send(deps.smtp, message)
    except mail.MailError as exc:
        return _Outcome("failed", str(exc)[:MAX_ERROR], retry=True)
    return _Outcome("sent")


async def _send_webhook(claimed: _Claimed, deps: Deps) -> _Outcome:
    body = messages.webhook_body(claimed.content)
    headers = messages.webhook_headers(claimed.content, body, secret=claimed.secret, version=__version__)
    url = claimed.url or ""
    try:
        # Pinned to the checked address, without redirects (spec 021).
        client = await deps.net.http_client(url)
    except TargetRefusedError as exc:
        return _Outcome("failed", str(exc)[:MAX_ERROR])
    try:
        # Only the status is read: the body is never loaded, so a receiver cannot hold the
        # delivery or fill the memory by sending one without end.
        async with (
            client,
            client.stream("POST", url, content=body, headers=headers, timeout=WEBHOOK_TIMEOUT_S) as response,
        ):
            status_code = response.status_code
    except TargetRefusedError as exc:
        return _Outcome("failed", str(exc)[:MAX_ERROR])
    except httpx.HTTPError as exc:
        return _Outcome("failed", f"{type(exc).__name__}: {exc}"[:MAX_ERROR], retry=True)
    if 200 <= status_code < 300:
        return _Outcome("sent")
    # 4xx says the request is wrong as it is; anything else may pass.
    retry = status_code >= 500 or status_code in (408, 429)
    return _Outcome("failed", f"the webhook answered {status_code}", retry=retry)


async def _send(claimed: _Claimed, deps: Deps) -> _Outcome:
    """Send by the rule's channel. An error nothing here expects is the notification's
    outcome too: it is claimed, and nothing else would record what became of it."""
    try:
        if claimed.channel == "email":
            return await _send_email(claimed, deps)
        return await _send_webhook(claimed, deps)
    except Exception as exc:  # noqa: BLE001  (recorded on the notification and logged, not swallowed)
        log.exception("alert.delivery_error", notification_id=str(claimed.content.delivery))
        return _Outcome("failed", f"{type(exc).__name__}: {exc}"[:MAX_ERROR])


async def _record(
    factory: async_sessionmaker[AsyncSession],
    notification_id: uuid.UUID,
    outcome: _Outcome,
    *,
    claim: int,
    final: bool,
    at: datetime,
) -> str | None:
    """Store the outcome; a failure that may pass and has attempts left waits as `pending`.
    None when the claim is no longer this one's: the notification was taken again meanwhile
    (this send took longer than `STUCK_AFTER`), and the later claim's outcome stands."""
    again = outcome.status == "failed" and outcome.retry and not final
    status = "pending" if again else outcome.status
    async with factory() as session, session.begin():
        notification = await session.scalar(
            select(AlertNotification).where(AlertNotification.id == notification_id).with_for_update()
        )
        if notification is None:
            return None
        if notification.status != "sending" or notification.attempts != claim:
            log.warning("alert.delivery_superseded", notification_id=str(notification_id), claim=claim)
            return None
        notification.status, notification.error = status, outcome.error
        if status == "sent":
            notification.sent_at = at
    return status


async def deliver(
    factory: async_sessionmaker[AsyncSession],
    notification_id: uuid.UUID,
    deps: Deps,
    *,
    final_attempt: bool,
    now: datetime | None = None,
) -> str | None:
    """Send one notification when it is due and record the outcome: `sent`, `skipped` or
    `failed`; None when there was nothing to do. The send happens outside any transaction.

    Raises:
        DeliveryError: the send failed and may pass later (not on the final attempt).
    """
    now = now or _now()
    async with factory() as session, session.begin():
        claimed = await _claim(session, notification_id, deps, now)
    if claimed is None:
        return None
    claim, what = claimed
    outcome = what if isinstance(what, _Outcome) else await _send(what, deps)
    # A notification taken again after a lost worker starts a new job: its own count of
    # attempts ends the retries, whatever the job's says.
    final = final_attempt or claim >= ATTEMPTS
    status = await _record(factory, notification_id, outcome, claim=claim, final=final, at=now)
    if status is None:
        return None
    log.info("alert.delivery", notification_id=str(notification_id), status=status, error=outcome.error)
    if status == "pending":
        raise DeliveryError(outcome.error or "delivery failed")
    return status


async def requeue_stuck(factory: async_sessionmaker[AsyncSession], now: datetime | None = None) -> int:
    """Put the notifications left `sending` for longer than `STUCK_AFTER` back to `pending`,
    in every org, and queue their delivery: a worker that died after claiming one, or lost
    the database before recording its outcome, leaves no job behind. Returns how many."""
    now = now or _now()
    async with factory() as session, session.begin():
        rows = (await session.execute(REQUEUE_SQL, {"before": now - STUCK_AFTER})).all()
        for notification_id, org_id in rows:
            await evaluate.defer_job(session, notification_id, org_id, now)
    return len(rows)


async def deliver_inline(
    factory: async_sessionmaker[AsyncSession],
    notifications: list[AlertNotification],
    deps: Deps,
    now: datetime,
) -> None:
    """Send in this process what is due now, one attempt each; the rest waits for a worker."""
    for notification in notifications:
        if notification.due_at <= now:
            await deliver(factory, notification.id, deps, final_attempt=True, now=now)


async def after_run(
    factory: async_sessionmaker[AsyncSession], scope: Scope, run_id: uuid.UUID, dispatch: Dispatch | None
) -> None:
    """Evaluate the workspace's rules on a run that succeeded and get what matched delivered.
    Whatever goes wrong here is logged: alerting never fails a run."""
    if dispatch is None:
        return
    try:
        now = _now()
        async with factory() as session, session.begin():
            started = await evaluate.evaluate_run(
                session, scope, run_id, now=now, defer=dispatch.inline is None
            )
        if dispatch.inline is not None:
            await deliver_inline(factory, started, dispatch.inline, now)
    except Exception:  # noqa: BLE001  (the run is stored; an alerting error must not undo or fail it)
        log.exception("alert.evaluation_failed", run_id=str(run_id))
