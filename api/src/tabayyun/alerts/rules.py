"""Alert rules (spec 031): what a rule may be, and storing it."""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tabayyun import core
from tabayyun.alerts.matching import OVERALL
from tabayyun.authz.scope import Scope
from tabayyun.db.models import ALERT_CHANNELS, ALERT_KINDS, AlertNotification, AlertRule
from tabayyun.secrets import Keyring, Sealed, require
from tabayyun.services.findings import DIMENSIONS, SEVERITIES

MAX_NAME = 120
MAX_RECIPIENTS = 10
MAX_EMAIL = 254
MAX_URL = 2000
MAX_ASSET_PREFIX = 200
MAX_THROTTLE_MINUTES = 1440
DEFAULT_THROTTLE_MINUTES = 60
SECRET_CHARS = (8, 200)
MAX_HISTORY = 50
_EMAIL = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")
_FINDING_KEYS = {"min_severity", "check_ids", "dimensions", "asset_prefix"}
_SCORE_KEYS = {"metric", "below"}
# Not given in a request: the field keeps its value.
UNSET: Any = object()


class RuleError(ValueError):
    """A rule that is wrong in itself: one entry per field, for a 422."""

    def __init__(self, errors: list[dict[str, str]]) -> None:
        super().__init__("; ".join(f"{e['field']}: {e['message']}" for e in errors))
        self.errors = errors


class RuleNameTakenError(Exception):
    """Another rule of the workspace has the name."""


@dataclass
class RuleData:
    """The fields of a rule as a request gives them (already merged with the stored rule
    on a change)."""

    name: str
    kind: str
    condition: dict[str, Any]
    channel: str
    recipients: list[str] = field(default_factory=list)
    url: str | None = None
    throttle_minutes: int = DEFAULT_THROTTLE_MINUTES
    enabled: bool = True


def _strings(value: Any) -> list[str] | None:
    """`value` when it is a list of strings."""
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return list(dict.fromkeys(v.strip() for v in value if v.strip()))
    return None


def _finding_condition(raw: dict[str, Any], problem: Any) -> dict[str, Any]:
    for key in sorted(set(raw) - _FINDING_KEYS):
        problem(f"condition.{key}", "unknown field")
    severity = raw.get("min_severity")
    if severity not in SEVERITIES:
        problem("condition.min_severity", f"must be one of {', '.join(SEVERITIES)}")
    checks = _strings(raw.get("check_ids", []))
    if checks is None:
        problem("condition.check_ids", "must be a list of check ids")
    else:
        known = {check["id"] for check in core.check_catalogue()}
        unknown = [c for c in checks if c not in known]
        if unknown:
            problem("condition.check_ids", f"unknown check: {', '.join(unknown)}")
    dimensions = _strings(raw.get("dimensions", []))
    if dimensions is None or any(d not in DIMENSIONS for d in dimensions):
        problem("condition.dimensions", f"must be a list of {', '.join(DIMENSIONS)}")
    prefix = raw.get("asset_prefix")
    if prefix is not None and (not isinstance(prefix, str) or len(prefix) > MAX_ASSET_PREFIX):
        problem("condition.asset_prefix", f"must be text of at most {MAX_ASSET_PREFIX} characters")
        prefix = None
    return {
        "min_severity": severity,
        "check_ids": checks or [],
        "dimensions": dimensions or [],
        "asset_prefix": (prefix or "").strip() or None,
    }


def _score_condition(raw: dict[str, Any], problem: Any) -> dict[str, Any]:
    for key in sorted(set(raw) - _SCORE_KEYS):
        problem(f"condition.{key}", "unknown field")
    metric = raw.get("metric", OVERALL)
    if metric != OVERALL and metric not in DIMENSIONS:
        problem("condition.metric", f"must be {OVERALL} or one of {', '.join(DIMENSIONS)}")
    below = raw.get("below")
    if isinstance(below, bool) or not isinstance(below, int | float) or not 0 <= below <= 100:
        problem("condition.below", "must be a number from 0 to 100")
        below = 0
    return {"metric": metric, "below": float(below)}


def _recipients(raw: Any, problem: Any) -> list[str]:
    addresses = _strings(raw)
    if addresses is None:
        problem("recipients", "must be a list of email addresses")
        return []
    addresses = list(dict.fromkeys(a.lower() for a in addresses))
    if not 1 <= len(addresses) <= MAX_RECIPIENTS:
        problem("recipients", f"give 1 to {MAX_RECIPIENTS} email addresses")
    bad = [a for a in addresses if len(a) > MAX_EMAIL or not _EMAIL.match(a)]
    if bad:
        problem("recipients", f"not an email address: {', '.join(bad)}")
    return addresses


def _url(raw: Any, problem: Any) -> str | None:
    if not isinstance(raw, str) or not raw.strip():
        problem("url", "a webhook needs a URL")
        return None
    url = raw.strip()
    parts = urlsplit(url)
    if len(url) > MAX_URL or parts.scheme != "https" or not parts.hostname:
        problem("url", "must be an https URL")
    elif parts.username or parts.password:
        problem("url", "must not carry a user name or a password")
    return url


