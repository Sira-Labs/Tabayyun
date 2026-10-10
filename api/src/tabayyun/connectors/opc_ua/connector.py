"""The `opc_ua` connector (spec 023): historical data from an OPC UA server through asyncua.

External ids are node ids (`ns=2;s=FIC101.PV`). A fetch checks every node's class and
history-read access in one Read, then reads raw history for all of them in one HistoryRead per
round, following continuation points. Search browses the hierarchy breadth-first in batches.
"""

from __future__ import annotations

import fnmatch
import uuid
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pyarrow as pa
from asyncua import ua
from asyncua.ua.uaerrors import UaError
from pydantic import BaseModel

from tabayyun.connectors.base import (
    BATCH_SCHEMA,
    Connector,
    FetchedBatch,
    PointDescription,
    PointFailure,
    PointMetadata,
    PointRef,
    RemotePoint,
)
from tabayyun.connectors.errors import ConnectorError
from tabayyun.connectors.opc_ua.config import (
    OpcUaConfig,
    OpcUaCredentials,
    certificate_info,
    generate_identity,
)
from tabayyun.connectors.opc_ua.session import Session, code_name, open_session
from tabayyun.connectors.opc_ua.status import convert, from_ns, severity, to_ns


def _ns0(identifier: int) -> ua.NodeId:
    """A node of the standard namespace by its numeric id."""
    return ua.NodeId.from_string(f"i={identifier}")


BROWSE_BATCH = 100
MAX_PARENTS = 20
HISTORY_READ_BIT = 1 << int(ua.AccessLevel.HistoryRead)
BAD_NO_DATA = 0x809B0000
HIERARCHICAL = _ns0(ua.ObjectIds.HierarchicalReferences)
HAS_PROPERTY = _ns0(ua.ObjectIds.HasProperty)
STOP_AT = {_ns0(ua.ObjectIds.ObjectsFolder), _ns0(ua.ObjectIds.RootFolder)}
OBJECT_OR_VARIABLE = int(ua.NodeClass.Object) | int(ua.NodeClass.Variable)
PROPERTIES = ("EngineeringUnits", "EURange", "InstrumentRange")


def _local(node: ua.NodeId) -> ua.NodeId:
    """A plain NodeId from an ExpandedNodeId (namespace index form)."""
    return ua.NodeId(node.Identifier, node.NamespaceIndex, node.NodeIdType)


def _remote(node: ua.NodeId) -> bool:
    """Whether a reference target lives on another server (an ExpandedNodeId with a server index)."""
    return bool(getattr(node, "ServerIndex", 0))


def _parse(external_id: str) -> ua.NodeId | None:
    try:
        return ua.NodeId.from_string(external_id)
    except (UaError, ValueError):
        return None


def _matcher(query: str) -> Any:
    query = query.strip().lower()
    if any(c in query for c in "*?"):
        return lambda *names: any(fnmatch.fnmatchcase(n.lower(), query) for n in names if n)
    return lambda *names: any(query in n.lower() for n in names if n)


def _text(value: Any) -> str | None:
    text = getattr(value, "Text", value)
    text = str(text).strip() if text is not None else ""
    return text or None


def _range(value: Any) -> tuple[float | None, float | None]:
    low, high = getattr(value, "Low", None), getattr(value, "High", None)
    if isinstance(low, int | float) and isinstance(high, int | float):
        return float(low), float(high)
    return None, None


