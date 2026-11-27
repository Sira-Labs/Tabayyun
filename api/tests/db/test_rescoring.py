"""The corrected layer's score (spec 036): an approval has the corrected layer scored beside
the raw one, and nothing of the raw layer changes."""

from __future__ import annotations

import uuid
from datetime import timedelta

from db.test_corrections_api import H0, HOUR, HOUR0, HOURS, lab  # noqa: F401
from db.test_layer_reads import approve
from tabayyun.db import for_org
from tabayyun.db.models import DEFAULT_ORG_ID
from tabayyun.services import corrections, rescoring
from tabayyun.services.cache import CacheError

RAW_TABLES = {
    "scores": "SELECT * FROM scores WHERE layer = 'raw' ORDER BY series_id, computed_at",
    "score_days": "SELECT * FROM score_days WHERE layer = 'raw' ORDER BY series_id, day",
    "findings": "SELECT * FROM findings ORDER BY id",
    "metrics": "SELECT * FROM metrics ORDER BY series_id, ts, check_id, name",
}


def raw_rows(lab) -> dict[str, list[tuple]]:  # noqa: F811
    """Every raw score, raw score day, finding and metric, whole rows."""
    return {name: [tuple(r) for r in lab.env.sql(stmt)] for name, stmt in RAW_TABLES.items()}


def corrected_rows(lab) -> list:  # noqa: F811
    return list(
        lab.env.sql(
            "SELECT series_id, run_id, overall, dimensions, n_findings, baseline, computed_at "
            "FROM scores WHERE layer = 'corrected' ORDER BY computed_at"
        )
    )


async def rescore(lab, max_rows: int = 5_000_000) -> rescoring.Outcome:  # noqa: F811
    """The worker's call for the lab's series."""
    app = lab.app
    return await rescoring.rescore(
        for_org(app.state.session_factory, DEFAULT_ORG_ID), uuid.UUID(lab.sid), app.state.run_cache, max_rows
    )


async def test_approval_rescored_raw_to_corrected(lab):  # noqa: F811
    """Approving the correction that fills the gap stores one `corrected` score that is higher
    than its baseline, the raw layer scored the same way; the series shows both numbers."""
    before = raw_rows(lab)
    [(run_id, raw_overall, raw_at)] = lab.env.sql("SELECT run_id, overall, computed_at FROM scores")
    assert corrected_rows(lab) == [] and len(before["scores"]) == 1 and before["findings"]
    async with lab.client() as c:
        assert (await c.get(f"/api/series/{lab.sid}")).json()["corrected_score"] is None
        approved = await approve(lab, c, max_gap_ns=str(6 * HOUR))
        assert approved["status"] == "approved"

        [row] = corrected_rows(lab)
        assert (str(row.series_id), row.run_id) == (lab.sid, run_id)
        assert set(row.baseline) == {"overall", "dimensions", "n_findings", "version"}
        assert row.baseline["version"] == 1 and row.baseline["overall"] == raw_overall
        assert row.overall > row.baseline["overall"]
        assert row.dimensions["completeness"] == 100.0 > row.baseline["dimensions"]["completeness"]
        assert row.n_findings < row.baseline["n_findings"] and row.computed_at > raw_at
        [day] = lab.env.sql(
            "SELECT overall, completeness, n_findings FROM score_days WHERE layer = 'corrected'"
        )
        assert tuple(day) == (row.overall, 100.0, row.n_findings)

        series = (await c.get(f"/api/series/{lab.sid}")).json()
        assert series["corrected_score"] == {
            "overall": row.overall,
            "computed_at": row.computed_at.isoformat().replace("+00:00", "Z"),
            "version": 1,
            "raw": {"overall": raw_overall},
        }
        assert series["latest_score"]["overall"] == raw_overall
    assert raw_rows(lab) == before


