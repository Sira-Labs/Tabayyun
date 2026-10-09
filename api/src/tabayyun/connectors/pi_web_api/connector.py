"""The `pi_web_api` connector (spec 022): AVEVA PI through PI Web API.

External ids are PI paths: `\\SERVER\TAG` for a PI point, `\\AF\DB\Element|Attribute` for an AF
attribute (the `|` tells them apart). Paths resolve to WebIds once per instance; a fetch
call resolves its points with one `POST batch`.

Recorded values are read one stream at a time (`streams/{webId}/recorded`), paging past PI's
silent `maxCount` cut by `values.read_page`. `streamsets/recorded` would save requests, but
PI's reference does not say whether its `maxCount` counts per stream, so a cut answer could
go unnoticed; revisit with a live server.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

import pyarrow as pa

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
from tabayyun.connectors.pi_web_api.client import PiClient, PiNotFoundError, open_client, raise_for
from tabayyun.connectors.pi_web_api.config import PiWebApiConfig, PiWebApiCredentials
from tabayyun.connectors.pi_web_api.values import Cursor, TimestampError, format_time, read_page

POINT_FIELDS = "WebId;Name;Path;Descriptor;EngineeringUnits;PointType;Zero;Span;Step"
ATTRIBUTE_FIELDS = (
    "WebId;Name;Path;Description;DefaultUnitsNameAbbreviation;DataReferencePlugIn;Step;Links.Point"
)
RECORDED_FIELDS = "Items.Timestamp;Items.Value;Items.Good;Items.Questionable;Items.Substituted;Items.Errors"
# PI point attributes kept in the series metadata (lower case: PI's names are case-insensitive).
COMPRESSION = (
    "excdev",
    "excdevpercent",
    "excmin",
    "excmax",
    "compdev",
    "compdevpercent",
    "compmin",
    "compmax",
    "compressing",
)
PHYSICAL = {"minimum": "physical_min", "maximum": "physical_max"}
OPERATIONAL = {"lo": "operational_min", "hi": "operational_max"}
OTHER_LIMITS = ("lolo", "hihi", "target")
PI_POINT_REFERENCE = "PI Point"
# WebIds go into URL paths; anything but PI's URL-safe base64 alphabet is refused.
WEB_ID = re.compile(r"^[A-Za-z0-9_-]{1,512}$")


def _web_id(value: Any) -> str:
    """A WebId PI answered, checked before it goes into a URL path."""
    if not isinstance(value, str) or not WEB_ID.match(value):
        raise ConnectorError("PI Web API answered a malformed WebId", retryable=False)
    return value


def is_attribute(path: str) -> bool:
    """Whether a PI path names an AF attribute rather than a PI point."""
    return "|" in path


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _items(body: Any) -> list[Mapping[str, Any]]:
    items = body.get("Items") if isinstance(body, Mapping) else None
    return [i for i in items if isinstance(i, Mapping)] if isinstance(items, list) else []


def _wildcard(query: str) -> str:
    """PI's name filter for a query: as given with `*` or `?`, else `*query*`."""
    query = query.strip()
    return query if any(c in query for c in "*?") else f"*{query}*"


