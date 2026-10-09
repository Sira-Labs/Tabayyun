"""Connector framework (spec 021): the interface, the registry and the built-in connectors.

`get(type)` gives the connector class of a source type; a type without one (`upload`) is not
a connector. `build(...)` validates a stored config and credentials and makes the instance.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ValidationError

from tabayyun.connectors.base import (
    BATCH_SCHEMA,
    QUALITIES,
    Connector,
    ConnectorConfig,
    FetchedBatch,
    Limits,
    PointDescription,
    PointFailure,
    PointMetadata,
    PointRef,
    RemotePoint,
)
from tabayyun.connectors.errors import AuthError, ConnectorError, NotSupportedError, TargetRefusedError
from tabayyun.connectors.net import NetPolicy
from tabayyun.connectors.synthetic import SyntheticConnector

__all__ = [
    "BATCH_SCHEMA",
    "QUALITIES",
    "AuthError",
    "Connector",
    "ConnectorConfig",
    "ConnectorError",
    "FetchedBatch",
    "Limits",
    "NetPolicy",
    "NotSupportedError",
    "PointDescription",
    "PointFailure",
    "PointMetadata",
    "PointRef",
    "RemotePoint",
    "TargetRefusedError",
    "build",
    "get",
    "is_connector",
    "register",
]

_REGISTRY: dict[str, type[Connector]] = {}


def register(cls: type[Connector]) -> type[Connector]:
    """Make `cls` the connector of its source type (usable as a decorator)."""
    _REGISTRY[cls.type] = cls
    return cls


def get(source_type: str) -> type[Connector] | None:
    """The connector class of a source type, or None (an `upload` source has none)."""
    return _REGISTRY.get(source_type)


def is_connector(source_type: str) -> bool:
    """Whether sources of this type fetch through a connector."""
    return source_type in _REGISTRY


def build(
    source_type: str, config: dict[str, Any], credentials: dict[str, Any] | None, net: NetPolicy
) -> Connector:
    """An instance for a stored source.

    Raises:
        ConnectorError: the type has no connector, or the stored config or credentials no
            longer validate (a non-retryable error naming the fields, never their values).
    """
    cls = get(source_type)
    if cls is None:
        raise ConnectorError(f"source type {source_type} has no connector", retryable=False)
    try:
        cfg = cls.config_model.model_validate(config)
        creds: BaseModel | None = None
        if cls.credentials_model is not None and credentials is not None:
            creds = cls.credentials_model.model_validate(credentials)
    except ValidationError as exc:
        fields = sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})
        raise ConnectorError(f"invalid stored settings: {', '.join(fields)}", retryable=False) from None
    return cls(cfg, creds, net)


register(SyntheticConnector)
