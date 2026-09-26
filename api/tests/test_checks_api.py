import pytest
from httpx import ASGITransport, AsyncClient

from synth_csv import faulty_csv
from tabayyun.main import create_app
from tabayyun.settings import Settings


@pytest.fixture
def app():
    return create_app(Settings(env="test"))


async def test_list_checks(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers={"X-Tabayyun-Request": "1"}
    ) as c:
        r = await c.get("/api/checks")
    assert r.status_code == 200
    assert "tby.flatline" in r.json()


async def test_run_checks_on_csv(app):
    csv = faulty_csv(["gap", "flatline", "nans", "negative"])
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers={"X-Tabayyun-Request": "1"}
    ) as c:
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


async def test_latency_via_ingest_column(app):
    csv = faulty_csv([], late_minutes=10)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers={"X-Tabayyun-Request": "1"}
    ) as c:
        r = await c.post(
            "/api/checks/run",
            files={"file": ("f.csv", csv, "text/csv")},
            data={"series_id": "late", "ingest_col": "arrived"},
        )
    assert r.status_code == 200, r.text
    assert "tby.latency" in {f["check_id"] for f in r.json()["findings"]}
    assert r.json()["score"]["method_version"] == "v2"


async def test_bad_csv_is_422(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers={"X-Tabayyun-Request": "1"}
    ) as c:
        r = await c.post("/api/checks/run", files={"file": ("f.csv", b"a,b\n1,2\n", "text/csv")})
    assert r.status_code == 422


async def test_empty_upload_is_400(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers={"X-Tabayyun-Request": "1"}
    ) as c:
        r = await c.post("/api/checks/run", files={"file": ("f.csv", b"", "text/csv")})
    assert r.status_code == 400


@pytest.mark.parametrize(("now_ns", "status"), [(2**63 - 1, 200), (2**63, 422), (-(2**63) - 1, 422)])
async def test_now_ns_must_fit_the_core(app, now_ns, status):
    """`now_ns` is an `i64` in the core; beyond that range the request is rejected, not a 500."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers={"X-Tabayyun-Request": "1"}
    ) as c:
        r = await c.post(
            "/api/checks/run",
            files={"file": ("f.csv", faulty_csv([]), "text/csv")},
            data={"series_id": "now", "now_ns": str(now_ns)},
        )
    assert r.status_code == status, r.text
