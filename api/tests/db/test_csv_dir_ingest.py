"""File-drop sources against Postgres and a local cache (spec 026): scans of long, wide and Parquet
files, known and changed files, failures beside good files, the caps, skipped files, checks,
polling, refused jobs, the files list, roles, and a dataset run over ingested data."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from admin_support import ALICE, MIA
from tabayyun.connectors.csv_dir import NOT_ENABLED
from tabayyun.db import for_org
from tabayyun.db.models import DEFAULT_ORG_ID, Coverage, SourceFetch
from tabayyun.main import create_app
from tabayyun.services import fetches
from tenancy import app_settings

S = 1_000_000_000
T0 = 1_767_225_600 * S  # 2026-01-01T00:00:00Z
OLD = 1_700_000_000  # a modification time long settled
LONG = (
    "ts,series,value\n"
    "2026-01-01T00:00:00Z,flow,1.5\n"
    "2026-01-01T00:01:00Z,flow,2.5\n"
    "2026-01-01T00:00:00Z,temp,20\n"
)
WIDE = "ts,P1,P2\n2026-01-01T00:00:00Z,1,\n2026-01-01T00:01:00Z,2,3\n"


def drop(folder: Path, name: str, text: str, *, mtime: int | None = OLD) -> Path:
    path = folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


@dataclass
class Env:
    app: Any
    client: AsyncClient
    root: Path

    @property
    def folder(self) -> Path:
        return self.root / "drop"

    async def source(self, name: str = "drop", **config: Any) -> str:
        config.setdefault("path", "drop")
        r = await self.client.post("/api/sources", json={"type": "csv_dir", "name": name, "config": config})
        assert r.status_code == 201, r.text
        return r.json()["id"]

    async def scan(self, source_id: str) -> dict[str, Any]:
        r = await self.client.post(f"/api/sources/{source_id}/ingest")
        assert r.status_code == 202, r.text
        detail = await self.client.get(f"/api/sources/{source_id}/fetches/{r.json()['id']}")
        return detail.json()

    async def files(self, source_id: str, **params: Any) -> dict[str, Any]:
        r = await self.client.get(f"/api/sources/{source_id}/files", params=params)
        assert r.status_code == 200, r.text
        return r.json()

    async def series(self, source_id: str) -> dict[str, str]:
        items = (await self.client.get("/api/series", params={"source_id": source_id})).json()["items"]
        return {s["external_id"]: s["id"] for s in items}

    def values(self, source_id: str, series_id: str) -> list[float]:
        batches = self.app.state.run_cache.read_series(
            source_id=source_id, series_ids=[series_id], start_ns=T0, end_ns=T0 + 3600 * S
        )
        return batches[series_id].column("value").to_pylist()

    async def coverage(self, series_id: str) -> list[tuple[datetime, datetime, int]]:
        async with for_org(self.app.state.session_factory, DEFAULT_ORG_ID)() as session:
            rows = await session.execute(
                select(Coverage.range_start, Coverage.range_end, Coverage.rows).where(
                    Coverage.series_id == uuid.UUID(series_id)
                )
            )
            return [tuple(r) for r in rows]


def make_env(db_url: str, tmp_path: Path, **settings: Any) -> Env:
    root = tmp_path / "files"
    root.mkdir()
    settings.setdefault("file_sources_root", str(root))
    app = create_app(app_settings(db_url, inline_jobs=True, cache_url=str(tmp_path / "cache"), **settings))
    client = AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers={"X-Tabayyun-Request": "1"}
    )
    return Env(app, client, root.resolve())


@pytest.fixture
async def env(db_url, fresh_schema, tmp_path) -> AsyncIterator[Env]:
    fresh_schema("auto")
    e = make_env(db_url, tmp_path)
    async with e.client:
        yield e
    await e.app.state.engine.dispose()


async def test_a_scan_ingests_long_csv_and_parquet(env):
    drop(env.folder, "a.csv", LONG)
    pq.write_table(
        pa.table({"ts": pa.array([T0], pa.int64()), "series": ["level"], "value": [7.0]}),
        env.folder / "b.parquet",
    )
    os.utime(env.folder / "b.parquet", (OLD + 1, OLD + 1))
    sid = await env.source(ts_unit="ns")
    job = await env.scan(sid)
    assert (job["trigger"], job["status"], job["rows"], job["calls"]) == ("ingest", "succeeded", 4, 2)
    assert [
        (f["path"], f["status"], f["rows"], f["series"], f["new_series"]) for f in job["result"]["files"]
    ] == [
        ("a.csv", "ingested", 3, 2, 2),
        ("b.parquet", "ingested", 1, 1, 1),
    ]
    series = await env.series(sid)
    assert set(series) == {"flow", "temp", "level"}
    assert env.values(sid, series["flow"]) == [1.5, 2.5]
    [(start, end, rows)] = await env.coverage(series["flow"])
    assert (start, rows) == (datetime(2026, 1, 1, tzinfo=UTC), 2)
    assert end > datetime(2026, 1, 1, 0, 1, tzinfo=UTC)
    files = (await env.files(sid))["items"]
    assert [(f["path"], f["status"], f["n_series"], f["versions"]) for f in files] == [
        ("b.parquet", "ingested", 1, 1),
        ("a.csv", "ingested", 2, 1),
    ]
    assert files[1]["start_at"].startswith("2026-01-01T00:00:00") and files[1]["fetch_id"] == job["id"]
    source = (await env.client.get(f"/api/sources/{sid}")).json()
    assert source["health"]["status"] == "ok" and source["health"]["last_fetch"]["rows"] == 4
    assert source["n_series"] == 3


async def test_a_wide_source(env):
    drop(env.root / "wide", "w.csv", WIDE)
    sid = await env.source(name="wide", path="wide", layout="wide", series_column=None, value_column=None)
    job = await env.scan(sid)
    assert (job["status"], job["rows"]) == ("succeeded", 3)
    series = await env.series(sid)
    assert env.values(sid, series["P2"]) == [3.0]


async def test_a_rescan_skips_known_files_and_rereads_changed_ones(env):
    path = drop(env.folder, "a.csv", LONG)
    sid = await env.source()
    await env.scan(sid)
    again = await env.scan(sid)
    assert (again["status"], again["rows"], again["result"]["files"], again["result"]["known"]) == (
        "succeeded",
        0,
        [],
        1,
    )
    # Copied again, same content: known by its hash, and the row takes the new time.
    os.utime(path, (OLD + 50, OLD + 50))
    touched = await env.scan(sid)
    assert touched["result"]["known"] == 1 and touched["result"]["files"] == []
    [row] = (await env.files(sid))["items"]
    assert row["mtime"].startswith(datetime.fromtimestamp(OLD + 50, tz=UTC).isoformat()[:19])
    # Changed content: read again, its values win.
    drop(env.folder, "a.csv", LONG.replace("1.5", "9.5"), mtime=OLD + 100)
    changed = await env.scan(sid)
    assert [f["rows"] for f in changed["result"]["files"]] == [3]
    [row] = (await env.files(sid))["items"]
    assert row["versions"] == 2
    assert env.values(sid, (await env.series(sid))["flow"]) == [9.5, 2.5]


async def test_a_bad_file_does_not_stop_the_others(env):
    drop(env.folder, "a.csv", LONG)
    drop(env.folder, "bad.csv", "ts,series\n2026-01-01T00:00:00Z,x\n", mtime=OLD - 10)
    sid = await env.source()
    job = await env.scan(sid)
    assert job["status"] == "partial"
    assert job["error"] == "1 file(s) failed: bad.csv: column 'value' is missing"
    assert [f["status"] for f in job["result"]["files"]] == ["failed", "ingested"]
    failed = (await env.files(sid, status="failed"))["items"]
    assert [(f["path"], f["error"], f["rows"]) for f in failed] == [
        ("bad.csv", "column 'value' is missing", 0)
    ]
    # The same content is not read again.
    again = await env.scan(sid)
    assert again["status"] == "succeeded" and again["result"]["known"] == 2
    health = (await env.client.get(f"/api/sources/{sid}")).json()["health"]
    assert health["status"] == "ok" and health["consecutive_failures"] == 0


async def test_the_file_and_series_caps(env):
    drop(env.folder, "a.csv", LONG, mtime=OLD)
    drop(env.folder, "b.csv", LONG.replace("flow", "pump"), mtime=OLD + 1)
    sid = await env.source(max_files_per_scan=1, max_series_per_file=2)
    first = await env.scan(sid)
    assert [f["path"] for f in first["result"]["files"]] == ["a.csv"] and first["result"]["more"]
    second = await env.scan(sid)
    assert [f["path"] for f in second["result"]["files"]] == ["b.csv"] and not second["result"]["more"]
    # Two new files older than the known ones: one is read, one waits, and the known ones past
    # the cap still count as known.
    drop(env.folder, "0.csv", LONG.replace("flow", "zero"), mtime=OLD - 2)
    drop(env.folder, "1.csv", LONG.replace("flow", "one"), mtime=OLD - 1)
    capped = await env.scan(sid)
    assert [f["path"] for f in capped["result"]["files"]] == ["0.csv"]
    assert (capped["result"]["known"], capped["result"]["more"]) == (2, True)
    assert [f["path"] for f in (await env.scan(sid))["result"]["files"]] == ["1.csv"]
    drop(env.folder, "c.csv", LONG + "2026-01-01T00:00:00Z,third,1\n", mtime=OLD + 2)
    third = await env.scan(sid)
    [c] = third["result"]["files"]
    assert c["status"] == "failed" and "3 series" in c["error"]
    assert "third" not in await env.series(sid)


async def test_settling_and_outside_files_are_skipped(env, tmp_path):
    drop(env.folder, "fresh.csv", LONG, mtime=None)
    outside = drop(tmp_path / "elsewhere", "x.csv", LONG)
    (env.folder / "link.csv").symlink_to(outside)
    sid = await env.source()
    job = await env.scan(sid)
    assert job["status"] == "succeeded" and job["result"]["files"] == []
    assert (job["result"]["settling"], job["result"]["outside_root"]) == (1, ["link.csv"])


async def test_check_counts_files(env):
    drop(env.folder, "a.csv", "12345")
    sid = await env.source()
    r = await env.client.post(f"/api/sources/{sid}/check")
    job = (await env.client.get(f"/api/sources/{sid}/fetches/{r.json()['id']}")).json()
    assert (job["status"], job["result"]) == ("succeeded", {"files": 1, "bytes": 5})


async def test_without_the_root_setting_scans_and_checks_fail(db_url, fresh_schema, tmp_path):
    fresh_schema("auto")
    e = make_env(db_url, tmp_path, file_sources_root=None)
    async with e.client:
        sid = await e.source()
        job = await e.scan(sid)
        assert (job["status"], job["error"]) == ("failed", NOT_ENABLED)
        r = await e.client.post(f"/api/sources/{sid}/check")
        check = (await e.client.get(f"/api/sources/{sid}/fetches/{r.json()['id']}")).json()
        assert (check["status"], check["error"]) == ("failed", NOT_ENABLED)
    await e.app.state.engine.dispose()


async def test_a_due_source_is_polled_with_a_scan(env):
    sid = await env.source(poll_interval_s=60)
    now = datetime.now(UTC)
    due = await fetches.claim_due_sources(env.app.state.session_factory, now)
    assert [str(d[0]) for d in due] == [sid]
    factory = for_org(env.app.state.session_factory, DEFAULT_ORG_ID)
    fetch_id = await fetches.queue_poll(factory, uuid.UUID(sid), now, inline=True)
    async with factory() as session:
        fetch = await session.get(SourceFetch, fetch_id)
        assert fetch is not None and fetch.trigger == "ingest"


async def test_jobs_a_file_source_cannot_run(env):
    sid = await env.source()
    window = {"start": "2026-01-01T00:00:00Z", "end": "2026-01-02T00:00:00Z"}
    r = await env.client.post(f"/api/sources/{sid}/fetches", json=window)
    assert (r.status_code, r.json()["detail"]) == (409, "not_fetchable")
    for route, body in (("search", {"query": "x"}), ("metadata", {})):
        r = await env.client.post(f"/api/sources/{sid}/{route}", json=body)
        assert (r.status_code, r.json()["detail"]) == (409, "not_supported")
    syn = await env.client.post("/api/sources", json={"type": "synthetic", "name": "syn", "config": {}})
    r = await env.client.post(f"/api/sources/{syn.json()['id']}/ingest")
    assert (r.status_code, r.json()["detail"]) == (409, "not_supported")
    await env.client.patch(f"/api/sources/{sid}", json={"enabled": False})
    r = await env.client.post(f"/api/sources/{sid}/ingest")
    assert (r.status_code, r.json()["detail"]) == (409, "source_disabled")


@pytest.mark.parametrize("path", ["/etc", "../x", "a/../../b"])
async def test_paths_outside_the_root_are_refused_on_save(env, path):
    r = await env.client.post("/api/sources", json={"type": "csv_dir", "name": "x", "config": {"path": path}})
    assert (r.status_code, r.json()["detail"]) == (422, "invalid_config")


async def test_the_files_list_pages_and_filters(env):
    for i in range(3):
        drop(env.folder, f"f{i}.csv", LONG, mtime=OLD + i)
    sid = await env.source()
    await env.scan(sid)
    first = await env.files(sid, limit=2)
    assert len(first["items"]) == 2 and first["next_cursor"]
    rest = await env.files(sid, limit=2, cursor=first["next_cursor"])
    assert len(rest["items"]) == 1 and rest["next_cursor"] is None
    paths = [f["path"] for f in first["items"] + rest["items"]]
    assert sorted(paths) == ["f0.csv", "f1.csv", "f2.csv"]
    assert (await env.files(sid, status="failed"))["items"] == []
    r = await env.client.get(f"/api/sources/{sid}/files", params={"cursor": "nope"})
    assert (r.status_code, r.json()["detail"]) == (422, "invalid_cursor")


async def test_a_dataset_run_reads_ingested_data_without_fetching(env):
    drop(env.folder, "a.csv", LONG)
    sid = await env.source()
    await env.scan(sid)
    series = await env.series(sid)
    window = {"start": "2026-01-01T00:00:00Z", "end": "2026-01-01T01:00:00Z"}
    dataset = await env.client.post(
        "/api/datasets", json={"name": "drop", "series_ids": list(series.values()), "window": window}
    )
    assert dataset.status_code == 201, dataset.text
    r = await env.client.post("/api/runs", json={"dataset_id": dataset.json()["id"], "now": window["end"]})
    run = (await env.client.get(f"/api/runs/{r.json()['id']}")).json()
    assert run["status"] == "succeeded", run
    assert run["stats"]["n_samples"] == 3 and "fetch_errors" not in run["stats"]
    triggers = [f["trigger"] for f in (await env.client.get(f"/api/sources/{sid}/fetches")).json()["items"]]
    assert triggers == ["ingest"]


async def test_viewers_list_files_and_editors_scan(admin_env, tmp_path):
    root = tmp_path / "files"
    drop(root / "drop", "a.csv", LONG)
    settings = {"file_sources_root": str(root), "cache_url": str(tmp_path / "cache")}
    async with admin_env.client(ALICE, **settings) as alice:
        r = await alice.post(
            "/api/sources", json={"type": "csv_dir", "name": "d", "config": {"path": "drop"}}
        )
        sid = r.json()["id"]
        assert (await alice.post(f"/api/sources/{sid}/ingest")).status_code == 202
    async with admin_env.client(MIA, **settings) as mia:
        assert (await mia.post(f"/api/sources/{sid}/ingest")).status_code == 403
        assert [f["path"] for f in (await mia.get(f"/api/sources/{sid}/files")).json()["items"]] == ["a.csv"]
    [event] = admin_env.events("source.fetch_requested")
    assert event.details["trigger"] == "ingest" and "start" not in event.details
    assert admin_env.scalar("SELECT count(*) FROM series WHERE source_id = :s", s=sid) == 2
