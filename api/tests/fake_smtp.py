"""An in-process SMTP server (aiosmtpd) for the mail tests: never a real host."""

from __future__ import annotations

import socket
from collections.abc import Iterator
from dataclasses import dataclass, field
from email import message_from_bytes
from email.message import Message
from typing import Any

import pytest
from aiosmtpd.controller import Controller


@dataclass
class Inbox:
    """Messages the fake server accepted, and recipients it refuses."""

    host: str
    port: int
    messages: list[Message] = field(default_factory=list)
    refuse: set[str] = field(default_factory=set)

    async def handle_RCPT(self, server: Any, session: Any, envelope: Any, address: str, options: Any) -> str:  # noqa: N802
        if address.lower() in self.refuse:
            return "550 5.1.1 mailbox unavailable"
        envelope.rcpt_tos.append(address)
        return "250 OK"

    async def handle_DATA(self, server: Any, session: Any, envelope: Any) -> str:  # noqa: N802
        self.messages.append(message_from_bytes(envelope.content))
        return "250 Message accepted"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def smtp() -> Iterator[Inbox]:
    inbox = Inbox(host="127.0.0.1", port=_free_port())
    controller = Controller(inbox, hostname=inbox.host, port=inbox.port)
    controller.start()
    yield inbox
    controller.stop()
