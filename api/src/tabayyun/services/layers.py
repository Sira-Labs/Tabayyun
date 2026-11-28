"""Layer names of a read (spec 036): `raw`, `corrected@latest` and `corrected@v{n}`.

Reads are opt-in (ADR-0010): every read names its layer and the default is raw. A name is
parsed without the database (`parse_layer`); `resolve` then finds the version of the series'
corrected layer it stands for, which is None for raw and for a series without an approved
correction, whose corrected layer is its raw rows.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tabayyun.db.models import Correction

RAW_NAME = "raw"
LATEST_NAME = "corrected@latest"
UNKNOWN_LAYER_VERSION = "unknown_layer_version"
# Versions are 32-bit integers in `corrections`; no leading zero, as the cache's layer names.
_VERSIONED = re.compile(r"corrected@v([1-9]\d{0,9})")
_MAX_VERSION = 2**31 - 1
_NAMES = "raw, corrected@latest or corrected@v{n}"


class LayerError(ValueError):
    """A layer name that is refused; the message is the 422's."""


class UnknownLayerVersionError(LookupError):
    """`corrected@v{n}` names a version that is not an approved one of the series."""


@dataclass(frozen=True)
class LayerRef:
    """A layer as a request named it. `version` None with kind `corrected` is the latest."""

    name: str
    kind: Literal["raw", "corrected"]
    version: int | None = None

    @property
    def is_raw(self) -> bool:
        """Whether this is the raw layer, the default of every read."""
        return self.kind == "raw"


RAW = LayerRef(name=RAW_NAME, kind="raw")


def parse_layer(text: str | None, *, versions: bool = True) -> LayerRef:
    """The layer a `layer` parameter names; None (not given) is raw.

    `versions=False` is for reads of several series, where `corrected@v{n}` is refused
    because versions count per series.

    Raises:
        LayerError: any other name.
    """
    if text is None or text == RAW_NAME:
        return RAW
    if text == LATEST_NAME:
        return LayerRef(name=text, kind="corrected")
    match = _VERSIONED.fullmatch(text)
    if match is None or int(match.group(1)) > _MAX_VERSION:
        raise LayerError(f"layer must be {_NAMES}")
    if not versions:
        raise LayerError(
            "layer must be raw or corrected@latest here: versions count per series, "
            "so corrected@v{n} names a layer of one series only"
        )
    return LayerRef(name=text, kind="corrected", version=int(match.group(1)))


async def resolve(session: AsyncSession, series_id: uuid.UUID, ref: LayerRef) -> tuple[int | None, str]:
    """The version of the series' corrected layer to read for `ref`, and a mark of its state.

    The version is None for raw and for `corrected@latest` of a series without an approved
    correction: both read the raw rows. The mark changes whenever the layer up to that
    version does (an approval at or below it), for ETags; it is empty for raw.

    Raises:
        UnknownLayerVersionError: `corrected@v{n}` where n is not an approved version.
    """
    if ref.is_raw:
        return None, ""
    approved = Correction.status == "approved"
    stmt = select(func.max(Correction.version), func.count(), func.max(Correction.decided_at)).where(
        Correction.series_id == series_id, approved
    )
    if ref.version is not None:
        stmt = stmt.where(Correction.version <= ref.version)
    highest, count, decided_at = (await session.execute(stmt)).one()
    if ref.version is not None and highest != ref.version:
        raise UnknownLayerVersionError(ref.name)
    if highest is None:
        return None, "none"
    return int(highest), f"{count}:{decided_at.isoformat() if decided_at else ''}"
