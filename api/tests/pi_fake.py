"""A fake PI Web API for the `pi_web_api` connector tests (spec 022).

`FakePiWebApi.handler` is an `httpx.MockTransport` handler. It answers from the fixtures in
`tests/fixtures/pi_web_api/` (built from AVEVA's reference shapes; see the README there) and
generates recorded values on a minute grid:

- `FIC101.PV` (and the AF attribute `…\\FIC101|Flow`, which references it): a daily sine with
  noise; 02:00–03:00 flat; minute 100 questionable, 200 substituted, 300 an `I/O Timeout`
  system state, 400 a value with `Errors`; two values at minute 999 of each day;
- `LIC201.PV`: a sine with noise;
- `PMP301.STATE`: a digital point, `Running`/`Stopped` every six hours.

It honours `maxCount` (the earliest values first), `boundaryType=Inside` (both ends included),
`nameFilter` wildcards and `POST batch`. Every request is kept in `requests` for assertions;
`fail_status` makes every answer that status; `deleted` WebIds answer 410.
"""

from __future__ import annotations

import fnmatch
import json
import math
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from tabayyun.connectors import NetPolicy
from tabayyun.connectors.pi_web_api.values import parse_time

FIXTURES = Path(__file__).parent / "fixtures" / "pi_web_api"
HOST = "pi.example.com"
BASE_URL = f"https://{HOST}/piwebapi"
PUBLIC_IP = "93.184.215.14"
MINUTE_NS = 60 * 1_000_000_000
DAY_MINUTES = 1440
FLOW = "\\\\PISRV01\\FIC101.PV"
LEVEL = "\\\\PISRV01\\LIC201.PV"
PUMP = "\\\\PISRV01\\PMP301.STATE"
FLOW_ATTRIBUTE = "\\\\AFSRV01\\Plant\\Area1\\FIC101|Flow"
CONFIG = {"base_url": BASE_URL, "data_server": "\\\\PISRV01"}
CREDENTIALS = {"kind": "basic", "username": "PLANT\\svc-tabayyun", "password": "fixture-only"}


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def stamp(ts_ns: int) -> str:
    return datetime.fromtimestamp(ts_ns // 1_000_000_000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _noise(ts_ns: int) -> float:
    return ((ts_ns // MINUTE_NS) * 2654435761 % 1000) / 1000 - 0.5


def _analog(ts_ns: int, base: float, amplitude: float) -> float:
    minute = (ts_ns // MINUTE_NS) % DAY_MINUTES
    return round(base + amplitude * math.sin(2 * math.pi * minute / DAY_MINUTES) + _noise(ts_ns), 4)


def _good(value: Any) -> dict[str, Any]:
    return {
        "Value": value,
        "UnitsAbbreviation": "",
        "Good": True,
        "Questionable": False,
        "Substituted": False,
        "Annotated": False,
    }


class FakePiWebApi:
    """The answers of one small PI System; see the module docstring."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.fail_status: int | None = None
        self.connected = True
        self.deleted: set[str] = set()
        self.items = load("recorded_items.json")
        self.points = {p["Path"]: p for p in load("points.json")}
        self.attributes = {a["Path"]: a for a in load("attributes.json")}
        self.traits = load("traits.json")
        self.streams: dict[str, Callable[[int], Iterator[dict[str, Any]]]] = {
            "F1DPFIC101PV": self._flow,
            "F1AbEAFFIC101FLOW": self._flow,
            "F1DPLIC201PV": lambda ts: iter([_good(_analog(ts, 5.0, 1.0))]),
            "F1DPPMP301STATE": self._pump,
        }

    # Generated streams: the items at one minute.

    def _flow(self, ts_ns: int) -> Iterator[dict[str, Any]]:
        minute = (ts_ns // MINUTE_NS) % DAY_MINUTES
        special = {100: "questionable", 200: "substituted", 300: "io_timeout", 400: "error"}.get(minute)
        if special:
            yield dict(self.items[special])
            return
        if 120 <= minute < 180:
            yield _good(42.0)
            return
        value = _analog(ts_ns, 50.0, 10.0)
        yield _good(value)
        if minute == 999:
            yield _good(value + 0.5)

    def _pump(self, ts_ns: int) -> Iterator[dict[str, Any]]:
        if ts_ns % (360 * MINUTE_NS) == 0:
            hour = (ts_ns // (60 * MINUTE_NS)) % 24
            yield dict(self.items["pump_running" if hour in (0, 12) else "pump_stopped"])

    # Transport

    def handler(self, request: httpx.Request) -> httpx.Response:
        """The MockTransport entry point."""
        self.requests.append(request)
        if self.fail_status is not None:
            return httpx.Response(self.fail_status, json={"Errors": [f"fake failure {self.fail_status}"]})
        status, body = self._route(request.method, request.url.path, request.url.params, request.content)
        return httpx.Response(status, json=body)

    def _route(self, method: str, path: str, params: httpx.QueryParams, content: bytes) -> tuple[int, Any]:
        route = path.removeprefix("/piwebapi/").strip("/")
        parts = route.split("/")
        if method == "POST" and route == "batch":
            return self._batch(json.loads(content))
        if method != "GET":
            return 405, {"Errors": ["method not allowed"]}
        if route == "dataservers":
            if params.get("path", "").upper() != "\\\\PISRV01":
                return 404, {"Errors": [f"Not found: '{params.get('path')}'."]}
            return 200, {**load("dataserver.json"), "IsConnected": self.connected}
        if route == "assetdatabases":
            if params.get("path", "").upper() != "\\\\AFSRV01\\PLANT":
                return 404, {"Errors": [f"Not found: '{params.get('path')}'."]}
            return 200, load("assetdatabase.json")
        if route == "points":
            point = self.points.get(params.get("path", ""))
            return (200, point) if point else (404, {"Errors": ["PI Point not found."]})
        if route == "attributes":
            attribute = self.attributes.get(params.get("path", ""))
            return (200, attribute) if attribute else (404, {"Errors": ["Attribute not found."]})
        if parts[0] == "dataservers" and parts[2:] == ["points"]:
            return 200, {"Items": self._filter(self.points.values(), params, "nameFilter"), "Links": {}}
        if parts[0] == "assetdatabases" and parts[2:] == ["elementattributes"]:
            return 200, {
                "Items": self._filter(self.attributes.values(), params, "attributeNameFilter"),
                "Links": {},
            }
        if parts[0] == "points" and parts[2:] == ["attributes"]:
            return 200, load("point_attributes.json")
        if parts[0] == "attributes" and parts[2:] == ["attributes"]:
            return 200, self.traits.get(parts[1], {"Items": [], "Links": {}})
        if parts[0] == "streams" and parts[2:] == ["value"]:
            value = self.traits["values"].get(parts[1])
            return (200, value) if value else (404, {"Errors": ["Stream not found."]})
        if parts[0] == "streams" and parts[2:] == ["recorded"]:
            return self._recorded(parts[1], params)
        return 404, {"Errors": [f"no fake route for {route}"]}

    @staticmethod
    def _filter(items: Any, params: httpx.QueryParams, key: str) -> list[dict[str, Any]]:
        pattern = params.get(key, "*").lower()
        found = [i for i in items if fnmatch.fnmatchcase(i["Name"].lower(), pattern)]
        return found[: int(params.get("maxCount", 1000))]

    def _recorded(self, web_id: str, params: httpx.QueryParams) -> tuple[int, Any]:
        if web_id in self.deleted or web_id not in self.streams:
            return 410, {"Errors": [f"The stream {web_id} no longer exists."]}
        if params.get("boundaryType") != "Inside":
            return 400, {"Errors": ["the connector should ask for Inside"]}
        start, end = parse_time(params["startTime"]), parse_time(params["endTime"])
        max_count = int(params.get("maxCount", 1000))
        items: list[dict[str, Any]] = []
        minute = -(-start // MINUTE_NS) * MINUTE_NS
        while minute <= end and len(items) < max_count:
            for item in self.streams[web_id](minute):
                if len(items) < max_count:
                    items.append({"Timestamp": stamp(minute), **item})
            minute += MINUTE_NS
        return 200, {"Items": items, "UnitsAbbreviation": "", "Links": {}}

    def _batch(self, requests: dict[str, Any]) -> tuple[int, Any]:
        out: dict[str, Any] = {}
        for key, sub in requests.items():
            url = urlsplit(sub["Resource"])
            if url.hostname != HOST:
                out[key] = {"Status": 400, "Headers": {}, "Content": {"Errors": ["other host"]}}
                continue
            status, body = self._route(sub["Method"], url.path, httpx.QueryParams(url.query), b"")
            out[key] = {"Status": status, "Headers": {"Content-Type": "application/json"}, "Content": body}
        return 207, out


def net_policy(fake: FakePiWebApi) -> NetPolicy:
    """A policy resolving the fake's host to a public address, over the fake's transport."""

    async def resolve(host: str, port: int) -> list[str]:
        if host != HOST:
            raise OSError("no such host")
        return [PUBLIC_IP]

    return NetPolicy(resolver=resolve, transport=httpx.MockTransport(fake.handler))
