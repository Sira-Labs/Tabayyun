"""What a notification says (spec 031): the email and the webhook request. No I/O."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr
from typing import Any

# Matches an email lists; a webhook carries all that the notification kept.
EMAIL_ITEMS = 20
SIGNATURE_HEADER = "X-Tabayyun-Signature"


@dataclass(frozen=True)
class Content:
    """Everything a notification tells, read from the database before it is sent."""

    delivery: uuid.UUID
    event: str
    rule_id: uuid.UUID
    rule_name: str
    rule_kind: str
    condition: dict[str, Any]
    workspace_id: uuid.UUID
    workspace_name: str
    n_matches: int
    items: list[dict[str, Any]]
    sent_at: datetime
    link: str | None


def link_for(public_url: str | None, rule_kind: str) -> str | None:
    """Where a notification points: the findings page, or the series for a score rule."""
    if not public_url:
        return None
    return f"{public_url.rstrip('/')}/{'findings' if rule_kind == 'finding' else 'series'}"


def headline(content: Content) -> str:
    """`3 new findings of severity high or higher` or `2 series below 70 (overall)`."""
    n = content.n_matches
    if content.event == "test":
        return "test notification"
    if content.rule_kind == "finding":
        severity = content.condition.get("min_severity", "low")
        level = severity if severity == "critical" else f"{severity} or higher"
        return f"{n} new {'finding' if n == 1 else 'findings'} of severity {level}"
    below = content.condition.get("below")
    metric = content.condition.get("metric", "overall")
    return f"{n} series below {below:g} ({metric})"


def _finding_line(item: dict[str, Any]) -> str:
    where = item.get("series_name") or item.get("series_id")
    asset = f" ({item['asset_path']})" if item.get("asset_path") else ""
    return (
        f"- [{item.get('severity')}] {where}{asset}: {item.get('check_id')}, "
        f"{item.get('window_start')} to {item.get('window_end')}\n  {item.get('summary')}"
    )


def _score_line(item: dict[str, Any]) -> str:
    where = item.get("series_name") or item.get("series_id")
    asset = f" ({item['asset_path']})" if item.get("asset_path") else ""
    before = "no score before" if item.get("previous") is None else f"was {item['previous']:.1f}"
    return f"- {where}{asset}: {item.get('metric')} {item.get('score'):.1f}, {before}"


def email_message(content: Content, *, sender: str, recipients: list[str]) -> EmailMessage:
    """A plain-text email with the first matches and, when known, a link to the page."""
    line = _finding_line if content.rule_kind == "finding" else _score_line
    shown = content.items[:EMAIL_ITEMS]
    lines = [
        f"Rule: {content.rule_name}",
        f"Workspace: {content.workspace_name}",
        "",
        f"{headline(content).capitalize()}.",
    ]
    if content.event == "test":
        lines += ["This is a test: nothing was found. A real notification looks like this:"]
    lines += ["", *(line(item) for item in shown)]
    if content.n_matches > len(shown):
        lines += ["", f"… and {content.n_matches - len(shown)} more."]
    if content.link:
        lines += ["", f"Open: {content.link}"]
    lines += [
        "",
        "You get this email because an alert rule of this workspace names your address.",
        "— Tabayyun",
    ]
    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message["Subject"] = f"[Tabayyun] {content.rule_name}: {headline(content)}"
    message["Date"] = formatdate(localtime=False)
    message["Message-ID"] = make_msgid(domain=parseaddr(sender)[1].rpartition("@")[2] or None)
    message["Auto-Submitted"] = "auto-generated"
    message.set_content("\n".join(lines))
    return message


def webhook_body(content: Content) -> bytes:
    """The JSON a webhook receives."""
    payload = {
        "event": content.event,
        "delivery": str(content.delivery),
        "sent_at": content.sent_at.isoformat(),
        "rule": {"id": str(content.rule_id), "name": content.rule_name, "kind": content.rule_kind},
        "workspace": {"id": str(content.workspace_id), "name": content.workspace_name},
        "n_matches": content.n_matches,
        "truncated": content.n_matches > len(content.items),
        "matches": content.items,
        "url": content.link,
    }
    return json.dumps(payload, separators=(",", ":")).encode()


def sign(secret: str, body: bytes) -> str:
    """The value of the signature header: `sha256=<hex HMAC-SHA256 of the body>`."""
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def webhook_headers(content: Content, body: bytes, *, secret: str | None, version: str) -> dict[str, str]:
    """The headers of a webhook request; the signature only with a secret."""
    headers = {
        "Content-Type": "application/json",
        "User-Agent": f"Tabayyun/{version}",
        "X-Tabayyun-Event": content.event,
        "X-Tabayyun-Delivery": str(content.delivery),
    }
    if secret is not None:
        headers[SIGNATURE_HEADER] = sign(secret, body)
    return headers