def validate(data: RuleData) -> RuleData:
    """The rule in its stored form.

    Raises:
        RuleError: with one entry per wrong field.
    """
    errors: list[dict[str, str]] = []

    def problem(where: str, message: str) -> None:
        errors.append({"field": where, "message": message})

    name = data.name.strip() if isinstance(data.name, str) else ""
    if not 1 <= len(name) <= MAX_NAME:
        problem("name", f"must have 1 to {MAX_NAME} characters")
    condition: dict[str, Any] = {}
    if data.kind not in ALERT_KINDS:
        problem("kind", f"must be one of {', '.join(ALERT_KINDS)}")
    elif not isinstance(data.condition, dict):
        problem("condition", "must be an object")
    elif data.kind == "finding":
        condition = _finding_condition(data.condition, problem)
    else:
        condition = _score_condition(data.condition, problem)
    recipients: list[str] = []
    url: str | None = None
    if data.channel not in ALERT_CHANNELS:
        problem("channel", f"must be one of {', '.join(ALERT_CHANNELS)}")
    elif data.channel == "email":
        recipients = _recipients(data.recipients, problem)
        if data.url:
            problem("url", "an email rule has no URL")
    else:
        url = _url(data.url, problem)
        if data.recipients:
            problem("recipients", "a webhook rule has no recipients")
    throttle = data.throttle_minutes
    if (
        isinstance(throttle, bool)
        or not isinstance(throttle, int)
        or not 0 <= throttle <= MAX_THROTTLE_MINUTES
    ):
        problem("throttle_minutes", f"must be a whole number from 0 to {MAX_THROTTLE_MINUTES}")
    if errors:
        raise RuleError(errors)
    return RuleData(
        name=name,
        kind=data.kind,
        condition=condition,
        channel=data.channel,
        recipients=recipients,
        url=url,
        throttle_minutes=throttle,
        enabled=bool(data.enabled),
    )


def validate_secret(secret: str, channel: str) -> str:
    """A signing secret as it is stored.

    Raises:
        RuleError: it is not text of the allowed length, or the rule is not a webhook.
    """
    low, high = SECRET_CHARS
    if channel != "webhook":
        raise RuleError([{"field": "secret", "message": "only a webhook rule has a signing secret"}])
    if not isinstance(secret, str) or not low <= len(secret) <= high:
        raise RuleError([{"field": "secret", "message": f"must have {low} to {high} characters"}])
    return secret


def seal_secret(rule: AlertRule, keyring: Keyring | None, secret: str | None) -> None:
    """Store (or with None remove) the rule's signing secret.

    Raises:
        CredentialsUnavailableError: a secret is given and no master key is set.
    """
    if secret is None:
        rule.secret_key_id = rule.secret_nonce = rule.secret_ciphertext = None
        return
    sealed = require(keyring).seal(rule.org_id, rule.id, {"secret": secret})
    rule.secret_key_id, rule.secret_nonce, rule.secret_ciphertext = (
        sealed.key_id,
        sealed.nonce,
        sealed.ciphertext,
    )


def open_secret(rule: AlertRule, keyring: Keyring | None) -> str | None:
    """The rule's signing secret; None when it has none.

    Raises:
        CredentialsUnavailableError: it has one and no master key is set.
        CredentialsError: it does not decrypt.
    """
    if rule.secret_key_id is None or rule.secret_nonce is None or rule.secret_ciphertext is None:
        return None
    sealed = Sealed(rule.secret_key_id, rule.secret_nonce, rule.secret_ciphertext)
    return str(require(keyring).open(rule.org_id, rule.id, sealed)["secret"])


async def rotate_secrets(session: AsyncSession, keyring: Keyring) -> int:
    """Re-encrypt every rule secret not under the current key; how many were re-encrypted.
    Runs as the table owner, like `tabayyun.secrets.rotate`."""
    rows = (
        await session.scalars(
            select(AlertRule)
            .where(AlertRule.secret_key_id.is_not(None), AlertRule.secret_key_id != keyring.current_id)
            .with_for_update()
        )
    ).all()
    for rule in rows:
        seal_secret(rule, keyring, open_secret(rule, keyring))
    return len(rows)


async def _flush_or_taken(session: AsyncSession) -> None:
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError as exc:
        if "uq_alert_rules_workspace_id_name" in str(exc.orig):
            raise RuleNameTakenError from exc
        raise


def apply(rule: AlertRule, data: RuleData) -> None:
    """Copy validated fields onto the rule; a rule that stops being a webhook loses its secret."""
    rule.name, rule.enabled, rule.kind, rule.condition = data.name, data.enabled, data.kind, data.condition
    rule.channel, rule.recipients, rule.url = data.channel, data.recipients, data.url
    rule.throttle_minutes = data.throttle_minutes
    if data.channel != "webhook":
        seal_secret(rule, None, None)


