"""Findings persistence and deduplication against the database (spec 003, ADR-0013)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, update

from tabayyun import core
from tabayyun.db import make_engine, make_session_factory
from tabayyun.db.models import DEFAULT_ORG_ID, DEFAULT_WORKSPACE_ID, Finding, Metric, Run
from tabayyun.services import findings as findings_service
from tabayyun.services import runs as runs_service
from tabayyun.services.timeconv import datetime_to_ns
from tabayyun.settings import Settings

T0 = 1_700_000_000 * 10**9
MINUTE = 60 * 10**9
GAP_EVIDENCE = {"gap_start": 0, "gap_end": 0, "gap_duration_ns": 0}
WINDOW_EVIDENCE = {"completeness": 0.9, "gap_count": 2}


def _finding(
    start_min: int,
    end_min: int,
    *,
    check_id: str = "tby.completeness",
    severity: str = "high",
    summary: str = "No data",
    evidence: dict | None = None,
) -> core.Finding:
    return core.Finding(
        check_id=check_id,
        series_id="demo",
        dimension="completeness",
        severity=severity,
        window={"start": T0 + start_min * MINUTE, "end": T0 + end_min * MINUTE},
        score_impact=0.1,
        summary=summary,
        evidence=dict(GAP_EVIDENCE if evidence is None else evidence),
    )


def _report(findings: list[core.Finding], metrics: list[dict] | None = None) -> core.CheckReport:
    return core.CheckReport(
        series_id="demo",
        n_samples=100,
        window={"start": T0, "end": T0 + 1000 * MINUTE},
        now_ns=T0 + 1000 * MINUTE,
        score=core.Score(series_id="demo", method_version="1", overall=90.0, dimensions={}, n_findings=0),
        findings=findings,
        metrics=metrics or [],
        skipped=[],
    )


class Ctx:
    """Session factory plus the series every test writes to."""

    def __init__(self, factory, series_id: uuid.UUID) -> None:
        self.factory = factory
        self.series_id = series_id

    async def new_run(self) -> uuid.UUID:
        async with self.factory() as session, session.begin():
            run = Run(
                org_id=DEFAULT_ORG_ID,
                workspace_id=DEFAULT_WORKSPACE_ID,
                trigger="upload",
                status="succeeded",
                stats={},
                created_at=datetime.now(UTC),
            )
            session.add(run)
            await session.flush()
            return run.id

    async def persist(self, findings: list[core.Finding], metrics: list[dict] | None = None):
        run_id = await self.new_run()
        async with self.factory() as session, session.begin():
            outcome = await findings_service.persist_report(
                session,
                run_id=run_id,
                series_id=self.series_id,
                report=_report(findings, metrics),
                now=datetime.now(UTC),
            )
        return run_id, outcome

    async def findings(self) -> list[Finding]:
        async with self.factory() as session:
            stmt = select(Finding).order_by(Finding.window_start, Finding.created_at)
            return list((await session.execute(stmt)).scalars())

    async def set_status(self, status: str) -> None:
        async with self.factory() as session, session.begin():
            await session.execute(update(Finding).values(status=status))


@pytest.fixture
async def ctx(db_url, fresh_schema):
    """Fresh schema, a session factory and one series in the Uploads source."""
    fresh_schema("auto")
    engine = make_engine(Settings(env="test", database_url=db_url))
    factory = make_session_factory(engine)
    async with factory() as session, session.begin():
        series = await runs_service._resolve_series(session, "demo")
    yield Ctx(factory, series.id)
    await engine.dispose()


def _minutes(value: datetime) -> float:
    return (datetime_to_ns(value) - T0) / MINUTE


async def test_insert_new_findings(ctx):
    """Findings with no counterpart are inserted open, first and last run the same."""
    run_id, outcome = await ctx.persist([_finding(0, 60), _finding(200, 260)])
    assert (outcome.new, outcome.merged) == (2, 0)
    rows = await ctx.findings()
    assert len(rows) == 2
    for row in rows:
        assert row.status == "open"
        assert row.first_run_id == row.last_run_id == run_id
        assert row.occurrences == 1
    assert [(_minutes(r.window_start), _minutes(r.window_end)) for r in rows] == [(0, 60), (200, 260)]


async def test_merge_overlapping_open_finding_unions_window(ctx):
    """A later overlapping report updates the open finding: union window, newest facts."""
    first, _ = await ctx.persist([_finding(0, 60)])
    second, outcome = await ctx.persist([_finding(30, 90, severity="critical", summary="No data, longer")])
    assert (outcome.new, outcome.merged) == (0, 1)
    [row] = await ctx.findings()
    assert (_minutes(row.window_start), _minutes(row.window_end)) == (0, 90)
    assert (row.first_run_id, row.last_run_id, row.occurrences) == (first, second, 2)
    assert (row.severity, row.summary) == ("critical", "No data, longer")


async def test_merge_growing_backwards_keeps_the_id(ctx):
    """A union that moves the window start earlier rewrites the row under the same id."""
    await ctx.persist([_finding(30, 90)])
    [before] = await ctx.findings()
    await ctx.persist([_finding(0, 40)])
    [after] = await ctx.findings()
    assert after.id == before.id and after.created_at == before.created_at
    assert (_minutes(after.window_start), _minutes(after.window_end)) == (0, 90)
    assert after.occurrences == 2


async def test_acked_stays_acked_on_merge(ctx):
    """Merging keeps an acknowledged finding acknowledged."""
    await ctx.persist([_finding(0, 60)])
    await ctx.set_status("acked")
    await ctx.persist([_finding(10, 70)])
    [row] = await ctx.findings()
    assert (row.status, row.occurrences) == ("acked", 2)


@pytest.mark.parametrize("status", ["resolved", "muted"])
async def test_resolved_not_merged_new_finding_created(ctx, status):
    """Resolved and muted findings never absorb: the re-detected problem is a new finding."""
    await ctx.persist([_finding(0, 60)])
    await ctx.set_status(status)
    _, outcome = await ctx.persist([_finding(0, 60)])
    assert (outcome.new, outcome.merged) == (1, 0)
    rows = await ctx.findings()
    assert sorted(r.status for r in rows) == sorted([status, "open"])


async def test_two_incoming_merge_into_one_existing(ctx):
    """Two findings of one run overlapping one existing finding both merge; one occurrence."""
    await ctx.persist([_finding(0, 100)])
    run_id, outcome = await ctx.persist([_finding(0, 40), _finding(60, 120)])
    assert (outcome.new, outcome.merged) == (0, 2)
    [row] = await ctx.findings()
    assert (_minutes(row.window_start), _minutes(row.window_end)) == (0, 120)
    assert (row.last_run_id, row.occurrences) == (run_id, 2)


async def test_other_evidence_shape_is_not_absorbed(ctx):
    """A gap does not merge into the whole-window completeness finding of the same check."""
    await ctx.persist([_finding(0, 1000, severity="medium", evidence=WINDOW_EVIDENCE)])
    _, outcome = await ctx.persist(
        [_finding(100, 160), _finding(0, 1000, severity="medium", evidence=WINDOW_EVIDENCE)]
    )
    assert (outcome.new, outcome.merged) == (1, 1)
    rows = await ctx.findings()
    assert len(rows) == 2
    whole = next(r for r in rows if "completeness" in r.evidence)
    assert whole.occurrences == 2


async def test_best_overlap_wins_among_candidates(ctx):
    """With two overlapping candidates the incoming finding joins the closer one."""
    await ctx.persist([_finding(0, 100), _finding(150, 200)])
    _, outcome = await ctx.persist([_finding(90, 200)])
    assert outcome.merged == 1
    rows = await ctx.findings()
    assert [(r.occurrences, _minutes(r.window_start), _minutes(r.window_end)) for r in rows] == [
        (1, 0, 100),
        (2, 90, 200),
    ]


async def test_findings_of_one_run_do_not_merge_with_each_other(ctx):
    """Overlapping findings from the same run stay separate statements."""
    _, outcome = await ctx.persist([_finding(0, 60), _finding(30, 90)])
    assert (outcome.new, outcome.merged) == (2, 0)
    assert len(await ctx.findings()) == 2


async def test_other_check_is_not_absorbed(ctx):
    """Only findings of the same check merge."""
    await ctx.persist([_finding(0, 60)])
    _, outcome = await ctx.persist([_finding(0, 60, check_id="tby.value_type")])
    assert outcome.new == 1


async def test_metrics_rewritten_by_a_rerun(ctx):
    """Metric points are keyed by (series, check, name, ts); a rerun rewrites them."""
    metric = {
        "check_id": "tby.completeness",
        "name": "completeness",
        "series_id": "demo",
        "ts": T0,
        "value": 0.9,
    }
    nan = {**metric, "name": "latency", "value": None}
    await ctx.persist([], [metric, nan])
    second, outcome = await ctx.persist([], [{**metric, "value": 0.8}])
    assert outcome.metrics == 1
    async with ctx.factory() as session:
        rows = list((await session.execute(select(Metric))).scalars())
    assert [(r.name, r.value, r.run_id) for r in rows] == [("completeness", 0.8, second)]
