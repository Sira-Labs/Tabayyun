"""The `synthetic` connector (spec 021): deterministic series without a system behind them.

Each configured point is a sine wave plus hashed noise, sampled on a fixed grid of
`interval_s`. Every value is a pure function of `(seed, external_id, ts)`, so fetching a
window twice, or in different pieces, gives the same rows. Optional faults:

- `spikes`: every 997th sample of the grid is eight amplitudes high;
- `flatline`: 02:00 to 03:00 UTC each day holds the 02:00 value.

It needs no credentials or network: the framework's end-to-end fixture and a live, polling
source for demos on staging.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import AsyncIterator, Sequence
from typing import Literal

import pyarrow as pa
import pyarrow.compute as pc
from pydantic import BaseModel, ConfigDict, Field, field_validator

from tabayyun.connectors.base import (
    BATCH_SCHEMA,
    SECOND_NS,
    Connector,
    ConnectorConfig,
    FetchedBatch,
    PointRef,
    RemotePoint,
)

DAY_NS = 86_400 * SECOND_NS
HOUR_NS = 3_600 * SECOND_NS
SPIKE_EVERY = 997
SPIKE_AMPLITUDES = 8.0
# A fetch call yields at most this many rows per batch, so a long span never builds one huge table.
ROWS_PER_BATCH = 100_000
MASK64 = (1 << 64) - 1


class SyntheticPoint(BaseModel):
    """One generated point."""

    model_config = ConfigDict(extra="forbid")

    external_id: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._:/-]+$")
    name: str | None = Field(default=None, max_length=200)
    unit: str | None = Field(default=None, max_length=50)
    base: float = 0.0
    amplitude: float = Field(default=1.0, ge=0, le=1e9)
    period_s: int = Field(default=86_400, ge=60, le=366 * 86_400)
    noise: float = Field(default=0.1, ge=0, le=1e9)
    faults: list[Literal["spikes", "flatline"]] = Field(default_factory=list, max_length=2)


class SyntheticConfig(ConnectorConfig):
    """`interval_s`, `seed` and up to 50 points with distinct ids."""

    interval_s: int = Field(default=60, ge=1, le=86_400)
    seed: int = Field(default=0, ge=0, le=2**63 - 1)
    points: list[SyntheticPoint] = Field(default_factory=list, max_length=50)

    @field_validator("points")
    @classmethod
    def _distinct(cls, points: list[SyntheticPoint]) -> list[SyntheticPoint]:
        ids = [p.external_id for p in points]
        if len(ids) != len(set(ids)):
            raise ValueError("external_id must be unique")
        return points


def _splitmix64(x: int) -> int:
    """One splitmix64 step (scalar), for the per-point key."""
    x = (x + 0x9E3779B97F4A7C15) & MASK64
    x = ((x ^ (x >> 30)) * 0xBF58476D1CE4E5B9) & MASK64
    x = ((x ^ (x >> 27)) * 0x94D049BB133111EB) & MASK64
    return x ^ (x >> 31)


def _point_key(seed: int, external_id: str) -> int:
    digest = hashlib.blake2b(external_id.encode(), digest_size=8).digest()
    return _splitmix64(seed ^ int.from_bytes(digest, "big"))


def _noise(ts: pa.Array, key: int) -> pa.Array:
    """Uniform noise in [-1, 1) per timestamp: splitmix64 of `ts ^ key`, vectorised (uint64 wraps)."""
    u64 = pa.uint64()
    x = pc.bit_wise_xor(pc.cast(ts, u64, safe=False), pa.scalar(key, u64))
    x = pc.add(x, pa.scalar(0x9E3779B97F4A7C15, u64))
    x = pc.multiply(
        pc.bit_wise_xor(x, pc.shift_right(x, pa.scalar(30, u64))), pa.scalar(0xBF58476D1CE4E5B9, u64)
    )
    x = pc.multiply(
        pc.bit_wise_xor(x, pc.shift_right(x, pa.scalar(27, u64))), pa.scalar(0x94D049BB133111EB, u64)
    )
    x = pc.bit_wise_xor(x, pc.shift_right(x, pa.scalar(31, u64)))
    unit = pc.multiply(pc.cast(pc.shift_right(x, pa.scalar(11, u64)), pa.float64()), 2.0**-53)
    return pc.subtract(pc.multiply(unit, 2.0), 1.0)


def _mod(a: pa.Array, d: int) -> pa.Array:
    """Floor modulo of an int64 array (pyarrow has none): in `[0, d)` also before 1970."""
    r = pc.subtract(a, pc.multiply(pc.divide(a, d), d))
    return pc.if_else(pc.less(r, 0), pc.add(r, d), r)


def values(point: SyntheticPoint, seed: int, interval_ns: int, ts: pa.Array) -> pa.Array:
    """The point's values at grid timestamps `ts` (int64 ns)."""
    at = ts
    if "flatline" in point.faults:
        into_day = _mod(ts, DAY_NS)
        flat = pc.and_(pc.greater_equal(into_day, 2 * HOUR_NS), pc.less(into_day, 3 * HOUR_NS))
        at = pc.if_else(flat, pc.add(pc.subtract(ts, into_day), 2 * HOUR_NS), ts)
    period_ns = point.period_s * SECOND_NS
    # The phase within the period keeps full precision where seconds since 1970 as a float would not.
    phase = pc.divide(pc.cast(_mod(at, period_ns), pa.float64(), safe=False), float(period_ns))
    wave = pc.sin(pc.multiply(phase, 2 * math.pi))
    out = pc.add(
        pc.add(pa.scalar(point.base), pc.multiply(wave, point.amplitude)),
        pc.multiply(_noise(at, _point_key(seed, point.external_id)), point.noise),
    )
    if "spikes" in point.faults:
        index = pc.divide(pc.subtract(at, _mod(at, interval_ns)), interval_ns)
        out = pc.if_else(
            pc.equal(_mod(index, SPIKE_EVERY), 0), pc.add(out, SPIKE_AMPLITUDES * point.amplitude), out
        )
    return out


