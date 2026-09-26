#!/usr/bin/env python3
"""Load example series into a Tabayyun install and run every check family on them.

The data is synthetic and deterministic, with known faults planted, so a fresh install
(staging, a local stack, a restored database) can be filled and checked in one command:

    python3 deploy/examples/seed.py --url https://tabayyun-stg.siralabs.org

It uploads the series below through `POST /api/runs`, creates one series group per
cross-series check, puts all group members in a dataset, runs it, and prints which checks
found something. It exits non-zero when a run fails or a planted fault goes unfound.
Standard library only. Run it once per install (a second run re-uploads the series and adds
another set of groups and a dataset), and never against production, whose data belongs to
real people.

An install with sign-in (spec 013) needs a session: sign in with the browser as a member of
the org, copy the value of the `__Host-tby_session` cookie (developer tools → Application →
Cookies) and pass it in the environment, not on the command line:

    TABAYYUN_SESSION='<cookie value>' python3 deploy/examples/seed.py --url https://...

| Series | Planted fault | Expected check |
|---|---|---|
| `demo-flow` | gap, flatline, nulls, negatives, beyond 200, exact duplicates | single-series |
| `site-load` | daily pattern lost for the last 10 days | `tby.seasonality_break` |
| `outdoor-temp`, `hvac-load` | load stops following temperature on day 29 | `tby.correlation_break` |
| `flow-a`, `flow-b`, `flow-c` | `flow-c` reads 8 % high on days 31–33 | `tby.redundant_disagreement` |
| `feeder-in`, `out-1..3` | `out-2` under-reads by 30 % on days 36–37 | `tby.balance_residual` |
"""

# A command-line tool: it prints its progress, opens the URL the operator names, and its
# random numbers only shape synthetic data.
# ruff: noqa: T201, S310, S311

from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import time
import uuid
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from urllib import error, request
from urllib.parse import urlsplit

DAYS = 42
STEP = timedelta(minutes=15)
POINTS = DAYS * 24 * 4
RUN_TIMEOUT_S = 300
DONE = {"succeeded", "failed"}

Row = tuple[datetime, float | None]


class SeedError(RuntimeError):
    """The install answered in a way the seed cannot continue from."""


class Api:
    """Minimal JSON and multipart client for the Tabayyun API."""

    def __init__(self, base_url: str, session: str | None = None) -> None:
        self.base = base_url.rstrip("/")
        self.session = session
        if session and urlsplit(self.base).scheme.lower() != "https":
            # The session is the operator's credential: never over plain HTTP.
            raise SeedError("TABAYYUN_SESSION needs an https:// --url")

    def _send(self, method: str, path: str, body: bytes | None, content_type: str | None) -> dict:
        req = request.Request(self.base + path, data=body, method=method)
        # The api refuses unsafe requests without it (CSRF guard, spec 013).
        req.add_header("X-Tabayyun-Request", "1")
        if self.session:
            # Unredirected: a redirect to another host must not carry the session.
            req.add_unredirected_header("Cookie", f"__Host-tby_session={self.session}")
        if content_type:
            req.add_header("Content-Type", content_type)
        try:
            with request.urlopen(req, timeout=60) as resp:
                raw = resp.read()
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            if exc.code in (401, 403) and any(c in detail for c in ("not_authenticated", "no_access")):
                detail += " (sign in and set TABAYYUN_SESSION, see the docstring)"
            raise SeedError(f"{method} {path}: HTTP {exc.code}: {detail}") from exc
        return json.loads(raw) if raw else {}

    def get(self, path: str) -> dict:
        return self._send("GET", path, None, None)

    def post_json(self, path: str, payload: dict) -> dict:
        return self._send("POST", path, json.dumps(payload).encode(), "application/json")

    def post_form(self, path: str, fields: dict[str, str], filename: str, data: bytes) -> dict:
        boundary = uuid.uuid4().hex
        parts: list[bytes] = []
        for name, value in fields.items():
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
            )
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            "Content-Type: text/csv\r\n\r\n".encode()
            + data
            + b"\r\n"
        )
        parts.append(f"--{boundary}--\r\n".encode())
        return self._send("POST", path, b"".join(parts), f"multipart/form-data; boundary={boundary}")