class OpcUaConnector(Connector):
    """Reads historized variables of an OPC UA server."""

    type = "opc_ua"
    config_model = OpcUaConfig
    credentials_model = OpcUaCredentials

    @property
    def _config(self) -> OpcUaConfig:
        assert isinstance(self.config, OpcUaConfig)
        return self.config

    def _open(self) -> Any:
        assert self.credentials is None or isinstance(self.credentials, OpcUaCredentials)
        return open_session(self._config, self.credentials, self.net)

    # Credentials

    @classmethod
    def prepare_credentials(
        cls, new: BaseModel, previous: BaseModel | None, *, source_id: uuid.UUID
    ) -> tuple[BaseModel, dict[str, Any]]:
        """Keep the earlier client identity, or generate one, unless `new` brings its own."""
        assert isinstance(new, OpcUaCredentials)
        if new.has_identity:
            return new, {"client_certificate": "provided"}
        if isinstance(previous, OpcUaCredentials) and previous.has_identity:
            kept = new.model_copy(
                update={
                    "client_certificate_pem": previous.client_certificate_pem,
                    "client_private_key_pem": previous.client_private_key_pem,
                }
            )
            return kept, {"client_certificate": "kept"}
        cert, key = generate_identity(source_id)
        made = new.model_copy(update={"client_certificate_pem": cert, "client_private_key_pem": key})
        return made, {"client_certificate": "generated"}

    @classmethod
    def client_certificate(cls, credentials: BaseModel) -> dict[str, Any] | None:
        """The client certificate (never the key) and its thumbprints."""
        if not isinstance(credentials, OpcUaCredentials) or credentials.client_certificate_pem is None:
            return None
        return certificate_info(credentials.client_certificate_pem)

    # Check and search

    async def check(self) -> None:
        """Connect, secure and log on; the server must be running."""
        async with self._open() as session:
            [state] = await session.call(
                "read",
                ua.ReadParameters(
                    NodesToRead=[
                        ua.ReadValueId(
                            NodeId=_ns0(ua.ObjectIds.Server_ServerStatus_State),
                            AttributeId=ua.AttributeIds.Value,
                        )
                    ]
                ),
            )
            if severity(state.StatusCode.value) == "bad" or state.Value.Value != ua.ServerState.Running:
                raise ConnectorError("OPC UA server is not running", retryable=True)

    async def _browse(
        self,
        session: Session,
        nodes: Sequence[ua.NodeId],
        *,
        reference: ua.NodeId,
        mask: int,
        inverse: bool = False,
    ) -> list[list[ua.ReferenceDescription]]:
        """The references of each node (one Browse, then BrowseNext while continuation points remain)."""
        direction = ua.BrowseDirection.Inverse if inverse else ua.BrowseDirection.Forward
        params = ua.BrowseParameters(
            NodesToBrowse=[
                ua.BrowseDescription(
                    NodeId=node,
                    BrowseDirection=direction,
                    ReferenceTypeId=reference,
                    IncludeSubtypes=True,
                    NodeClassMask=mask,
                    ResultMask=int(ua.BrowseResultMask.All),
                )
                for node in nodes
            ]
        )
        out: list[list[ua.ReferenceDescription]] = []
        for result in await session.call("browse", params):
            refs = list(result.References or [])
            point = result.ContinuationPoint
            while point:
                [more] = await session.call(
                    "browse_next",
                    ua.BrowseNextParameters(ReleaseContinuationPoints=False, ContinuationPoints=[point]),
                )
                refs.extend(more.References or [])
                point = more.ContinuationPoint
            out.append(refs if severity(result.StatusCode.value) != "bad" else [])
        return out

    async def search(self, query: str, limit: int) -> list[RemotePoint]:
        """Variables below `browse_root` whose display or browse name matches, breadth-first."""
        config = self._config
        matches = _matcher(query)
        root = ua.NodeId.from_string(config.browse_root)
        frontier: list[tuple[ua.NodeId, list[str], int]] = [(root, [], 0)]
        seen = {root.to_string()}
        found: list[RemotePoint] = []
        async with self._open() as session:
            while frontier and len(found) < limit and len(seen) < config.browse_limit:
                batch, frontier = frontier[:BROWSE_BATCH], frontier[BROWSE_BATCH:]
                results = await self._browse(
                    session, [node for node, _, _ in batch], reference=HIERARCHICAL, mask=OBJECT_OR_VARIABLE
                )
                for (_, path, depth), refs in zip(batch, results, strict=True):
                    for ref in refs:
                        if ref.ReferenceTypeId == HAS_PROPERTY or _remote(ref.NodeId):
                            continue
                        node = _local(ref.NodeId)
                        key = node.to_string()
                        if key in seen or len(seen) >= config.browse_limit:
                            continue
                        seen.add(key)
                        name = _text(ref.DisplayName) or ref.BrowseName.Name
                        here = [*path, name]
                        if ref.NodeClass == ua.NodeClass.Variable and matches(name, ref.BrowseName.Name):
                            found.append(RemotePoint(external_id=key, name=name, description="/".join(here)))
                            if len(found) >= limit:
                                break
                        if depth + 1 < config.browse_depth:
                            frontier.append((node, here, depth + 1))
                    if len(found) >= limit:
                        break
        return found[:limit]

    # Fetch

    async def _readable(
        self, session: Session, points: Sequence[PointRef]
    ) -> tuple[list[tuple[PointRef, ua.NodeId]], list[PointFailure]]:
        """The points whose nodes are variables with history-read access; failures for the rest."""
        parsed: list[tuple[PointRef, ua.NodeId]] = []
        failures: list[PointFailure] = []
        for ref in points:
            node = _parse(ref.external_id)
            if node is None:
                failures.append(PointFailure(ref.series_id, f"not a node id: {ref.external_id[:100]}"))
            else:
                parsed.append((ref, node))
        if not parsed:
            return [], failures
        reads = [
            ua.ReadValueId(NodeId=node, AttributeId=attribute)
            for _, node in parsed
            for attribute in (ua.AttributeIds.NodeClass, ua.AttributeIds.AccessLevel)
        ]
        values = await session.call("read", ua.ReadParameters(NodesToRead=reads))
        readable: list[tuple[PointRef, ua.NodeId]] = []
        for (ref, node), node_class, access in zip(parsed, values[0::2], values[1::2], strict=True):
            if severity(node_class.StatusCode.value) == "bad":
                failures.append(
                    PointFailure(
                        ref.series_id, f"{ref.external_id}: {code_name(node_class.StatusCode.value)}"
                    )
                )
            elif node_class.Value.Value != ua.NodeClass.Variable:
                failures.append(PointFailure(ref.series_id, f"{ref.external_id}: not a variable"))
            elif (
                severity(access.StatusCode.value) == "bad"
                or not int(access.Value.Value or 0) & HISTORY_READ_BIT
            ):
                failures.append(PointFailure(ref.series_id, f"{ref.external_id}: no history read access"))
            else:
                readable.append((ref, node))
        return readable, failures

    async def fetch(
        self, points: Sequence[PointRef], start_ns: int, end_ns: int
    ) -> AsyncIterator[FetchedBatch | PointFailure]:
        """Raw history of each readable node in `[start_ns, end_ns)`, page by page."""
        config = self._config
        details = ua.ReadRawModifiedDetails(
            IsReadModified=False,
            StartTime=from_ns(start_ns),
            EndTime=from_ns(end_ns, ceil=True),
            NumValuesPerNode=config.max_values,
            ReturnBounds=False,
        )
        async with self._open() as session:
            readable, failures = await self._readable(session, points)
            for failure in failures:
                yield failure
            pending: list[tuple[PointRef, ua.NodeId, bytes | None]] = [
                (ref, node, None) for ref, node in readable
            ]
            try:
                while pending:
                    params = ua.HistoryReadParameters(
                        HistoryReadDetails=details,
                        TimestampsToReturn=ua.TimestampsToReturn.Both,
                        ReleaseContinuationPoints=False,
                        NodesToRead=[
                            ua.HistoryReadValueId(NodeId=node, ContinuationPoint=cp)
                            for _, node, cp in pending
                        ],
                    )
                    results = await session.call("history_read", params)
                    following: list[tuple[PointRef, ua.NodeId, bytes | None]] = []
                    for (ref, node, _), result in zip(pending, results, strict=True):
                        status = result.StatusCode.value
                        if severity(status) == "bad" and status & 0xFFFF0000 != BAD_NO_DATA:
                            yield PointFailure(ref.series_id, f"{ref.external_id}: {code_name(status)}")
                            continue
                        values = getattr(result.HistoryData, "DataValues", None) or []
                        table = _rows(values, start_ns, end_ns)
                        if table is not None:
                            yield FetchedBatch(ref.series_id, table)
                        if result.ContinuationPoint:
                            following.append((ref, node, result.ContinuationPoint))
                    pending = following
            finally:
                if pending:
                    await _release(session, details, pending)

    # Describe

    async def describe(self, points: Sequence[PointRef]) -> list[PointDescription]:
        """Unit, ranges, description and asset path of each node."""
        out: list[PointDescription] = []
        async with self._open() as session:
            for ref in points:
                node = _parse(ref.external_id)
                if node is None:
                    out.append(
                        PointDescription(ref.series_id, None, error=f"not a node id: {ref.external_id[:100]}")
                    )
                    continue
                try:
                    out.append(PointDescription(ref.series_id, await self._describe(session, node)))
                except _NodeError as exc:
                    out.append(PointDescription(ref.series_id, None, error=f"{ref.external_id}: {exc}"))
        return out

    async def _describe(self, session: Session, node: ua.NodeId) -> PointMetadata:
        attributes = (
            ua.AttributeIds.DisplayName,
            ua.AttributeIds.Description,
            ua.AttributeIds.DataType,
            ua.AttributeIds.AccessLevel,
        )
        values = await session.call(
            "read",
            ua.ReadParameters(NodesToRead=[ua.ReadValueId(NodeId=node, AttributeId=a) for a in attributes]),
        )
        if severity(values[0].StatusCode.value) == "bad":
            raise _NodeError(code_name(values[0].StatusCode.value))
        display, description, data_type, access = (v.Value.Value for v in values)
        [props] = await self._browse(session, [node], reference=HAS_PROPERTY, mask=int(ua.NodeClass.Variable))
        wanted = [(p.BrowseName.Name, _local(p.NodeId)) for p in props if p.BrowseName.Name in PROPERTIES]
        read = {}
        if wanted:
            got = await session.call(
                "read",
                ua.ReadParameters(
                    NodesToRead=[
                        ua.ReadValueId(NodeId=n, AttributeId=ua.AttributeIds.Value) for _, n in wanted
                    ]
                ),
            )
            read = {
                name: v.Value.Value
                for (name, _), v in zip(wanted, got, strict=True)
                if severity(v.StatusCode.value) != "bad"
            }
        eu = read.get("EngineeringUnits")
        physical = _range(read.get("InstrumentRange"))
        operational = _range(read.get("EURange"))
        data_type_name = None
        if isinstance(data_type, ua.NodeId):
            ident = data_type.Identifier
            standard = data_type.NamespaceIndex == 0 and isinstance(ident, int)
            data_type_name = ua.ObjectIdNames.get(int(ident)) if standard else None
            data_type_name = data_type_name or data_type.to_string()
        extra: dict[str, Any] = {
            "node_id": node.to_string(),
            "data_type": data_type_name,
            "history_read": bool(int(access or 0) & HISTORY_READ_BIT),
            "eu_range": list(operational) if operational[0] is not None else None,
            "instrument_range": list(physical) if physical[0] is not None else None,
        }
        if eu is not None:
            extra["eu_unit_id"] = getattr(eu, "UnitId", None)
            extra["eu_namespace"] = getattr(eu, "NamespaceUri", None)
        return PointMetadata(
            unit=_text(getattr(eu, "DisplayName", None)) if eu is not None else None,
            description=_text(description),
            physical_min=physical[0],
            physical_max=physical[1],
            operational_min=operational[0],
            operational_max=operational[1],
            asset_path=await self._parents(session, node) or None,
            extra={k: v for k, v in extra.items() if v is not None} | {"display_name": _text(display)},
        )

    async def _parents(self, session: Session, node: ua.NodeId) -> str:
        """The display names from below the Objects folder down to the node's parent, joined by `/`."""
        names: list[str] = []
        current = node
        for _ in range(MAX_PARENTS):
            [refs] = await self._browse(session, [current], reference=HIERARCHICAL, mask=0, inverse=True)
            parent = next((r for r in refs if not _remote(r.NodeId)), None)
            if parent is None:
                break
            current = _local(parent.NodeId)
            if current in STOP_AT:
                break
            names.append(_text(parent.DisplayName) or parent.BrowseName.Name)
        return "/".join(reversed(names))


