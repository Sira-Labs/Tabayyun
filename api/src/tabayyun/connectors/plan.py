"""Fetch planning (spec 021): which calls fill the gaps, and how fast they may start. Pure.

Each gap is cut into spans of at most `max_span_ns` from its start; points whose spans have
the same bounds share calls of at most `max_points`. Calls run in time order. A pacer spaces
call starts by `1 / requests_per_second` (no bursts).
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass

from tabayyun.connectors.base import Limits, PointRef


@dataclass(frozen=True)
class Call:
    """One connector call: these points over `[start_ns, end_ns)`."""

    start_ns: int
    end_ns: int
    points: tuple[PointRef, ...]


def spans(start_ns: int, end_ns: int, max_span_ns: int) -> list[tuple[int, int]]:
    """`[start_ns, end_ns)` cut into consecutive spans of at most `max_span_ns`."""
    out: list[tuple[int, int]] = []
    lo = start_ns
    while lo < end_ns:
        hi = min(end_ns, lo + max_span_ns)
        out.append((lo, hi))
        lo = hi
    return out


def plan_calls(gaps: Mapping[PointRef, Sequence[tuple[int, int]]], limits: Limits) -> list[Call]:
    """The calls that fetch every gap: spans per gap, grouped by bounds, chunked by points."""
    by_span: dict[tuple[int, int], list[PointRef]] = defaultdict(list)
    for point, ranges in gaps.items():
        for lo, hi in ranges:
            for span in spans(lo, hi, limits.max_span_ns):
                by_span[span].append(point)
    calls: list[Call] = []
    for (lo, hi), points in sorted(by_span.items()):
        ordered = sorted(points, key=lambda p: (p.external_id, str(p.series_id)))
        for i in range(0, len(ordered), limits.max_points):
            calls.append(Call(lo, hi, tuple(ordered[i : i + limits.max_points])))
    return calls


def settled_end(end_ns: int, now_ns: int, settle_ns: int) -> int:
    """Where a span counts as covered: its end, but never past `now - settle`."""
    return min(end_ns, now_ns - settle_ns)


class Pacer:
    """Spaces call starts by `1 / rate` seconds; the first call starts at once."""

    def __init__(
        self,
        rate: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._interval = 1.0 / rate
        self._clock = clock
        self._sleep = sleep
        self._next: float | None = None

    async def wait(self) -> None:
        """Return when the next call may start."""
        now = self._clock()
        if self._next is not None and now < self._next:
            await self._sleep(self._next - now)
            now = self._next
        self._next = now + self._interval