# --- synthetic series -------------------------------------------------------------------


def timeline(end: datetime) -> list[datetime]:
    start = end - STEP * POINTS
    return [start + STEP * i for i in range(POINTS)]


def day_of(ts: list[datetime], i: int) -> float:
    return (ts[i] - ts[0]) / timedelta(days=1)


def daily_shape(t: datetime) -> float:
    hour = t.hour + t.minute / 60
    weekend = 0.8 if t.weekday() >= 5 else 1.0
    return weekend * (1 + 0.6 * math.sin((hour - 9) / 24 * 2 * math.pi))


def site_load(ts: list[datetime], rng: random.Random) -> list[Row]:
    """Daily and weekly pattern, lost (flat plus noise) for the last 10 days."""
    out = []
    for i, t in enumerate(ts):
        base = 50 * daily_shape(t) if day_of(ts, i) < DAYS - 10 else 50
        out.append((t, round(base + rng.gauss(0, 2), 3)))
    return out


def temp_and_hvac(ts: list[datetime], rng: random.Random) -> tuple[list[Row], list[Row]]:
    """Cooling load follows outdoor temperature, then decouples from day 29 on."""
    temp, hvac = [], []
    walk = 0.0
    for i, t in enumerate(ts):
        walk = 0.98 * walk + rng.gauss(0, 0.3)
        hour = t.hour + t.minute / 60
        c = 18 + 6 * math.sin((hour - 15) / 24 * 2 * math.pi) + walk
        temp.append((t, round(c, 3)))
        if day_of(ts, i) < 28:
            load = 5 + 3 * max(c - 16, 0) + rng.gauss(0, 1)
        else:
            load = 20 + rng.gauss(0, 6)
        hvac.append((t, round(load, 3)))
    return temp, hvac


def redundant_flows(ts: list[datetime], rng: random.Random) -> list[list[Row]]:
    """Three meters on one pipe; the third reads 8 % high on days 31–33."""
    a, b, c = [], [], []
    for i, t in enumerate(ts):
        true = 120 * daily_shape(t) + 20
        a.append((t, round(true + rng.gauss(0, 0.5), 3)))
        b.append((t, round(true + rng.gauss(0, 0.5), 3)))
        drift = 1.08 if 30 <= day_of(ts, i) < 33 else 1.0
        c.append((t, round(true * drift + rng.gauss(0, 0.5), 3)))
    return [a, b, c]


def balance(ts: list[datetime], rng: random.Random) -> tuple[list[Row], list[list[Row]]]:
    """Feeder = sum of three outputs plus 2 % losses; `out-2` under-reads on days 36–37."""
    feeder, outs = [], [[], [], []]
    for i, t in enumerate(ts):
        shape = daily_shape(t)
        true = [40 * shape + 10, 25 * shape + 5, 15 * shape + 8]
        feeder.append((t, round(sum(true) * 1.02 + rng.gauss(0, 0.2), 3)))
        for k, value in enumerate(true):
            if k == 1 and 35 <= day_of(ts, i) < 37:
                value *= 0.7
            outs[k].append((t, round(value + rng.gauss(0, 0.2), 3)))
    return feeder, outs


def demo_flow(ts: list[datetime], rng: random.Random) -> list[Row]:
    """A flow meter with the single-series faults of `make demo`."""
    rows: list[Row] = []
    for i, t in enumerate(ts):
        d = day_of(ts, i)
        if 5 <= d < 5.5:  # gap: half a day missing
            continue
        value: float | None = 80 * daily_shape(t) + rng.gauss(0, 1.5)
        if 10 <= d < 11:  # flatline: stuck at one value for a day
            value = 63.2
        elif 15 <= d < 15.1:  # nulls
            value = None
        elif 20 <= d < 20.2:  # negative flow
            value = -abs(value or 0) - 5
        elif 25 <= d < 25.1:  # beyond physical_max 200
            value = 450.0
        rows.append((t, None if value is None else round(value, 3)))
        if 30 <= d < 30.05:  # duplicate timestamps
            rows.append((t, None if value is None else round(value, 3)))
    return rows


