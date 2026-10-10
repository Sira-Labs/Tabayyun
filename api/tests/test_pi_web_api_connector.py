"""The `pi_web_api` connector against the fake PI Web API (spec 022): check, search, fetch with
paging, quality and point failures, describe, and what every request carries."""

from __future__ import annotations

import asyncio
import base64
import math
import uuid
from datetime import UTC, datetime

import pyarrow as pa
import pytest

from pi_fake import (
    CONFIG,
    CREDENTIALS,
    FLOW,
    FLOW_ATTRIBUTE,
    LEVEL,
    MINUTE_NS,
    PUMP,
    FakePiWebApi,
    net_policy,
)
from tabayyun import connectors
from tabayyun.connectors import AuthError, ConnectorError, FetchedBatch, PointFailure, PointRef
from tabayyun.connectors.pi_web_api.values import parse_time

DAY0 = int(datetime(2026, 9, 1, tzinfo=UTC).timestamp()) * 1_000_000_000
DAY = 1440 * MINUTE_NS


@pytest.fixture
def fake() -> FakePiWebApi:
    return FakePiWebApi()


def make(fake, credentials=CREDENTIALS, **config):
    return connectors.build("pi_web_api", {**CONFIG, **config}, credentials, net_policy(fake))


def collect(conn, refs, start=DAY0, end=DAY0 + DAY):
    async def go():
        return [b async for b in conn.fetch(refs, start, end)]

    out = asyncio.run(go())
    tables: dict[uuid.UUID, list[pa.Table]] = {}
    failures = []
    for item in out:
        if isinstance(item, PointFailure):
            failures.append(item)
        else:
            assert isinstance(item, FetchedBatch)
            tables.setdefault(item.series_id, []).append(item.table)
    return {k: pa.concat_tables(v) for k, v in tables.items()}, failures


# Check


def test_check_succeeds_and_every_request_carries_csrf_header_and_basic_auth(fake):
    asyncio.run(make(fake).check())
    [request] = fake.requests
    assert request.url.path == "/piwebapi/dataservers" and request.url.params["path"] == "\\\\PISRV01"
    assert request.headers["x-requested-with"] == "tabayyun"
    assert request.headers["accept"] == "application/json"
    expected = base64.b64encode(b"PLANT\\svc-tabayyun:fixture-only").decode()
    assert request.headers["authorization"] == f"Basic {expected}"
    assert request.headers["host"] == "pi.example.com"


def test_check_with_bearer_and_asset_database(fake):
    conn = make(fake, {"kind": "bearer", "token": "abc.def.ghi"}, asset_database="\\\\AFSRV01\\Plant")
    asyncio.run(conn.check())
    assert [r.url.path for r in fake.requests] == ["/piwebapi/dataservers", "/piwebapi/assetdatabases"]
    assert all(r.headers["authorization"] == "Bearer abc.def.ghi" for r in fake.requests)


@pytest.mark.parametrize(
    ("setup", "error", "retryable", "message"),
    [
        (lambda f: setattr(f, "fail_status", 401), AuthError, False, "PI Web API refused the credentials"),
        (lambda f: setattr(f, "fail_status", 403), AuthError, False, "PI Web API refused the credentials"),
        (lambda f: setattr(f, "fail_status", 503), ConnectorError, True, "PI Web API answered 503"),
        (lambda f: setattr(f, "fail_status", 429), ConnectorError, True, "PI Web API answered 429"),
        (lambda f: setattr(f, "fail_status", 400), ConnectorError, False, "fake failure 400"),
        (lambda f: setattr(f, "connected", False), ConnectorError, True, "data server not connected"),
    ],
)
def test_check_failures(fake, setup, error, retryable, message):
    setup(fake)
    with pytest.raises(error, match=message) as exc:
        asyncio.run(make(fake).check())
    assert exc.value.retryable is retryable


def test_check_names_a_missing_server_or_database(fake):
    with pytest.raises(ConnectorError, match="data server not found"):
        asyncio.run(make(fake, data_server="\\\\OTHER").check())
    with pytest.raises(ConnectorError, match="asset database not found"):
        asyncio.run(make(fake, asset_database="\\\\AFSRV01\\Other").check())