class PiWebApiConnector(Connector):
    """Reads PI points and AF attributes through PI Web API."""

    type = "pi_web_api"
    config_model = PiWebApiConfig
    credentials_model = PiWebApiCredentials

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._web_ids: dict[str, str] = {}

    @property
    def _config(self) -> PiWebApiConfig:
        assert isinstance(self.config, PiWebApiConfig)
        return self.config

    @property
    def _credentials(self) -> PiWebApiCredentials | None:
        assert self.credentials is None or isinstance(self.credentials, PiWebApiCredentials)
        return self.credentials

    def _open(self) -> Any:
        return open_client(self._config, self._credentials, self.net)

    # Check and search

    async def check(self) -> None:
        """The data server (and the AF database) answer, and the data server is connected."""
        async with self._open() as pi:
            server = await self._data_server(pi)
            if server.get("IsConnected") is False:
                raise ConnectorError("data server not connected", retryable=True)
            if self._config.asset_database is not None:
                await self._asset_database(pi)

    async def _data_server(self, pi: PiClient) -> Mapping[str, Any]:
        try:
            body = await pi.get(
                "dataservers",
                {"path": self._config.data_server, "selectedFields": "WebId;Name;IsConnected"},
                what="data server",
            )
        except PiNotFoundError:
            raise ConnectorError(
                f"data server not found: {self._config.data_server}", retryable=False
            ) from None
        if not isinstance(body, Mapping) or not body.get("WebId"):
            raise ConnectorError("data server answer without a WebId", retryable=False)
        return body

    async def _asset_database(self, pi: PiClient) -> Mapping[str, Any]:
        assert self._config.asset_database is not None
        try:
            body = await pi.get(
                "assetdatabases",
                {"path": self._config.asset_database, "selectedFields": "WebId;Name"},
                what="asset database",
            )
        except PiNotFoundError:
            raise ConnectorError(
                f"asset database not found: {self._config.asset_database}", retryable=False
            ) from None
        if not isinstance(body, Mapping) or not body.get("WebId"):
            raise ConnectorError("asset database answer without a WebId", retryable=False)
        return body

    async def search(self, query: str, limit: int) -> list[RemotePoint]:
        """PI points by name, then PI Point AF attributes of the asset database, `limit` in all."""
        name_filter = _wildcard(query)
        found: list[RemotePoint] = []
        async with self._open() as pi:
            server = await self._data_server(pi)
            body = await pi.get(
                f"dataservers/{_web_id(server['WebId'])}/points",
                {
                    "nameFilter": name_filter,
                    "maxCount": limit,
                    "selectedFields": "Items.Name;Items.Path;Items.Descriptor;Items.EngineeringUnits",
                },
                what="point search",
            )
            for item in _items(body):
                if item.get("Path"):
                    found.append(
                        RemotePoint(
                            external_id=str(item["Path"]),
                            name=str(item.get("Name") or item["Path"]),
                            unit=_text(item.get("EngineeringUnits")),
                            description=_text(item.get("Descriptor")),
                        )
                    )
            if self._config.asset_database is not None and len(found) < limit:
                database = await self._asset_database(pi)
                body = await pi.get(
                    f"assetdatabases/{_web_id(database['WebId'])}/elementattributes",
                    {
                        "attributeNameFilter": name_filter,
                        "searchFullHierarchy": "true",
                        "maxCount": limit,
                        "selectedFields": (
                            "Items.Name;Items.Path;Items.Description;"
                            "Items.DefaultUnitsNameAbbreviation;Items.DataReferencePlugIn"
                        ),
                    },
                    what="attribute search",
                )
                for item in _items(body):
                    if item.get("DataReferencePlugIn") == PI_POINT_REFERENCE and item.get("Path"):
                        found.append(
                            RemotePoint(
                                external_id=str(item["Path"]),
                                name=str(item.get("Name") or item["Path"]),
                                unit=_text(item.get("DefaultUnitsNameAbbreviation")),
                                description=_text(item.get("Description")),
                            )
                        )
        return found[:limit]

    # Fetch

    async def _resolve(self, pi: PiClient, paths: Sequence[str]) -> dict[str, str | PiNotFoundError]:
        """WebIds of `paths` (cached per instance), resolved with one batch request."""
        missing = [p for p in dict.fromkeys(paths) if p not in self._web_ids]
        out: dict[str, str | PiNotFoundError] = {p: self._web_ids[p] for p in paths if p in self._web_ids}
        if not missing:
            return out
        requests = {
            str(n): {
                "Method": "GET",
                "Resource": pi.url(
                    "attributes" if is_attribute(path) else "points",
                    {"path": path, "selectedFields": "WebId"},
                ),
            }
            for n, path in enumerate(missing)
        }
        body = await pi.post("batch", requests, what="path lookup")
        if not isinstance(body, Mapping):
            raise ConnectorError("PI Web API batch answer is not an object", retryable=False)
        for n, path in enumerate(missing):
            answer = body.get(str(n))
            if not isinstance(answer, Mapping):
                raise ConnectorError("PI Web API batch answer misses a request", retryable=False)
            status = answer.get("Status", answer.get("StatusCode"))
            content = answer.get("Content")
            if status in (404, 410) or (
                status == 200 and not (isinstance(content, Mapping) and content.get("WebId"))
            ):
                out[path] = PiNotFoundError(f"not found in PI: {path}")
                continue
            if status != 200:
                raise_for(int(status) if isinstance(status, int) else 502, content, f"path lookup of {path}")
            assert isinstance(content, Mapping)
            self._web_ids[path] = out[path] = _web_id(content["WebId"])
        return out

    async def fetch(
        self, points: Sequence[PointRef], start_ns: int, end_ns: int
    ) -> AsyncIterator[FetchedBatch | PointFailure]:
        """Recorded values of each point in `[start_ns, end_ns)`, page by page."""
        valid = [p for p in points if p.external_id.startswith("\\\\")]
        for ref in points:
            if ref not in valid:
                yield PointFailure(ref.series_id, f"not a PI path: {ref.external_id[:100]}")
        if not valid:
            return
        async with self._open() as pi:
            web_ids = await self._resolve(pi, [p.external_id for p in valid])
            for ref in valid:
                web_id = web_ids[ref.external_id]
                if isinstance(web_id, PiNotFoundError):
                    yield PointFailure(ref.series_id, str(web_id))
                    continue
                cursor: Cursor | None = Cursor(start_ns)
                while cursor is not None:
                    params = {
                        "startTime": format_time(cursor.at_ns),
                        "endTime": format_time(end_ns, ceil=True),
                        "boundaryType": "Inside",
                        "maxCount": self._config.max_count,
                        "selectedFields": RECORDED_FIELDS,
                    }
                    try:
                        body = await pi.get(f"streams/{web_id}/recorded", params, what=ref.external_id)
                    except PiNotFoundError as exc:
                        self._web_ids.pop(ref.external_id, None)
                        yield PointFailure(ref.series_id, str(exc))
                        break
                    try:
                        rows, cursor = read_page(
                            _items(body), cursor, start_ns, end_ns, self._config.max_count
                        )
                    except TimestampError as exc:
                        raise ConnectorError(f"{ref.external_id}: {exc}", retryable=False) from None
                    if rows.ts:
                        table = pa.Table.from_arrays(
                            [
                                pa.array(rows.ts, pa.int64()),
                                pa.array(rows.value, pa.float64()),
                                pa.array(rows.quality, pa.string()),
                            ],
                            schema=BATCH_SCHEMA,
                        )
                        yield FetchedBatch(ref.series_id, table)

    # Describe

    async def describe(self, points: Sequence[PointRef]) -> list[PointDescription]:
        """Unit, limits, asset path and compression settings of each point."""
        out: list[PointDescription] = []
        async with self._open() as pi:
            for ref in points:
                path = ref.external_id
                try:
                    if not path.startswith("\\\\"):
                        raise PiNotFoundError(f"not a PI path: {path[:100]}")
                    meta = await (
                        self._describe_attribute(pi, path)
                        if is_attribute(path)
                        else self._describe_point(pi, path)
                    )
                except PiNotFoundError as exc:
                    out.append(PointDescription(ref.series_id, None, error=str(exc)))
                    continue
                out.append(PointDescription(ref.series_id, meta))
        return out

    async def _point_attributes(self, pi: PiClient, web_id: str) -> dict[str, Any]:
        body = await pi.get(
            f"points/{web_id}/attributes",
            {"selectedFields": "Items.Name;Items.Value"},
            what="point attributes",
        )
        values = {str(i.get("Name", "")).lower(): i.get("Value") for i in _items(body)}
        return {name: values[name] for name in COMPRESSION if name in values}

    async def _describe_point(self, pi: PiClient, path: str) -> PointMetadata:
        point = await pi.get("points", {"path": path, "selectedFields": POINT_FIELDS}, what=path)
        if not isinstance(point, Mapping) or not point.get("WebId"):
            raise PiNotFoundError(f"not found in PI: {path}")
        self._web_ids[path] = web_id = _web_id(point["WebId"])
        zero, span = _number(point.get("Zero")), _number(point.get("Span"))
        extra: dict[str, Any] = {
            "point_type": point.get("PointType"),
            "step": point.get("Step"),
            "zero": zero,
            "span": span,
            **await self._point_attributes(pi, web_id),
        }
        has_range = zero is not None and span is not None and span > 0
        return PointMetadata(
            unit=_text(point.get("EngineeringUnits")),
            description=_text(point.get("Descriptor")),
            physical_min=zero if has_range else None,
            physical_max=zero + span if has_range and zero is not None and span is not None else None,
            extra=extra,
        )

    async def _describe_attribute(self, pi: PiClient, path: str) -> PointMetadata:
        attribute = await pi.get("attributes", {"path": path, "selectedFields": ATTRIBUTE_FIELDS}, what=path)
        if not isinstance(attribute, Mapping) or not attribute.get("WebId"):
            raise PiNotFoundError(f"not found in AF: {path}")
        web_id = _web_id(attribute["WebId"])
        self._web_ids[path] = web_id
        limits: dict[str, float | None] = {}
        extra: dict[str, Any] = {"step": attribute.get("Step")}
        traits = await pi.get(
            f"attributes/{web_id}/attributes",
            {"traitCategory": "Limit", "selectedFields": "Items.WebId;Items.TraitName"},
            what="limit traits",
        )
        for trait in _items(traits):
            name = str(trait.get("TraitName", "")).lower().removeprefix("limit")
            if not trait.get("WebId") or name not in (*PHYSICAL, *OPERATIONAL, *OTHER_LIMITS):
                continue
            value = await pi.get(
                f"streams/{_web_id(trait['WebId'])}/value",
                {"selectedFields": "Value;Good"},
                what="limit value",
            )
            number = (
                _number(value.get("Value"))
                if isinstance(value, Mapping) and value.get("Good", True)
                else None
            )
            if name in PHYSICAL:
                limits[PHYSICAL[name]] = number
            elif name in OPERATIONAL:
                limits[OPERATIONAL[name]] = number
            elif number is not None:
                extra[f"limit_{name}"] = number
        links = attribute.get("Links")
        point_link = links.get("Point") if isinstance(links, Mapping) else None
        if isinstance(point_link, str):
            # Only the WebId at the end of the link is used: the URL itself is never followed.
            extra.update(await self._point_attributes(pi, _web_id(point_link.rstrip("/").rsplit("/", 1)[-1])))
        return PointMetadata(
            unit=_text(attribute.get("DefaultUnitsNameAbbreviation")),
            description=_text(attribute.get("Description")),
            asset_path=path.split("|", 1)[0],
            extra=extra,
            **limits,
        )