def to_csv(rows: Iterable[Row]) -> bytes:
    lines = ["ts,value"]
    for t, v in rows:
        lines.append(f"{t.strftime('%Y-%m-%dT%H:%M:%SZ')},{'' if v is None else v}")
    return ("\n".join(lines) + "\n").encode()


# --- API steps --------------------------------------------------------------------------


def wait_run(api: Api, run_id: str) -> dict:
    deadline = time.monotonic() + RUN_TIMEOUT_S
    while time.monotonic() < deadline:
        run = api.get(f"/api/runs/{run_id}")
        if run["status"] in DONE:
            return run
        time.sleep(3)
    raise SeedError(f"run {run_id} did not finish within {RUN_TIMEOUT_S} s")


def upload(api: Api, name: str, rows: list[Row], unit: str, extra: dict[str, str] | None = None) -> dict:
    fields = {"series_id": name, "unit": unit, **(extra or {})}
    created = api.post_form("/api/runs", fields, f"{name}.csv", to_csv(rows))
    run = wait_run(api, created["id"])
    print(f"  upload {name:13} run {run['id']} {run['status']}")
    return run


def series_ids(api: Api) -> dict[str, str]:
    """external id → series uuid, over all pages."""
    out: dict[str, str] = {}
    cursor = ""
    while True:
        page = api.get("/api/series?limit=200" + (f"&cursor={cursor}" if cursor else ""))
        for item in page["items"]:
            out[item["external_id"]] = item["id"]
        cursor = page.get("next_cursor") or ""
        if not cursor:
            return out


def findings_of_run(api: Api, run_id: str) -> list[dict]:
    """Every finding the run created or saw again, over all pages."""
    found: list[dict] = []
    cursor = ""
    while True:
        page = api.get(f"/api/findings?run_id={run_id}&limit=200" + (f"&cursor={cursor}" if cursor else ""))
        found += page["items"]
        cursor = page.get("next_cursor") or ""
        if not cursor:
            return found


# Planted faults: (series, check, first day, last day) of the timeline. A fault counts as found
# when a finding of that check on that series overlaps the interval.
PLANTED = [
    ("demo-flow", "tby.completeness", 5.0, 5.5),  # half a day missing
    ("demo-flow", "tby.flatline", 10.0, 11.0),
    ("demo-flow", "tby.completeness", 15.0, 15.1),  # nulls
    ("demo-flow", "tby.non_negative", 20.0, 20.2),
    ("demo-flow", "tby.physical_range", 20.0, 20.2),  # below physical_min 0
    ("demo-flow", "tby.physical_range", 25.0, 25.1),  # above physical_max 200
    ("site-load", "tby.seasonality_break", DAYS - 10, DAYS),
    ("hvac-load", "tby.correlation_break", 28.0, DAYS),
    ("flow-c", "tby.redundant_disagreement", 30.0, 33.0),
    ("out-2", "tby.balance_residual", 35.0, 37.0),
]


def missing_faults(findings: list[dict], names: dict[str, str], start: datetime) -> list[str]:
    """The planted faults no finding covers; `names` maps series uuid to external id."""

    def ns(day: float) -> int:
        return int((start + timedelta(days=day)).timestamp() * 1e9)

    missing = []
    for series, check, first, last in PLANTED:
        lo, hi = ns(first), ns(last)
        if not any(
            names.get(f["series_id"]) == series
            and f["check_id"] == check
            and f["window"]["start"] < hi
            and f["window"]["end"] > lo
            for f in findings
        ):
            missing.append(f"{check} on {series}, days {first:g}-{last:g}")
    return missing