def test_no_credentials_is_an_auth_error(fake):
    with pytest.raises(AuthError, match="no credentials"):
        asyncio.run(make(fake, None).check())
    assert fake.requests == []


def test_a_refused_target_makes_no_request(fake):
    conn = connectors.build(
        "pi_web_api",
        {**CONFIG, "base_url": "https://elsewhere.example.com/piwebapi"},
        CREDENTIALS,
        net_policy(fake),
    )
    with pytest.raises(ConnectorError, match="does not resolve"):
        asyncio.run(conn.check())
    assert fake.requests == []


# Search


def test_search_wraps_a_word_in_wildcards(fake):
    found = asyncio.run(make(fake).search("fic", 10))
    assert [(p.external_id, p.name, p.unit, p.description) for p in found] == [
        (FLOW, "FIC101.PV", "m3/h", "Inlet flow")
    ]
    search = fake.requests[-1]
    assert search.url.path == "/piwebapi/dataservers/F1DSPISRV01/points"
    assert (search.url.params["nameFilter"], search.url.params["maxCount"]) == ("*fic*", "10")


def test_search_keeps_explicit_wildcards_and_adds_pi_point_attributes(fake):
    conn = make(fake, asset_database="\\\\AFSRV01\\Plant")
    found = asyncio.run(conn.search("*", 10))
    assert [p.external_id for p in found] == [FLOW, LEVEL, PUMP, FLOW_ATTRIBUTE]  # not the static attribute
    assert fake.requests[-1].url.params["attributeNameFilter"] == "*"
    assert len(asyncio.run(conn.search("*", 2))) == 2


# Fetch