class _NodeError(Exception):
    """A node `describe` cannot read."""


def _rows(values: Sequence[ua.DataValue], start_ns: int, end_ns: int) -> pa.Table | None:
    ts: list[int] = []
    value: list[float] = []
    quality: list[str] = []
    for dv in values:
        stamp = dv.SourceTimestamp or dv.ServerTimestamp
        if stamp is None:
            continue
        ns = to_ns(stamp)
        if ns < start_ns or ns >= end_ns:
            continue
        v, q = convert(
            dv.Value.Value if dv.Value is not None else None, dv.StatusCode.value if dv.StatusCode else 0
        )
        ts.append(ns)
        value.append(v)
        quality.append(q)
    if not ts:
        return None
    return pa.Table.from_arrays(
        [pa.array(ts, pa.int64()), pa.array(value, pa.float64()), pa.array(quality, pa.string())],
        schema=BATCH_SCHEMA,
    )


async def _release(
    session: Session, details: Any, pending: Sequence[tuple[PointRef, ua.NodeId, bytes | None]]
) -> None:
    """Release the server's continuation points after an early stop (best effort)."""
    open_points = [(node, cp) for _, node, cp in pending if cp]
    if not open_points:
        return
    try:
        await session.client.uaclient.history_read(
            ua.HistoryReadParameters(
                HistoryReadDetails=details,
                ReleaseContinuationPoints=True,
                NodesToRead=[ua.HistoryReadValueId(NodeId=n, ContinuationPoint=cp) for n, cp in open_points],
            )
        )
    except Exception:  # noqa: BLE001, S110  (the session is closing anyway)
        pass