async def test_series_shows_both_scores(lab):  # noqa: F811
    """The series and its row in the list carry `corrected_score` (null without one) beside
    `latest_score`, which stays the raw layer's, as do the run count and the last run."""
    async with lab.client() as c:
        one = (await c.get(f"/api/series/{lab.sid}")).json()
        [listed] = (await c.get("/api/series")).json()["items"]
        assert one["corrected_score"] is None and listed["corrected_score"] is None
        await approve(lab, c, max_gap_ns=str(6 * HOUR))
        after = (await c.get(f"/api/series/{lab.sid}")).json()
        [listed_after] = (await c.get("/api/series")).json()["items"]
        pair = after["corrected_score"]
        assert pair == listed_after["corrected_score"] and pair["version"] == 1
        assert pair["overall"] > pair["raw"]["overall"] == one["latest_score"]["overall"]
        for field in ("latest_score", "n_runs", "last_run_at", "open_findings"):
            assert after[field] == one[field], field
        assert listed_after["latest_score"] == listed["latest_score"]
        assert listed_after["last_run_at"] == listed["last_run_at"]
        # The list's score filter reads the raw layer: the corrected 94 does not hide the series.
        low = (await c.get("/api/series", params={"score_max": pair["raw"]["overall"]})).json()["items"]
        assert [s["id"] for s in low] == [lab.sid]
        # The overview reads the raw layer too.
        tiles = (await c.get("/api/overview")).json()["tiles"]
        assert next(t for t in tiles if t["metric"] == "overall")["mean"] == pair["raw"]["overall"]


async def test_scores_filter_by_layer(lab):  # noqa: F811
    """`GET /api/series/{id}/scores` lists both layers, or one with `layer`; a corrected row
    has its baseline and version, a raw row neither."""
    url = f"/api/series/{lab.sid}/scores"
    async with lab.client() as c:
        await approve(lab, c, max_gap_ns=str(6 * HOUR))
        both = (await c.get(url)).json()["items"]
        assert [s["layer"] for s in both] == ["corrected", "raw"]
        [raw] = (await c.get(url, params={"layer": "raw"})).json()["items"]
        [corrected] = (await c.get(url, params={"layer": "corrected"})).json()["items"]
        assert [corrected, raw] == both
        assert (raw["baseline"], raw["version"]) == (None, None)
        assert corrected["version"] == 1 and corrected["run_id"] == raw["run_id"]
        assert corrected["baseline"] == {
            "overall": raw["overall"],
            "dimensions": raw["dimensions"],
            "n_findings": raw["n_findings"],
        }
        assert corrected["overall"] > raw["overall"] and corrected["method_version"] == raw["method_version"]
        by_run = (await c.get(url, params={"run_id": raw["run_id"], "layer": "raw"})).json()["items"]
        assert by_run == [raw]
        assert (await c.get(url, params={"layer": "corrected@latest"})).status_code == 422


async def test_never_scored_series(lab):  # noqa: F811
    """A series without a raw score: the approval succeeds and no score is written."""
    lab.env.sql("DELETE FROM score_days")
    lab.env.sql("DELETE FROM scores")
    async with lab.client() as c:
        approved = await approve(lab, c, max_gap_ns=str(6 * HOUR))
        assert approved["status"] == "approved"
        assert lab.env.scalar("SELECT count(*) FROM scores") == 0
        assert lab.env.scalar("SELECT count(*) FROM score_days") == 0
        assert (await c.get(f"/api/series/{lab.sid}")).json()["corrected_score"] is None
        assert (await c.get(f"/api/series/{lab.sid}/chart", params={"layer": "corrected@latest"})).json()[
            "layer_version"
        ] == 1
    assert await rescore(lab) == rescoring.Outcome(status="never_scored")


