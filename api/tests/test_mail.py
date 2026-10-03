"""The invitation email and the SMTP send (spec 014), against the in-process fake server."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from fake_smtp import Inbox
from tabayyun.mail import InvitationMail, MailError, SmtpConfig, invitation_message, send
from tabayyun.settings import Settings

MAIL = InvitationMail(
    to="ada@example.org",
    org_name="Acme Energy",
    inviter="Alice",
    role="an editor",
    workspace="Plant North",
    expires_at=datetime(2026, 10, 17, 12, tzinfo=UTC),
    login_url="https://tabayyun.example/login",
)


def _config(inbox: Inbox) -> SmtpConfig:
    return SmtpConfig(
        host=inbox.host,
        port=inbox.port,
        starttls=False,
        username=None,
        password=None,
        sender="Tabayyun <t@example.org>",
    )


def test_message_says_who_where_and_how():
    message = invitation_message(MAIL, "Tabayyun <t@example.org>")
    body = message.get_content()
    assert message["To"] == "ada@example.org"
    assert message["Subject"] == "Alice invited you to Acme Energy on Tabayyun"
    assert message["Message-ID"].endswith("@example.org>")
    assert "as an editor, with access to the workspace “Plant North”" in body
    assert "Sign in at https://tabayyun.example/login" in body
    assert "17 October 2026" in body
    assert "token" not in body.lower()


def test_message_without_a_public_url():
    body = invitation_message(
        InvitationMail(**{**MAIL.__dict__, "login_url": None, "workspace": None}), "t@example.org"
    ).get_content()
    assert "Open Tabayyun and sign in" in body and "workspace" not in body


def test_config_is_none_without_a_host():
    assert SmtpConfig.from_settings(Settings()) is None
    config = SmtpConfig.from_settings(
        Settings(smtp_host="relay", smtp_from="t@example.org", smtp_password="p" * 20)
    )
    assert config is not None and (config.host, config.port, config.password) == ("relay", 587, "p" * 20)


async def test_send_delivers(smtp):
    await send(_config(smtp), invitation_message(MAIL, "Tabayyun <t@example.org>"))
    [received] = smtp.messages
    assert received["To"] == "ada@example.org"


async def test_a_refused_recipient_is_a_mail_error(smtp):
    smtp.refuse.add("ada@example.org")
    with pytest.raises(MailError, match="^550 "):
        await send(_config(smtp), invitation_message(MAIL, "t@example.org"))


async def test_an_unreachable_server_is_a_mail_error(smtp):
    config = SmtpConfig(
        host="127.0.0.1", port=1, starttls=False, username=None, password="secret-pw", sender="t@x.org"
    )
    with pytest.raises(MailError) as err:
        await send(config, invitation_message(MAIL, "t@example.org"))
    assert "secret-pw" not in str(err.value)
