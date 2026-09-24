"""Epoch integer timestamps: declared or inferred unit and the plausibility guard (ADR-0014)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pytest
from httpx import ASGITransport, AsyncClient

from tabayyun import core
from tabayyun.main import create_app
from tabayyun.settings import Settings

START = datetime(2026, 1, 1, tzinfo=UTC)
FACTOR = {"s": 1, "ms": 1_000, "us": 1_000_000, "ns": 1_000_000_000}
# Shared with `tabayyun_core::time` tests (spec 017): the CLI and the API read epoch integers alike.
EPOCH_CASES = (
    Path(__file__).resolve().parents[2] / "core" / "tabayyun-core" / "tests" / "data" / "epoch_cases.json"
)


def epoch_csv(unit: str, *, hours: int = 48, start: datetime = START) -> bytes:
    """Hourly series with epoch integer timestamps in `unit`."""
    rows = ["ts,value"]
    for h in range(hours):
        seconds = int((start + timedelta(hours=h)).timestamp())
        rows.append(f"{seconds * FACTOR[unit]},{50 + h % 5}")
    return ("\n".join(rows) + "\n").encode()


def iso_csv(hours: int = 48) -> bytes:
    """The same series with RFC 3339 timestamps."""
    rows = ["ts,value"] + [f"{(START + timedelta(hours=h)).isoformat()},{50 + h % 5}" for h in range(hours)]
    return ("\n".join(rows) + "\n").encode()


def ts_ns(parsed: core.ParsedCsv) -> list[int]:
    """Timestamp column as ns integers whatever its Arrow type."""
    column = parsed.table.column("ts")
    return [int(v.value) if hasattr(v, "value") else int(v) for v in column.to_pylist()]


@pytest.mark.parametrize("unit", ["s", "ms", "us", "ns"])
def test_auto_infers_every_unit_to_the_same_instants(unit):
    """Seconds, milliseconds, microseconds and nanoseconds all land on the same instants."""
    expected = [int((START + timedelta(hours=h)).timestamp()) * 10**9 for h in range(48)]
    parsed = core.read_csv(epoch_csv(unit), "ts", "value", None)
    assert parsed.ts_unit == unit
    assert ts_ns(parsed) == expected
    assert ts_ns(core.read_csv(epoch_csv(unit), "ts", "value", None, ts_unit=unit)) == expected  # type: ignore[arg-type]


def test_text_timestamps_report_text():
    """RFC 3339 text is unambiguous and reported as `text`."""
    assert core.read_csv(iso_csv(), "ts", "value", None).ts_unit == "text"


def test_declared_unit_too_small_is_rejected():
    """Seconds declared as nanoseconds would land in 1970: rejected with a hint."""
    with pytest.raises(core.TimestampUnitError, match="read as nanoseconds give dates from 1970"):
        core.read_csv(epoch_csv("s"), "ts", "value", None, ts_unit="ns")


def test_declared_unit_too_large_is_rejected():
    """Nanoseconds declared as seconds overflow: rejected, not wrapped around."""
    with pytest.raises(core.TimestampUnitError, match="overflow"):
        core.read_csv(epoch_csv("ns"), "ts", "value", None, ts_unit="s")


def test_pre_1971_epoch_integers_need_text():
    """Epoch integers before 1971 are refused; RFC 3339 text is the way for old data."""
    with pytest.raises(core.TimestampUnitError, match="outside 1971–2199"):
        core.read_csv(epoch_csv("s", start=datetime(1970, 6, 1, tzinfo=UTC)), "ts", "value", None)


def _cases() -> dict:
    """The (median → unit) and (value, unit → ns) table shared with the Rust core."""
    return json.loads(EPOCH_CASES.read_text())


def test_thresholds_match_the_core():
    """The API infers units with the core's thresholds, boundaries included (shared table)."""
    for case in _cases()["infer"]:
        assert core.infer_epoch_unit(case["median_abs"]) == case["unit"], case


def test_conversion_matches_the_core():
    """Each shared (value, unit) case converts to the same ns, or is refused in both."""
    for case in _cases()["convert"]:
        column = pa.array([case["value"]], type=pa.int64())
        if case["ns"] is None:
            with pytest.raises(core.TimestampUnitError):
                core.epoch_to_ns(column, "ts", case["unit"])
        else:
            ns, unit = core.epoch_to_ns(column, "ts", case["unit"])
            assert (ns.to_pylist(), unit) == ([case["ns"]], case["unit"]), case


async def test_stateless_endpoint_accepts_ts_unit():
    """Epoch seconds give the same report window as text; a wrong unit is a 422."""
    app = create_app(Settings(env="test"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:

        async def post(csv: bytes, form: dict[str, str]):
            """Post a CSV to the stateless endpoint with extra form fields."""
            return await client.post("/api/checks/run", files={"file": ("f.csv", csv, "text/csv")}, data=form)

        reports = []
        for csv, form in ((iso_csv(), {}), (epoch_csv("s"), {}), (epoch_csv("ms"), {"ts_unit": "ms"})):
            r = await post(csv, form)
            assert r.status_code == 200, r.text
            reports.append(r.json())
        assert reports[0]["window"] == reports[1]["window"] == reports[2]["window"]
        bad = await post(epoch_csv("s"), {"ts_unit": "ns"})
        assert bad.status_code == 422 and "ts_unit" in bad.json()["detail"]
        assert (await post(epoch_csv("s"), {"ts_unit": "minutes"})).status_code == 422
    await app.state.engine.dispose()
