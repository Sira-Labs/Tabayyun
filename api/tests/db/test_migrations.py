import pytest
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text

from tabayyun.db import migrate
from tabayyun.db.models import DEFAULT_ORG_ID, DEFAULT_WORKSPACE_ID

EXPECTED_TABLES = {
    "orgs",
    "workspaces",
    "sources",
    "series",
    "datasets",
    "dataset_series",
    "runs",
    "uploads",
    "findings",
    "metrics",
    "scores",
    "coverage",
}
HYPERTABLES = {"findings", "metrics", "scores"}


def _current_revision(url: str) -> str | None:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return MigrationContext.configure(conn).get_current_revision()
    finally:
        engine.dispose()


def _hypertables(url: str) -> set[str]:
    """Names in timescaledb_information.hypertables, or empty when the extension is absent."""
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            installed = conn.execute(
                text("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'")
            ).scalar()
            if not installed:
                return set()
            rows = conn.execute(text("SELECT hypertable_name FROM timescaledb_information.hypertables"))
            return {r[0] for r in rows}
    finally:
        engine.dispose()


def test_migrate_fresh_and_idempotent(db_url, fresh_schema):
    fresh_schema("auto")
    head = migrate.head_revision()
    assert _current_revision(db_url) == head
    engine = create_engine(db_url)
    try:
        assert set(inspect(engine).get_table_names()) == EXPECTED_TABLES | {"alembic_version"}
    finally:
        engine.dispose()
    migrate.upgrade(db_url, "head", timescale="auto")
    assert _current_revision(db_url) == head


def test_models_match_migrations(db_url, fresh_schema):
    fresh_schema("auto")
    migrate.check(db_url)  # raises AutogenerateError when models and migrations drift


def test_seed_rows_present(db_url, fresh_schema):
    fresh_schema("auto")
    engine = create_engine(db_url)
    try:
        with engine.connect() as conn:
            orgs = conn.execute(text("SELECT id, name FROM orgs")).all()
            workspaces = conn.execute(text("SELECT id, org_id, name, timezone FROM workspaces")).all()
    finally:
        engine.dispose()
    assert orgs == [(DEFAULT_ORG_ID, "default")]
    assert workspaces == [(DEFAULT_WORKSPACE_ID, DEFAULT_ORG_ID, "default", "UTC")]


def test_hypertables_created(db_url, fresh_schema, timescale_available):
    if not timescale_available:
        pytest.skip("timescaledb extension not available on this server")
    fresh_schema("auto")
    assert _hypertables(db_url) == HYPERTABLES


def test_hypertables_skipped_when_off(db_url, fresh_schema):
    fresh_schema("off")
    assert _hypertables(db_url) & HYPERTABLES == set()


def test_timescale_on_requires_extension(db_url, fresh_schema, timescale_available):
    if timescale_available:
        fresh_schema("on")
        assert _hypertables(db_url) == HYPERTABLES
        return
    with pytest.raises(RuntimeError, match="TABAYYUN_TIMESCALE=on"):
        fresh_schema("on")
    assert _current_revision(db_url) is None  # the failed migration left nothing behind
