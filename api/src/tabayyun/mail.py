"""Outgoing email (spec 014): the invitation message and one SMTP send.

The worker sends; the API only queues. Errors keep the server's reply code and text and never
the credentials, so they can be stored on the invitation and logged.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr

import aiosmtplib

from tabayyun.settings import Settings

SEND_TIMEOUT_S = 30
MAX_ERROR = 300


class MailError(Exception):
    """A message the SMTP server refused or could not take; the text is safe to store."""


@dataclass(frozen=True)
class SmtpConfig:
    """Where and how to send; built from the settings, None when email is off."""

    host: str
    port: int
    starttls: bool
    username: str | None
    password: str | None
    sender: str

    @classmethod
    def from_settings(cls, settings: Settings) -> SmtpConfig | None:
        if not settings.mail_enabled or not settings.smtp_host or not settings.smtp_from:
            return None
        return cls(
            host=settings.smtp_host.strip(),
            port=settings.smtp_port,
            starttls=settings.smtp_starttls,
            username=settings.smtp_username or None,
            password=settings.smtp_password.get_secret_value() if settings.smtp_password else None,
            sender=settings.smtp_from,
        )


@dataclass(frozen=True)
class InvitationMail:
    """What the invitation email says."""

    to: str
    org_name: str
    inviter: str
    role: str
    workspace: str | None
    expires_at: datetime
    login_url: str | None


def invitation_message(mail: InvitationMail, sender: str) -> EmailMessage:
    """A plain-text invitation. It carries no secret: joining needs a sign-in with this address."""
    grant = f"as {mail.role}" + (
        f", with access to the workspace “{mail.workspace}”" if mail.workspace else ""
    )
    where = f"Sign in at {mail.login_url}" if mail.login_url else "Open Tabayyun and sign in"
    sign_in = f"{where} with Google, GitHub or a passkey, using this email address ({mail.to})."
    body = "\n".join(
        [
            "Hello,",
            "",
            f"{mail.inviter} invited you to join {mail.org_name} on Tabayyun {grant}.",
            "",
            sign_in,
            "You become a member the moment you sign in; there is nothing else to click.",
            "",
            f"The invitation is open until {mail.expires_at:%d %B %Y} (UTC).",
            "If you did not expect it, you can ignore this email.",
            "",
            "— Tabayyun",
        ]
    )
    message = EmailMessage()
    message["From"] = sender
    message["To"] = mail.to
    message["Subject"] = f"{mail.inviter} invited you to {mail.org_name} on Tabayyun"
    message["Date"] = formatdate(localtime=False)
    domain = parseaddr(sender)[1].rpartition("@")[2] or None
    message["Message-ID"] = make_msgid(domain=domain)
    message["Auto-Submitted"] = "auto-generated"
    message.set_content(body)
    return message


async def send(config: SmtpConfig, message: EmailMessage) -> None:
    """Send one message; MailError with the server's reply on any failure."""
    try:
        await aiosmtplib.send(
            message,
            hostname=config.host,
            port=config.port,
            start_tls=config.starttls,
            username=config.username,
            password=config.password,
            timeout=SEND_TIMEOUT_S,
        )
    except aiosmtplib.SMTPRecipientsRefused as exc:
        first = exc.recipients[0]
        raise MailError(f"{first.code} {first.message}"[:MAX_ERROR]) from exc
    except aiosmtplib.SMTPResponseException as exc:
        raise MailError(f"{exc.code} {exc.message}"[:MAX_ERROR]) from exc
    except (aiosmtplib.SMTPException, OSError, TimeoutError) as exc:
        raise MailError(f"{type(exc).__name__}: {exc}"[:MAX_ERROR]) from exc
