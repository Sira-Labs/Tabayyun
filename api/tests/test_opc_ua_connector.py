"""The `opc_ua` connector against an in-process asyncua server (spec 023): connecting with a
pinned server certificate, logins, search, history with continuation points and quality, point
failures, and describe."""

from __future__ import annotations

import math
import uuid

import pyarrow as pa
import pytest

from opcua_server import DAY0, MINUTES, USERS, client_identity, net_policy, running_server
from tabayyun import connectors
from tabayyun.connectors import AuthError, ConnectorError, FetchedBatch, PointFailure, PointRef

START = int(DAY0.timestamp()) * 1_000_000_000
DAY = MINUTES * 60 * 1_000_000_000
MINUTE = 60 * 1_000_000_000


def credentials(kind="anonymous", **extra):
    cert, key = client_identity()
    return {"kind": kind, "client_certificate_pem": cert, "client_private_key_pem": key, **extra}


def make(fixture, creds=None, **config):
    return connectors.build("opc_ua", fixture.config(**config), creds or credentials(), net_policy())


async def collect(conn, refs, start=START, end=START + DAY):
    tables: dict[uuid.UUID, list[pa.Table]] = {}
    failures = []
    async for item in conn.fetch(refs, start, end):
        if isinstance(item, PointFailure):
            failures.append(item)
        else:
            assert isinstance(item, FetchedBatch)
            tables.setdefault(item.series_id, []).append(item.table)
    return {k: pa.concat_tables(v) for k, v in tables.items()}, failures


@pytest.fixture
async def opcua():
    async with running_server() as fixture:
        yield fixture


# Connecting


async def test_check_with_the_pinned_certificate(opcua):
    await make(opcua).check()
    await make(opcua, security_mode="Sign").check()


async def test_an_unpinned_server_names_its_thumbprint(opcua):
    conn = make(opcua, server_certificate_sha256=None)
    with pytest.raises(
        ConnectorError, match=f"server certificate not pinned: SHA-256 {opcua.thumbprint}"
    ) as exc:
        await conn.check()
    assert exc.value.retryable is False and "Fixture OPC UA server" in str(exc.value)


async def test_a_wrong_pin_is_refused(opcua):
    with pytest.raises(ConnectorError, match="does not match the pinned") as exc:
        await make(opcua, server_certificate_sha256="ab" * 32).check()
    assert exc.value.retryable is False


async def test_username_login_and_a_wrong_password(opcua):
    [(user, password)] = USERS.items()
    await make(opcua, credentials("username", username=user, password=password)).check()
    with pytest.raises(AuthError, match="OPC UA server refused the connection"):
        await make(opcua, credentials("username", username=user, password="wrong")).check()


async def test_anonymous_refused_when_the_server_wants_a_user():
    async with running_server(allow_anonymous=False) as fixture:
        with pytest.raises(AuthError, match="refused the connection"):
            await make(fixture).check()


async def test_a_secure_policy_needs_a_client_certificate(opcua):
    conn = connectors.build("opc_ua", opcua.config(), {"kind": "anonymous"}, net_policy())
    with pytest.raises(AuthError, match="no client certificate"):
        await conn.check()


async def test_insecure_only_when_allowed(opcua):
    conn = connectors.build(
        "opc_ua",
        opcua.config(security_policy="None", allow_insecure=True, server_certificate_sha256=None),
        None,
        net_policy(),
    )
    await conn.check()


async def test_unreachable_is_retryable_and_refused_targets_never_connect(opcua):
    gone = make(opcua, endpoint_url="opc.tcp://opcua.example.com:1/tabayyun")
    with pytest.raises(ConnectorError, match="cannot reach the OPC UA server") as exc:
        await gone.check()
    assert exc.value.retryable is True
    with pytest.raises(ConnectorError, match="does not resolve"):
        await make(opcua, endpoint_url=f"opc.tcp://elsewhere.example.com:{opcua.port}").check()


# Search


async def test_search_finds_variables_by_name_and_skips_properties(opcua):
    found = await make(opcua).search("fic", 10)
    assert [(p.external_id, p.name, p.description) for p in found] == [
        (opcua.node_id("FIC101.PV"), "FIC101.PV", "Plant/Area1/FIC101.PV"),
        (opcua.node_id("FIC201.PV"), "FIC201.PV", "Plant/Area2/Line/FIC201.PV"),
    ]
    assert await make(opcua).search("EURange", 10) == []
    assert [p.name for p in await make(opcua).search("*.PV", 2)] == ["FIC101.PV", "LIC201.PV"]


