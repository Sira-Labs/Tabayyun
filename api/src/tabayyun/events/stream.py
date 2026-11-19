"""A subscription as `text/event-stream` bytes (spec 030)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable

import structlog
from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from tabayyun.events import Event
from tabayyun.events.broker import Subscription

log = structlog.get_logger()

# How long the browser waits before it connects again, in milliseconds.
RETRY_MS = 5000
KEEP_ALIVE = b": keep-alive\n\n"
# How long past its maximum age a stream may take to end by itself before it is cut off.
GRACE_S = 5.0


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


class EventStreamResponse(StreamingResponse):
    """A `text/event-stream` response with a hard end. The frames' own age check runs only
    when the generator is resumed, and a client that keeps the connection but stops reading
    blocks the send before that: `deadline_s` after it started the response is cut off
    whatever it waits for. `on_close` runs however the response ends."""

    def __init__(
        self,
        frames: AsyncIterator[bytes],
        *,
        deadline_s: float,
        on_close: Callable[[], None],
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(frames, media_type="text/event-stream", headers=headers)
        self._deadline_s = deadline_s
        self._on_close = on_close

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            async with asyncio.timeout(self._deadline_s):
                await super().__call__(scope, receive, send)
        except TimeoutError:
            log.warning("events.stream_cut_off", after_s=self._deadline_s)
        finally:
            self._on_close()