async def create_rule(
    session: AsyncSession,
    scope: Scope,
    user_id: uuid.UUID,
    data: RuleData,
    *,
    secret: str | None,
    keyring: Keyring | None,
) -> AlertRule:
    """Store a new rule.

    Raises:
        RuleError, RuleNameTakenError, CredentialsUnavailableError.
    """
    data = validate(data)
    if secret is not None:
        secret = validate_secret(secret, data.channel)
    rule = AlertRule(
        id=uuid.uuid4(), org_id=scope.org_id, workspace_id=scope.workspace_id, created_by=user_id
    )
    apply(rule, data)
    seal_secret(rule, keyring, secret)
    session.add(rule)
    await _flush_or_taken(session)
    await session.refresh(rule)
    return rule


def data_of(rule: AlertRule) -> RuleData:
    """The rule's stored fields."""
    return RuleData(
        name=rule.name,
        kind=rule.kind,
        condition=dict(rule.condition),
        channel=rule.channel,
        recipients=list(rule.recipients),
        url=rule.url,
        throttle_minutes=rule.throttle_minutes,
        enabled=rule.enabled,
    )


async def change_rule(
    session: AsyncSession, rule: AlertRule, data: RuleData, *, secret: Any, keyring: Keyring | None
) -> None:
    """Replace the rule's fields with `data`; `secret` is `UNSET` (keep), None (remove) or text.

    Raises:
        RuleError, RuleNameTakenError, CredentialsUnavailableError.
    """
    data = validate(data)
    if secret is not UNSET and secret is not None:
        secret = validate_secret(secret, data.channel)
    apply(rule, data)
    if secret is not UNSET:
        seal_secret(rule, keyring, secret)
    rule.updated_at = func.now()
    await _flush_or_taken(session)
    await session.refresh(rule)


async def get_rule(session: AsyncSession, scope: Scope, rule_id: uuid.UUID) -> AlertRule | None:
    """The rule when it belongs to the scope's workspace."""
    return await session.scalar(
        select(AlertRule).where(AlertRule.id == rule_id, AlertRule.workspace_id == scope.workspace_id)
    )


async def list_rules(session: AsyncSession, scope: Scope) -> list[AlertRule]:
    """The workspace's rules by name."""
    rows = await session.scalars(
        select(AlertRule)
        .where(AlertRule.workspace_id == scope.workspace_id)
        .order_by(func.lower(AlertRule.name), AlertRule.id)
    )
    return list(rows)


async def last_notifications(
    session: AsyncSession, rule_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, AlertNotification]:
    """Each rule's newest notification."""
    if not rule_ids:
        return {}
    rows = await session.scalars(
        select(AlertNotification)
        .where(AlertNotification.rule_id.in_(rule_ids))
        .distinct(AlertNotification.rule_id)
        .order_by(AlertNotification.rule_id, AlertNotification.created_at.desc(), AlertNotification.id)
    )
    return {row.rule_id: row for row in rows}


async def list_notifications(
    session: AsyncSession, rule: AlertRule, limit: int = MAX_HISTORY
) -> list[AlertNotification]:
    """The rule's notifications, newest first."""
    rows = await session.scalars(
        select(AlertNotification)
        .where(AlertNotification.rule_id == rule.id)
        .order_by(AlertNotification.created_at.desc(), AlertNotification.id)
        .limit(min(limit, MAX_HISTORY))
    )
    return list(rows)


def url_host(rule: AlertRule) -> str | None:
    """The host of a webhook rule's URL: what is shown to everyone and written to the audit log."""
    return urlsplit(rule.url).hostname if rule.url else None


def notification_to_dict(notification: AlertNotification) -> dict[str, Any]:
    """A notification as the API returns it."""
    return {
        "id": str(notification.id),
        "kind": notification.kind,
        "status": notification.status,
        "due_at": notification.due_at,
        "sent_at": notification.sent_at,
        "n_matches": notification.n_matches,
        "attempts": notification.attempts,
        "error": notification.error,
        "created_at": notification.created_at,
    }


def rule_to_dict(rule: AlertRule, last: AlertNotification | None, *, with_url: bool) -> dict[str, Any]:
    """A rule as the API returns it; the full webhook URL only `with_url` (admins)."""
    return {
        "id": str(rule.id),
        "name": rule.name,
        "enabled": rule.enabled,
        "kind": rule.kind,
        "condition": rule.condition,
        "channel": rule.channel,
        "recipients": rule.recipients,
        "url": rule.url if with_url else None,
        "url_host": url_host(rule),
        "has_secret": rule.secret_key_id is not None,
        "throttle_minutes": rule.throttle_minutes,
        "created_at": rule.created_at,
        "updated_at": rule.updated_at,
        "last_notification": None if last is None else notification_to_dict(last),
    }


def audit_details(rule: AlertRule) -> dict[str, Any]:
    """What the audit log keeps of a rule: never the secret or the full URL."""
    return {
        "name": rule.name,
        "enabled": rule.enabled,
        "kind": rule.kind,
        "condition": rule.condition,
        "channel": rule.channel,
        "recipients": rule.recipients,
        "url_host": url_host(rule),
        "has_secret": rule.secret_key_id is not None,
        "throttle_minutes": rule.throttle_minutes,
    }
