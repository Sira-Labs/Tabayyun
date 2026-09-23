"""Upload runs write the Parquet cache and record coverage (spec 006)."""

from __future__ import annotations

import os
import uuid

import pytest
import tabayyun_core
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import select

from synth_csv import faulty_csv
from tabayyun.db.models import Coverage, Series
from tabayyun.main import create_app
from tabayyun.services.timeconv import datetime_to_ns
from tabayyun.settings import Settings


async def _run(settings: Settings, csv: bytes) -> tuple[dict, list[Coverage], Series]:
    """Post one upload with inline jobs; return the finished run, coverage rows and series."""
    app = create_app(settings)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            "/api/runs",
            files={"file": ("f.csv", csv, "text/csv")},
            data={"series_id": "cached", "quality_col": "quality"},
        )
        assert r.status_code == 202, r.text
        run = (await client.get(f"/api/runs/{r.json()['id']}")).json()
    async with app.state.session_factory() as session:
        series = (await session.execute(select(Series).where(Series.external_id == "cached"))).scalar_one()
        cov = list((await session.execute(select(Coverage).where(Coverage.series_id == series.id))).scalars())
    await app.state.engine.dispose()
    return run, cov, series


def _settings(db_url: str, **cache: object) -> Settings:
    return Settings(env="test", database_url=db_url, inline_jobs=True, **cache)


async def test_upload_run_writes_cache_and_coverage(db_url, fresh_schema, tmp_path):
    """The run's series lands in the local cache, readable by series id, with one coverage row."""
    fresh_schema("auto")
    run, cov, series = await _run(_settings(db_url, cache_url=str(tmp_path)), faulty_csv(["gap"]))
    assert run["status"] == "succeeded", run
    cache = run["stats"]["cache"]
    assert cache["written"] is True and cache["rows"] == run["stats"]["n_samples"] and cache["files"] >= 1
    assert len(cov) == 1 and cov[0].rows == cache["rows"] and cov[0].layer == "raw"

    start, end = datetime_to_ns(cov[0].range_start), datetime_to_ns(cov[0].range_end)
    reader = tabayyun_core.Cache({"url": str(tmp_path)})
    got = reader.read("raw", str(series.source_id), [str(series.id)], start, end)
    assert got[str(series.id)].num_rows == cache["rows"]


async def test_reupload_widens_the_same_coverage_row(db_url, fresh_schema, tmp_path):
    """A second upload starting at the same instant updates the row instead of adding one."""
    fresh_schema("auto")
    settings = _settings(db_url, cache_url=str(tmp_path))
    await _run(settings, faulty_csv(["gap"]))
    run, cov, _ = await _run(settings, faulty_csv(["gap"]))
    assert run["stats"]["cache"]["written"] is True
    assert len(cov) == 1


async def test_unreachable_store_does_not_fail_the_run(db_url, fresh_schema):
    """With the store down the run still succeeds; stats say why nothing was cached."""
    fresh_schema("auto")
    settings = _settings(
        db_url,
        cache_url="s3://nowhere",
        s3_endpoint="http://127.0.0.1:9",
        s3_allow_http=True,
        s3_access_key_id="key",
        s3_secret_access_key=SecretStr("secret-for-tests-only"),
    )
    run, cov, _ = await _run(settings, faulty_csv(["gap"]))
    assert run["status"] == "succeeded", run
    assert run["stats"]["cache"]["written"] is False
    assert run["stats"]["cache"]["error"]
    assert cov == []


@pytest.mark.skipif("TABAYYUN_TEST_S3_URL" not in os.environ, reason="TABAYYUN_TEST_S3_URL not set")
async def test_upload_run_writes_to_s3(db_url, fresh_schema):
    """Same round trip against an S3 endpoint (RustFS in CI)."""
    fresh_schema("auto")
    store = {
        "cache_url": f"{os.environ['TABAYYUN_TEST_S3_URL']}/api-{uuid.uuid4().hex[:12]}",
        "s3_endpoint": os.environ.get("TABAYYUN_TEST_S3_ENDPOINT"),
        "s3_allow_http": os.environ.get("TABAYYUN_TEST_S3_ENDPOINT", "").startswith("http://"),
        "s3_access_key_id": os.environ["TABAYYUN_TEST_S3_ACCESS_KEY_ID"],
        "s3_secret_access_key": SecretStr(os.environ["TABAYYUN_TEST_S3_SECRET_ACCESS_KEY"]),
    }
    run, cov, _ = await _run(_settings(db_url, **store), faulty_csv(["gap"]))
    assert run["stats"]["cache"]["written"] is True, run["stats"]["cache"]
    assert len(cov) == 1