async def test_window_too_large(lab):  # noqa: F811
    """A run window with more rows than `TABAYYUN_CORRECTION_MAX_ROWS`: the approval succeeds
    and no score is written; with room for the rows the same call scores."""
    async with lab.client() as c:
        made = await lab.propose(c, "impute.linear", 8, 16, max_gap_ns=str(HOUR * 6))
    async with lab.client(correction_max_rows=10) as tight:
        answer = await tight.post(f"/api/corrections/{made['id']}/approve")
        assert (answer.status_code, answer.json()["status"]) == (200, "approved")
        assert corrected_rows(lab) == []
        assert lab.env.scalar("SELECT count(*) FROM score_days WHERE layer = 'corrected'") == 0
    assert await rescore(lab, max_rows=10) == rescoring.Outcome(status="too_large")
    # The coverage estimate passes at 45 rows; the 48 corrected rows that are read do not.
    assert await rescore(lab, max_rows=HOURS - 3) == rescoring.Outcome(status="too_large", version=1)
    assert corrected_rows(lab) == []
    scored = await rescore(lab, max_rows=HOURS)
    assert (scored.status, scored.version) == ("scored", 1) and len(corrected_rows(lab)) == 1


async def test_raw_scores_untouched(lab):  # noqa: F811
    """No raw score, score day, finding or metric is written or changed by the re-scoring,
    however often it runs, and a series without an approved correction is left alone."""
    before = raw_rows(lab)
    assert all(before.values()), {name: len(rows) for name, rows in before.items()}
    assert await rescore(lab) == rescoring.Outcome(status="not_corrected")
    assert corrected_rows(lab) == []
    async with lab.client() as c:
        await approve(lab, c, max_gap_ns=str(6 * HOUR))
        await approve(lab, c, "mask", 20, 24)
        await approve(lab, c, "transform.affine", 0, HOURS, scale=3, offset=1)
    for _ in range(2):
        assert (await rescore(lab)).status == "scored"
    assert len(corrected_rows(lab)) == 5
    assert raw_rows(lab) == before
    # Corrected scores of the layer are the only rows that are new.
    assert lab.env.scalar("SELECT count(*) FROM scores") == len(before["scores"]) + 5
    assert lab.env.scalar("SELECT count(*) FROM score_days") == len(before["score_days"]) + 1


async def test_second_approval_scores_again(lab):  # noqa: F811
    """Every approval scores the layer again: the newer row names the higher version, and the
    series shows it. Without inline jobs the job is queued with the approval."""
    async with lab.client() as c:
        await approve(lab, c, max_gap_ns=str(6 * HOUR))
        first = (await c.get(f"/api/series/{lab.sid}")).json()["corrected_score"]
        # Masking four hours takes samples away again: the second score is lower.
        await approve(lab, c, "mask", 20, 24)
        second = (await c.get(f"/api/series/{lab.sid}")).json()["corrected_score"]
        assert (first["version"], second["version"]) == (1, 2)
        assert second["computed_at"] > first["computed_at"] and second["overall"] < first["overall"]
        assert second["raw"] == first["raw"]
        rows = corrected_rows(lab)
        assert [r.baseline["version"] for r in rows] == [1, 2] and rows[0].run_id == rows[1].run_id
        made = await lab.propose(c, "transform.affine", 30, 32, scale=1, offset=0.5)

    async with lab.client(inline_jobs=False) as queued:
        answer = await queued.post(f"/api/corrections/{made['id']}/approve")
        assert (answer.status_code, answer.json()["status"]) == (200, "approved")
        [job] = lab.env.sql("SELECT queue_name, task_name, args FROM procrastinate_jobs")
        assert (job.queue_name, job.task_name) == ("runs", "tabayyun.rescore_corrected")
        assert job.args == {"series_id": lab.sid, "org_id": str(DEFAULT_ORG_ID)}
        assert len(corrected_rows(lab)) == 2
        # A rejection queues nothing.
        other = await lab.propose(queued, "mask", 40, 42, expect="proposed")
        assert (await queued.post(f"/api/corrections/{other['id']}/reject")).status_code == 200
        tasks = [t for (t,) in lab.env.sql("SELECT task_name FROM procrastinate_jobs ORDER BY id")]
        assert tasks == ["tabayyun.rescore_corrected", "tabayyun.apply_repair"]
        outcome = await rescore(lab)
        assert (outcome.status, outcome.version) == ("scored", 3)
        assert (await queued.get(f"/api/series/{lab.sid}")).json()["corrected_score"]["version"] == 3


