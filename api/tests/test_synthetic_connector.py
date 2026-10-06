"""The synthetic connector (spec 021): determinism across fetches and windows, the faults, config
validation, search, and the registry's `build`."""

from __future__ import annotations

import asyncio
import uuid

import pyarrow as pa
import pytest

from tabayyun import connectors
from tabayyun.connectors import BATCH_SCHEMA, ConnectorError, NetPolicy, PointRef
from tabayyun.connectors.synthetic import ROWS_PER_BATCH, SPIKE_EVERY, grid

S = 1_000_000_000
DAY = 86_400 * S
T0 = 20_400 * DAY  # a midnight in 2025
SERIES = uuid.uuid4()


def make(**config):
    config.setdefault("points", [{"external_id": "flow", "base": 50, "amplitude": 10, "noise": 0.5}])
    return connectors.build("synthetic", config, None, NetPolicy())


def fetch(conn, start, end, ext="flow"):
    async def go():
        return [b async for b in conn.fetch([PointRef(SERIES, ext)], start, end)]

    batches = asyncio.run(go())
    return pa.concat_tables([b.table for b in batches]) if batches else None


def test_a_day_at_a_minute():
    table = fetch(make(), T0, T0 + DAY)
    assert table.schema == BATCH_SCHEMA and table.num_rows == 1440
    assert table.column("ts")[0].as_py() == T0 and set(table.column("quality").to_pylist()) == {"good"}
    assert all(39 < v < 61 for v in table.column("value").to_pylist())


def test_same_rows_whatever_the_windows():
    conn = make()
    whole = fetch(conn, T0, T0 + DAY)
    pieces = [fetch(conn, T0 + k * DAY // 4 + 7 * S, T0 + (k + 1) * DAY // 4 + 7 * S) for k in range(4)]
    joined = pa.concat_tables(pieces).slice(0, 1439)
    assert joined.column("ts").to_pylist() == whole.column("ts").to_pylist()[1:]
    assert joined.column("value").to_pylist() == whole.column("value").to_pylist()[1:]
    assert fetch(make(), T0, T0 + DAY).equals(whole)


def test_seed_and_point_change_the_noise():
    base = fetch(make(), T0, T0 + DAY).column("value").to_pylist()
    assert fetch(make(seed=1), T0, T0 + DAY).column("value").to_pylist() != base
    other = make(points=[{"external_id": "flow2", "base": 50, "amplitude": 10, "noise": 0.5}])
    assert fetch(other, T0, T0 + DAY, "flow2").column("value").to_pylist() != base


def test_faults():
    conn = make(
        points=[{"external_id": "flow", "amplitude": 10, "noise": 0.1, "faults": ["spikes", "flatline"]}]
    )
    table = fetch(conn, T0, T0 + DAY)
    values = table.column("value").to_pylist()
    assert len(set(values[120:180])) == 1  # 02:00 to 03:00 holds the 02:00 value
    assert len(set(values[180:240])) > 1
    stamps = table.column("ts").to_pylist()
    spikes = [ts for ts, v in zip(stamps, values, strict=True) if v > 60]
    assert spikes and all((ts // (60 * S)) % SPIKE_EVERY == 0 for ts in spikes)


def test_unknown_point_yields_nothing_and_long_spans_are_chunked():
    assert fetch(make(), T0, T0 + DAY, "nope") is None

    async def batches():
        return [b async for b in make(interval_s=1).fetch([PointRef(SERIES, "flow")], T0, T0 + DAY)]

    out = asyncio.run(batches())
    assert len(out) == -(-86_400 // ROWS_PER_BATCH) and sum(b.table.num_rows for b in out) == 86_400


def test_grid_starts_on_the_interval():
    assert list(grid(T0 + 1, T0 + 3 * 60 * S, 60 * S)) == [T0 + 60 * S, T0 + 120 * S]
    assert list(grid(T0, T0, 60 * S)) == []


def test_search():
    conn = make(points=[{"external_id": "pump.flow", "name": "Pump flow"}, {"external_id": "tank.level"}])
    found = asyncio.run(conn.search("FLOW", 10))
    assert [p.external_id for p in found] == ["pump.flow"] and found[0].name == "Pump flow"


@pytest.mark.parametrize(
    "config",
    [
        {"points": [{"external_id": "a"}, {"external_id": "a"}]},
        {"points": [{"external_id": "a", "faults": ["drift"]}]},
        {"points": [{"external_id": "bad id"}]},
        {"interval_s": 0},
        {"max_points": 5000},
        {"requests_per_second": 100},
        {"poll_interval_s": 10},
        {"surprise": True},
    ],
)
def test_invalid_config_names_the_field(config):
    with pytest.raises(ConnectorError, match="invalid stored settings") as err:
        connectors.build("synthetic", config, None, NetPolicy())
    assert not err.value.retryable


def test_registry():
    assert connectors.is_connector("synthetic") and not connectors.is_connector("upload")
    with pytest.raises(ConnectorError, match="no connector"):
        connectors.build("upload", {}, None, NetPolicy())


def test_limits_take_the_config():
    limits = make(max_points=7, max_span_s=3600, requests_per_second=2).limits()
    assert (limits.max_points, limits.max_span_ns, limits.requests_per_second) == (7, 3600 * S, 2)