def exact_duplicates(api: Api, series_uuid: str) -> float:
    """The latest `exact_duplicates` metric: exact duplicates are counted, not a finding."""
    items = api.get(f"/api/series/{series_uuid}/metrics?name=exact_duplicates&limit=1")["items"]
    return float(items[0]["value"]) if items else 0.0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--url", required=True, help="base URL of the install, e.g. https://tabayyun-stg.siralabs.org"
    )
    parser.add_argument("--seed", type=int, default=7, help="random seed of the synthetic data")
    args = parser.parse_args(argv)

    api = Api(args.url, os.environ.get("TABAYYUN_SESSION") or None)
    version = api.get("/api/version")
    commit, schema = version.get("commit", "?")[:7], version.get("schema_revision")
    print(f"install {args.url}: commit {commit}, schema {schema}")

    rng = random.Random(args.seed)
    end = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    ts = timeline(end)
    temp, hvac = temp_and_hvac(ts, rng)
    flows = redundant_flows(ts, rng)
    feeder, outs = balance(ts, rng)

    uploads: dict[str, tuple[list[Row], str, dict[str, str] | None]] = {
        "demo-flow": (demo_flow(ts, rng), "m3/h", {"physical_min": "0", "physical_max": "200"}),
        "site-load": (site_load(ts, rng), "kW", None),
        "outdoor-temp": (temp, "degC", None),
        "hvac-load": (hvac, "kW", None),
        "flow-a": (flows[0], "m3/h", None),
        "flow-b": (flows[1], "m3/h", None),
        "flow-c": (flows[2], "m3/h", None),
        "feeder-in": (feeder, "kW", None),
        "out-1": (outs[0], "kW", None),
        "out-2": (outs[1], "kW", None),
        "out-3": (outs[2], "kW", None),
    }
    print("uploading series")
    findings: list[dict] = []
    failed = False
    for name, (rows, unit, extra) in uploads.items():
        run = upload(api, name, rows, unit, extra)
        failed |= run["status"] != "succeeded"
        findings += findings_of_run(api, run["id"])

    ids = series_ids(api)
    member: Callable[[str, str], dict[str, str]] = lambda name, role="member": {  # noqa: E731
        "series_id": ids[name],
        "role": role,
    }
    groups = [
        {
            "name": "HVAC follows temperature",
            "kind": "related",
            "members": [member("hvac-load"), member("outdoor-temp")],
        },
        {
            "name": "Pipe flow meters",
            "kind": "redundant",
            "members": [member("flow-a"), member("flow-b"), member("flow-c")],
        },
        {
            "name": "Feeder balance",
            "kind": "balance",
            "members": [
                member("feeder-in", "input"),
                member("out-1", "output"),
                member("out-2", "output"),
                member("out-3", "output"),
            ],
        },
    ]
    print("creating series groups")
    in_groups: list[str] = []
    for body in groups:
        group = api.post_json("/api/series-groups", body)
        print(f"  {group['kind']:9} {group['name']} ({group['id']})")
        in_groups += [m["series_id"] for m in body["members"]]

    start = ts[0].strftime("%Y-%m-%dT%H:%M:%SZ")
    stop = (ts[-1] + STEP).strftime("%Y-%m-%dT%H:%M:%SZ")
    dataset = api.post_json(
        "/api/datasets",
        {"name": "Example site", "series_ids": in_groups, "window": {"start": start, "end": stop}},
    )
    print(f"dataset {dataset['name']} ({dataset['id']}), {len(in_groups)} series")
    created = api.post_json("/api/runs", {"dataset_id": dataset["id"]})
    run = wait_run(api, created["id"])
    print(f"  dataset run {run['id']} {run['status']}" + (f": {run['error']}" if run.get("error") else ""))
    failed |= run["status"] != "succeeded"
    findings += findings_of_run(api, run["id"])

    names = {uuid_: name for name, uuid_ in ids.items()}
    missing = missing_faults(findings, names, ts[0])
    duplicates = exact_duplicates(api, ids["demo-flow"])
    if duplicates < 1:
        missing.append("exact_duplicates metric on demo-flow")
    print(f"planted faults: {len(PLANTED) + 1 - len(missing)} of {len(PLANTED) + 1} found")
    for item in missing:
        print(f"  MISSING {item}")
    print("checks with findings: " + ", ".join(sorted({f["check_id"] for f in findings})))
    return 1 if failed or missing else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SeedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)
