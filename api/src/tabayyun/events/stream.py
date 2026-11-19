"""A subscription as `text/event-stream` bytes (spec 030)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from tabayyun.events import Event
from tabayyun.events.broker import Subscription

# How long the browser waits before it connects again, in milliseconds.
RETRY_MS = 5000
KEEP_ALIVE = b": keep-alive\n\n"


def frame(event: Event) -> bytes:
    """One event as an SSE frame: its type as the event name, its public JSON as the data."""
    data = json.dumps(event.public(), separators=(",", ":"))
    return f"event: {event.type}\ndata: {data}\n\n".encode()


async def stream(subscription: Subscription, *, heartbeat_s: float, max_age_s: float) -> AsyncIterator[bytes]:
    """The frames of `subscription` until it is `max_age_s` old: the retry hint first, then
    each event as it comes, and a comment after `heartbeat_s` of silence so proxies keep the
    connection and a client that went away is noticed."""
    loop = asyncio.get_running_loop()
    ends_at = loop.time() + max_age_s
    yield f"retry: {RETRY_MS}\n\n: connected\n\n".encode()
    while (left := ends_at - loop.time()) > 0:
        try:
            event = await asyncio.wait_for(subscription.queue.get(), timeout=min(heartbeat_s, left))
        except TimeoutError:
            if ends_at - loop.time() > 0:
                yield KEEP_ALIVE
            continue
        yield frame(event)