async def test_failed_rescoring_leaves_the_approval(lab, monkeypatch):  # noqa: F811
    """A cache that cannot be read, or checks that cannot run, write no score; the approval
    was committed before and stands."""
    async with lab.client() as c:
        made = await lab.propose(c, "impute.linear", 8, 16, max_gap_ns=str(6 * HOUR))

        async def unreadable(*args, **kwargs):
            raise CacheError("store down at s3://secret-bucket")

        monkeypatch.setattr(corrections, "read_composed", unreadable)
        answer = await c.post(f"/api/corrections/{made['id']}/approve")
        assert (answer.status_code, answer.json()["status"]) == (200, "approved")
        assert corrected_rows(lab) == []
        assert await rescore(lab) == rescoring.Outcome(status="cache_error", version=1)
        monkeypatch.undo()

        def broken(*args, **kwargs):
            raise RuntimeError("core panicked")

        monkeypatch.setattr(rescoring.core, "run_checks_multi", broken)
        assert await rescore(lab) == rescoring.Outcome(status="check_error", version=1)
        assert corrected_rows(lab) == []
        monkeypatch.undo()
        assert (await rescore(lab)).status == "scored"


async def test_scores_with_the_checks_of_the_newest_run(lab):  # noqa: F811
    """The newest raw score may be a suite's dataset run: its window, its `now` and its checks
    that look at one series score both layers, so the baseline is that run's own score."""
    window = {"start": HOUR0.isoformat(), "end": (HOUR0 + timedelta(hours=HOURS)).isoformat()}
    async with lab.client() as c:
        r = await c.post("/api/datasets", json={"name": "flows", "series_ids": [lab.sid], "window": window})
        assert r.status_code == 201, r.text
        checks = [{"id": "tby.completeness"}, {"id": "tby.balance_residual"}]
        r = await c.post("/api/suites", json={"name": "gaps", "dataset_id": r.json()["id"], "checks": checks})
        assert r.status_code == 201, r.text
        r = await c.post(f"/api/suites/{r.json()['id']}/runs")
        assert r.status_code == 202, r.text
        run = (await c.get(f"/api/runs/{r.json()['id']}")).json()
        assert run["status"] == "succeeded" and run["window"]["end"] == H0 + HOURS * HOUR, run
        [scored] = run["series"]

        await approve(lab, c, max_gap_ns=str(6 * HOUR))
        [row] = corrected_rows(lab)
        assert str(row.run_id) == run["id"]
        assert row.baseline["overall"] == scored["score"] < row.overall == 100.0
        assert row.n_findings < row.baseline["n_findings"] == run["stats"]["n_findings"]
        assert set(row.dimensions) == set(row.baseline["dimensions"])
        newest = (await c.get(f"/api/series/{lab.sid}")).json()
        assert newest["latest_score"]["overall"] == scored["score"]
        assert newest["corrected_score"]["raw"] == {"overall": scored["score"]}


def test_checks_across_series_are_left_out():
    """The run's checks that look at one series are kept with their parameters; checks across
    series are named. A run of the default set keeps it (None)."""
    checks = [
        {"id": "tby.completeness", "enabled": True, "params": {"min_ratio": 0.9}},
        {"id": "tby.balance_residual", "enabled": True, "params": {}},
        {"id": "tby.spikes", "enabled": False, "params": {}},
        {"id": "tby.correlation_break", "enabled": True},
    ]
    kept, left_out = rescoring.single_series_configs(checks, dataset_run=True)
    assert kept == [{"id": "tby.completeness", "params": {"min_ratio": 0.9}}]
    assert left_out == ["tby.balance_residual", "tby.correlation_break"]
    assert rescoring.single_series_configs(None, dataset_run=False) == (None, [])
    default, across = rescoring.single_series_configs(None, dataset_run=True)
    assert default is None
    assert across == ["tby.balance_residual", "tby.correlation_break", "tby.redundant_disagreement"]