def test_a_day_with_quality_and_duplicates(fake):
    flow, level = uuid.uuid4(), uuid.uuid4()
    tables, failures = collect(make(fake), [PointRef(flow, FLOW), PointRef(level, LEVEL)])
    assert failures == []
    table = tables[flow]
    assert table.num_rows == 1441  # a minute grid plus a second value at minute 999
    ts = table.column("ts").to_pylist()
    assert ts[0] == DAY0 and ts[-1] == DAY0 + 1439 * MINUTE_NS and ts == sorted(ts)
    by_minute = {}
    for t, v, q in zip(
        ts, table.column("value").to_pylist(), table.column("quality").to_pylist(), strict=True
    ):
        by_minute.setdefault((t - DAY0) // MINUTE_NS, []).append((v, q))
    assert by_minute[100] == [(51.5, "uncertain")] and by_minute[200] == [(49.0, "estimated")]
    assert math.isnan(by_minute[300][0][0]) and by_minute[300][0][1] == "bad"
    assert math.isnan(by_minute[400][0][0]) and by_minute[400][0][1] == "bad"
    assert len(by_minute[999]) == 2 and by_minute[999][1][0] == by_minute[999][0][0] + 0.5
    assert {v for v, _ in by_minute[150]} == {42.0}
    assert tables[level].num_rows == 1440
    # One batch lookup, then one recorded request per point.
    assert [r.method for r in fake.requests] == ["POST", "GET", "GET"]
    batch = fake.requests[0]
    assert batch.url.path == "/piwebapi/batch" and batch.headers["x-requested-with"] == "tabayyun"
    recorded = fake.requests[1]
    assert recorded.url.path == "/piwebapi/streams/F1DPFIC101PV/recorded"
    assert recorded.url.params["startTime"] == "2026-09-01T00:00:00.0000000Z"
    assert recorded.url.params["endTime"] == "2026-09-02T00:00:00.0000000Z"
    assert recorded.url.params["boundaryType"] == "Inside"


def test_paging_past_max_count_keeps_every_value_once(fake):
    flow = uuid.uuid4()
    whole, _ = collect(make(fake), [PointRef(flow, FLOW)])
    paged, _ = collect(make(fake, max_count=1000), [PointRef(flow, FLOW)])
    assert paged[flow].column("ts").to_pylist() == whole[flow].column("ts").to_pylist()
    assert paged[flow].column("quality").to_pylist() == whole[flow].column("quality").to_pylist()
    starts = [r.url.params["startTime"] for r in fake.requests if r.url.path.endswith("/recorded")][-2:]
    # The second page starts at minute 999, where the first page cut between two values.
    assert parse_time(starts[1]) == DAY0 + 999 * MINUTE_NS


def test_digital_states_by_code_and_web_ids_cached(fake):
    pump = uuid.uuid4()
    conn = make(fake)
    tables, _ = collect(conn, [PointRef(pump, PUMP)])
    assert tables[pump].column("value").to_pylist() == [1.0, 0.0, 1.0, 0.0]
    collect(conn, [PointRef(pump, PUMP)], DAY0 + DAY, DAY0 + 2 * DAY)
    assert [r.url.path for r in fake.requests].count("/piwebapi/batch") == 1


def test_af_attribute_reads_its_stream(fake):
    attr = uuid.uuid4()
    tables, failures = collect(make(fake), [PointRef(attr, FLOW_ATTRIBUTE)])
    assert failures == [] and tables[attr].num_rows == 1441
    assert fake.requests[1].url.path == "/piwebapi/streams/F1AbEAFFIC101FLOW/recorded"


def test_unknown_deleted_and_malformed_points_are_point_failures(fake):
    flow, ghost, deleted, bad = (uuid.uuid4() for _ in range(4))
    fake.deleted.add("F1DPLIC201PV")
    tables, failures = collect(
        make(fake),
        [
            PointRef(flow, FLOW),
            PointRef(ghost, "\\\\PISRV01\\NOPE"),
            PointRef(deleted, LEVEL),
            PointRef(bad, "FIC101.PV"),
        ],
    )
    assert set(tables) == {flow}
    messages = {f.series_id: f.message for f in failures}
    assert messages[ghost] == "not found in PI: \\\\PISRV01\\NOPE"
    assert messages[deleted].startswith(f"{LEVEL} not found: The stream F1DPLIC201PV no longer exists")
    assert messages[bad] == "not a PI path: FIC101.PV"


def test_server_errors_during_a_fetch_are_retryable(fake):
    conn = make(fake)
    fake.fail_status = 502
    with pytest.raises(ConnectorError) as exc:
        collect(conn, [PointRef(uuid.uuid4(), FLOW)])
    assert exc.value.retryable


# Describe


def test_describe_a_point_and_an_attribute(fake):
    point, attr, ghost = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    got = asyncio.run(
        make(fake).describe(
            [PointRef(point, FLOW), PointRef(attr, FLOW_ATTRIBUTE), PointRef(ghost, "\\\\PISRV01\\NOPE")]
        )
    )
    assert [d.series_id for d in got] == [point, attr, ghost]
    p = got[0].metadata
    assert (p.unit, p.description, p.physical_min, p.physical_max) == ("m3/h", "Inlet flow", 0.0, 200.0)
    assert (p.operational_min, p.operational_max, p.asset_path) == (None, None, None)
    assert p.extra["compdev"] == 0.2 and p.extra["excmax"] == 600 and p.extra["point_type"] == "Float32"
    assert "typicalvalue" not in p.extra and "pointsource" not in p.extra
    a = got[1].metadata
    assert (a.unit, a.description) == ("m3/h", "Inlet flow (AF)")
    assert (a.physical_min, a.physical_max, a.operational_min, a.operational_max) == (0.0, 120.0, 20.0, 90.0)
    assert a.asset_path == "\\\\AFSRV01\\Plant\\Area1\\FIC101"
    assert a.extra["limit_hihi"] == 110.0 and a.extra["compdev"] == 0.2
    assert (got[2].metadata, got[2].error) == (None, "\\\\PISRV01\\NOPE not found: PI Point not found.")
    # The point link's WebId is used, not its URL.
    assert "/piwebapi/points/F1DPFIC101PV/attributes" in [r.url.path for r in fake.requests]


def test_a_digital_point_has_no_physical_range(fake):
    [d] = asyncio.run(make(fake).describe([PointRef(uuid.uuid4(), PUMP)]))
    assert (d.metadata.physical_min, d.metadata.physical_max) == (None, None)
    assert d.metadata.extra["step"] is True and d.metadata.unit is None


def test_malformed_web_ids_are_refused(fake):
    fake.points[FLOW] = {**fake.points[FLOW], "WebId": "../dataservers"}
    with pytest.raises(ConnectorError, match="malformed WebId"):
        asyncio.run(make(fake).describe([PointRef(uuid.uuid4(), FLOW)]))
