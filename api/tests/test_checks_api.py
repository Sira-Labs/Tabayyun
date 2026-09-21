import io

import pytest
import tabayyun_core as tc
from httpx import ASGITransport, AsyncClient

from tabayyun.main import create_app
from tabayyun.settings import Settings


def faulty_csv(faults: list[str]) -> bytes:
    batch = tc.synth(n=2880, faults=faults)
    ts = batch.column("ts").to_pylist()
    values = batch.column("value").to_pylist()
    quality = batch.column("quality").to_pylist()
    buf = io.StringIO()
    buf.write("ts,value,quality\n")
    for t, v, q in zip(ts, values, quality, strict=True):
        buf.write(f"{t.isoformat()},{'' if v is None else f'{v:.4f}'},{q}\n")
    return buf.getvalue().encode()


@pytest.fixture
def app():
    return create_app(Settings(env="test"))


async def test_list_checks(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/checks")
    assert r.status_code == 200
    assert "tby.flatline" in r.json()


async def test_run_checks_on_csv(app):
    csv = faulty_csv(["gap", "flatline", "nans", "negative"])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post(
            "/api/checks/run",
            files={"file": ("f.csv", csv, "text/csv")},
            data={"series_id": "demo", "unit": "m3/h", "quality_col": "quality"},
        )
    assert r.status_code == 200, r.text
    report = r.json()
    found = {f["check_id"] for f in report["findings"]}
    assert {"tby.completeness", "tby.flatline", "tby.value_type", "tby.non_negative"} <= found
    assert report["n_samples"] == 2880 - 144
    assert 0 < report["score"]["overall"] < 100


async def test_bad_csv_is_422(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/checks/run", files={"file": ("f.csv", b"a,b\n1,2\n", "text/csv")})
    assert r.status_code == 422


async def test_empty_upload_is_400(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/checks/run", files={"file": ("f.csv", b"", "text/csv")})
    assert r.status_code == 400