def grid(start_ns: int, end_ns: int, interval_ns: int) -> range:
    """Grid timestamps in `[start_ns, end_ns)`: multiples of the interval since the epoch."""
    first = -(-start_ns // interval_ns) * interval_ns
    return range(first, end_ns, interval_ns)


class SyntheticConnector(Connector):
    """Generates the configured points; `check` always succeeds."""

    type = "synthetic"
    config_model = SyntheticConfig

    @property
    def _config(self) -> SyntheticConfig:
        assert isinstance(self.config, SyntheticConfig)
        return self.config

    async def check(self) -> None:
        """Nothing to reach."""

    async def search(self, query: str, limit: int) -> list[RemotePoint]:
        """Configured points whose id or name contains `query` (case-insensitive)."""
        q = query.lower()
        found = [
            RemotePoint(external_id=p.external_id, name=p.name or p.external_id, unit=p.unit)
            for p in self._config.points
            if q in p.external_id.lower() or q in (p.name or "").lower()
        ]
        return found[:limit]

    async def fetch(
        self, points: Sequence[PointRef], start_ns: int, end_ns: int
    ) -> AsyncIterator[FetchedBatch]:
        """Rows of every known point; a point the config does not have yields nothing."""
        cfg = self._config
        by_id = {p.external_id: p for p in cfg.points}
        interval_ns = cfg.interval_s * SECOND_NS
        stamps = grid(start_ns, end_ns, interval_ns)
        for ref in points:
            point = by_id.get(ref.external_id)
            if point is None:
                continue
            for lo in range(0, len(stamps), ROWS_PER_BATCH):
                ts = pa.array(stamps[lo : lo + ROWS_PER_BATCH], pa.int64())
                quality = pa.array(["good"] * len(ts), pa.string())
                table = pa.Table.from_arrays(
                    [ts, values(point, cfg.seed, interval_ns, ts), quality], schema=BATCH_SCHEMA
                )
                yield FetchedBatch(series_id=ref.series_id, table=table)
