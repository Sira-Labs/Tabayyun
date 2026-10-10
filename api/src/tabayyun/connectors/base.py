"""The connector interface (spec 021).

A connector reaches one external system for one source. It checks the connection, may list
points, and returns observations for points over a window; everything else (which windows,
how many calls, pacing, retries, the cache, coverage, health) is the fetch engine's
(`tabayyun.connectors.fetch`). Connectors run in the job worker, never in a request.
"""

from __future__ import annotations

import builtins
import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar

import pyarrow as pa
from pydantic import BaseModel, ConfigDict, Field

from tabayyun.connectors.errors import NotSupportedError
from tabayyun.connectors.net import NetPolicy

SECOND_NS = 1_000_000_000
QUALITIES = ("good", "uncertain", "bad", "estimated")
BATCH_SCHEMA = pa.schema([("ts", pa.int64()), ("value", pa.float64()), ("quality", pa.string())])


class ConnectorConfig(BaseModel):
    """Settings every connector's config inherits (spec 021). Unknown keys are refused."""

    model_config = ConfigDict(extra="forbid")

    poll_interval_s: int | None = Field(default=None, ge=60, le=86_400)
    backfill_s: int = Field(default=86_400, ge=60, le=366 * 86_400)
    settle_s: int = Field(default=300, ge=0, le=86_400)
    requests_per_second: float = Field(default=5.0, gt=0, le=50)
    max_points: int = Field(default=100, ge=1, le=1000)
    max_span_s: int = Field(default=7 * 86_400, ge=60, le=31 * 86_400)


@dataclass(frozen=True)
class Limits:
    """What one call may ask for, and how often calls may start."""

    max_points: int
    max_span_ns: int
    requests_per_second: float

    @classmethod
    def of(cls, config: ConnectorConfig) -> Limits:
        """The limits a config asks for."""
        return cls(config.max_points, config.max_span_s * SECOND_NS, config.requests_per_second)

    def within(self, other: Limits) -> Limits:
        """The tighter of two limits, field by field: a connector may lower a config, never raise it."""
        return Limits(
            min(self.max_points, other.max_points),
            min(self.max_span_ns, other.max_span_ns),
            min(self.requests_per_second, other.requests_per_second),
        )


@dataclass(frozen=True)
class PointRef:
    """A series of the source and the point it reads in the external system."""

    series_id: uuid.UUID
    external_id: str


@dataclass(frozen=True)
class RemotePoint:
    """A point the external system offers, with what it knows about it."""

    external_id: str
    name: str
    unit: str | None = None
    description: str | None = None


@dataclass(frozen=True)
class FetchedBatch:
    """Observations of one series: `table` has `BATCH_SCHEMA` (ts ns UTC, value, quality)."""

    series_id: uuid.UUID
    table: pa.Table


@dataclass(frozen=True)
class PointFailure:
    """A point `fetch` could not read (deleted, renamed, no access): its message, no data.

    The engine records no coverage for it in that call and ends the fetch `partial` (spec 022).
    """

    series_id: uuid.UUID
    message: str


@dataclass(frozen=True)
class PointMetadata:
    """What the external system knows about a point; None where it knows nothing.

    `extra` is the system's own vocabulary (for PI: `ExcDev`, `CompDev`, …), kept under
    `series.metadata[<source type>]`.
    """

    unit: str | None = None
    description: str | None = None
    physical_min: float | None = None
    physical_max: float | None = None
    operational_min: float | None = None
    operational_max: float | None = None
    asset_path: str | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PointDescription:
    """`describe`'s answer for one series: its metadata, or why there is none."""

    series_id: uuid.UUID
    metadata: PointMetadata | None
    error: str | None = None


class Connector(ABC):
    """One source's connection to its external system."""

    type: ClassVar[str]
    # `builtins.type`: the class attribute `type` above shadows the builtin in this body.
    config_model: ClassVar[builtins.type[ConnectorConfig]] = ConnectorConfig
    credentials_model: ClassVar[builtins.type[BaseModel] | None] = None

    def __init__(self, config: ConnectorConfig, credentials: BaseModel | None, net: NetPolicy) -> None:
        self.config = config
        self.credentials = credentials
        self.net = net

    def limits(self) -> Limits:
        """The limits of one call; a connector may tighten the config's, never loosen them."""
        return Limits.of(self.config)

    @abstractmethod
    async def check(self) -> None:
        """Reach the system and authenticate.

        Raises:
            tabayyun.connectors.errors.ConnectorError: it cannot be reached or refuses the
                credentials.
        """

    async def search(self, query: str, limit: int) -> list[RemotePoint]:
        """Points whose name or id matches `query`, at most `limit`."""
        raise NotSupportedError(f"{self.type} has no point search")

    async def describe(self, points: Sequence[PointRef]) -> list[PointDescription]:
        """Metadata of `points` (spec 022): one description per point, in order."""
        raise NotSupportedError(f"{self.type} has no metadata import")

    @classmethod
    def supports(cls, method: str) -> bool:
        """Whether the class overrides an optional method (`search`, `describe`)."""
        return getattr(cls, method) is not getattr(Connector, method)

    @abstractmethod
    def fetch(
        self, points: Sequence[PointRef], start_ns: int, end_ns: int
    ) -> AsyncIterator[FetchedBatch | PointFailure]:
        """Observations of `points` in `[start_ns, end_ns)`, as batches (several per point allowed).

        A point it cannot read yields a `PointFailure` instead (spec 022); the others go on.

        Raises:
            tabayyun.connectors.errors.ConnectorError: the call failed; the engine retries it
                when `retryable`.
        """