async def test_search_keeps_to_depth_and_root(opcua):
    shallow = await make(opcua, browse_depth=3).search("fic", 10)
    assert [p.name for p in shallow] == ["FIC101.PV"]
    rooted = await make(opcua, browse_root=f"ns={opcua.ns};s=Area2").search("*", 10)
    assert [(p.name, p.description) for p in rooted] == [("FIC201.PV", "Line/FIC201.PV")]


# Fetch


async def test_a_day_with_quality(opcua):
    flow, pump = uuid.uuid4(), uuid.uuid4()
    tables, failures = await collect(
        make(opcua),
        [PointRef(flow, opcua.node_id("FIC101.PV")), PointRef(pump, opcua.node_id("PumpRunning"))],
    )
    assert failures == []
    table = tables[flow]
    assert table.num_rows == MINUTES
    ts = table.column("ts").to_pylist()
    assert ts[0] == START and ts[-1] == START + (MINUTES - 1) * MINUTE
    values, qualities = table.column("value").to_pylist(), table.column("quality").to_pylist()
    assert qualities[100] == "uncertain" and qualities[200] == "uncertain"  # LocalOverride, Clamped
    assert qualities[300] == "estimated" and qualities[400] == "uncertain"
    assert qualities[500] == "bad" and math.isnan(values[500])
    assert qualities.count("good") == MINUTES - 5 and values[150] == 42.0
    assert tables[pump].column("value").to_pylist() == [1.0, 0.0, 1.0, 0.0]


async def test_continuation_points_give_the_same_rows(opcua):
    flow = uuid.uuid4()
    whole, _ = await collect(make(opcua), [PointRef(flow, opcua.node_id("FIC101.PV"))])
    async with running_server(page=500) as paged_server:
        paged, _ = await collect(make(paged_server), [PointRef(flow, paged_server.node_id("FIC101.PV"))])
    assert paged[flow].column("ts").to_pylist() == whole[flow].column("ts").to_pylist()
    # asyncua's server cuts at NumValuesPerNode without a continuation point (the standard asks
    # for one), so `max_values` is not exercised here; its page size stands in for it.


async def test_unreadable_nodes_are_point_failures(opcua):
    level, cold, ghost, bad, folder = (uuid.uuid4() for _ in range(5))
    tables, failures = await collect(
        make(opcua),
        [
            PointRef(level, opcua.node_id("LIC201.PV")),
            PointRef(cold, opcua.node_id("TI102.PV")),
            PointRef(ghost, opcua.node_id("NOPE")),
            PointRef(bad, "not-a-node-id"),
            PointRef(folder, opcua.node_id("Area1")),
        ],
    )
    assert set(tables) == {level} and tables[level].num_rows == MINUTES
    messages = {f.series_id: f.message for f in failures}
    assert messages[cold] == f"{opcua.node_id('TI102.PV')}: no history read access"
    assert messages[ghost] == f"{opcua.node_id('NOPE')}: BadNodeIdUnknown"
    assert messages[bad] == "not a node id: not-a-node-id"
    assert messages[folder] == f"{opcua.node_id('Area1')}: not a variable"


async def test_the_window_is_half_open(opcua):
    flow = uuid.uuid4()
    tables, _ = await collect(
        make(opcua), [PointRef(flow, opcua.node_id("LIC201.PV"))], START + MINUTE, START + 3 * MINUTE
    )
    assert tables[flow].column("ts").to_pylist() == [START + MINUTE, START + 2 * MINUTE]


# Describe


async def test_describe(opcua):
    flow, level, ghost = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    got = await make(opcua).describe(
        [
            PointRef(flow, opcua.node_id("FIC101.PV")),
            PointRef(level, opcua.node_id("LIC201.PV")),
            PointRef(ghost, opcua.node_id("NOPE")),
        ]
    )
    f = got[0].metadata
    assert (f.unit, f.description, f.asset_path) == ("m³/h", "Inlet flow", "Plant/Area1")
    assert (f.physical_min, f.physical_max, f.operational_min, f.operational_max) == (0.0, 120.0, 10.0, 90.0)
    assert f.extra["data_type"] == "Double" and f.extra["history_read"] is True
    assert f.extra["node_id"] == opcua.node_id("FIC101.PV")
    lv = got[1].metadata
    assert (lv.unit, lv.physical_min, lv.operational_max) == ("m", None, None)
    assert (got[2].metadata, got[2].error) == (None, f"{opcua.node_id('NOPE')}: BadNodeIdUnknown")


def test_registered():
    assert connectors.get("opc_ua").type == "opc_ua"
