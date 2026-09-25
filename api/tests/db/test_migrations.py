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
    "series_groups",
    "series_group_members",
    # Spec 007.
    "users",
    "org_memberships",
    "workspace_memberships",
    "teams",
    "team_members",
    "workspace_team_roles",
}
HYPERTABLES = {"findings", "metrics", "scores"}


def _current_revision(url: str) -> str | None:
    """Revision stamped in the database (None before the first migration)."""
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
    """An empty database migrates to head with exactly the spec's tables; a second upgrade is a no-op."""
    fresh_schema("auto")
    head = migrate.head_revision()
    assert _current_revision(db_url) == head
    engine = create_engine(db_url)
    try:
        tables = {n for n in inspect(engine).get_table_names() if not n.startswith("procrastinate_")}
        assert tables == EXPECTED_TABLES | {"alembic_version"}
        # Migration 0002 brings the queue schema alongside.
        assert "procrastinate_jobs" in inspect(engine).get_table_names()
    finally:
        engine.dispose()
    migrate.upgrade(db_url, "head", timescale="auto")
    assert _current_revision(db_url) == head


def test_models_match_migrations(db_url, fresh_schema):
    """Autogenerate against the migrated schema finds nothing to change."""
    fresh_schema("auto")
    migrate.check(db_url)  # raises AutogenerateError when models and migrations drift


def test_seed_rows_present(db_url, fresh_schema):
    """Migration 0001 seeds the default org and workspace with the fixed ids."""
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
    """auto mode converts the three result tables when TimescaleDB is available."""
    if not timescale_available:
        pytest.skip("timescaledb extension not available on this server")
    fresh_schema("auto")
    assert _hypertables(db_url) == HYPERTABLES


def test_hypertables_skipped_when_off(db_url, fresh_schema):
    """off mode never creates hypertables, even on a TimescaleDB server."""
    fresh_schema("off")
    assert _hypertables(db_url) & HYPERTABLES == set()


def test_timescale_on_requires_extension(db_url, fresh_schema, timescale_available):
    """on mode succeeds with the extension and fails cleanly without it."""
    if timescale_available:
        fresh_schema("on")
        assert _hypertables(db_url) == HYPERTABLES
        return
    with pytest.raises(RuntimeError, match="TABAYYUN_TIMESCALE=on"):
        fresh_schema("on")
    assert _current_revision(db_url) is None  # the failed migration left nothing behind


def test_0003_downgrades_and_upgrades(db_url, fresh_schema):
    """Migration 0003 downgrades to 0002 (groups gone, plain dataset FKs) and back cleanly."""
    fresh_schema("auto")

    def fk_rules() -> dict[str, str]:
        engine = create_engine(db_url)
        try:
            with engine.connect() as conn:
                rows = conn.execute(
                    text(
                        "SELECT conname, confdeltype FROM pg_constraint WHERE conname IN "
                        "('fk_runs_dataset_id_datasets', 'fk_dataset_series_dataset_id_datasets')"
                    )
                )
                return {r[0]: r[1] for r in rows}
        finally:
            engine.dispose()

    assert fk_rules() == {"fk_runs_dataset_id_datasets": "n", "fk_dataset_series_dataset_id_datasets": "c"}
    migrate.downgrade(db_url, "0002")
    assert _current_revision(db_url) == "0002"
    assert fk_rules() == {"fk_runs_dataset_id_datasets": "a", "fk_dataset_series_dataset_id_datasets": "a"}
    engine = create_engine(db_url)
    try:
        assert "series_groups" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()
    migrate.upgrade(db_url, "head", timescale="auto")
    assert _current_revision(db_url) == migrate.head_revision()
    migrate.check(db_url)


ORG_X = "00000000-0000-0000-0000-0000000000f1"
# Rows at revision 0003 in a second org, one per table that gains `org_id` in 0004.
SEED_0003 = f"""
INSERT INTO orgs (id, name) VALUES ('{ORG_X}', 'x');
INSERT INTO workspaces (id, org_id, name) VALUES ('00000000-0000-0000-0000-0000000000f2', '{ORG_X}', 'x');
INSERT INTO sources (id, org_id, workspace_id, type, name)
  VALUES ('00000000-0000-0000-0000-0000000000f3', '{ORG_X}', '00000000-0000-0000-0000-0000000000f2',
          'upload', 's');
INSERT INTO series (id, org_id, workspace_id, source_id, external_id, name)
  VALUES ('00000000-0000-0000-0000-0000000000f4', '{ORG_X}', '00000000-0000-0000-0000-0000000000f2',
          '00000000-0000-0000-0000-0000000000f3', 'x', 'x');
INSERT INTO runs (id, org_id, workspace_id, trigger, status)
  VALUES ('00000000-0000-0000-0000-0000000000f5', '{ORG_X}', '00000000-0000-0000-0000-0000000000f2', 'upload',
          'queued');
INSERT INTO uploads (run_id, filename, size_bytes, data)
  VALUES ('00000000-0000-0000-0000-0000000000f5', 'f.csv', 1, 'x');
INSERT INTO metrics (series_id, run_id, check_id, name, ts, value)
  VALUES ('00000000-0000-0000-0000-0000000000f4', '00000000-0000-0000-0000-0000000000f5', 'c', 'm', now(), 1);
INSERT INTO scores (series_id, run_id, method_version, overall, computed_at)
  VALUES ('00000000-0000-0000-0000-0000000000f4', '00000000-0000-0000-0000-0000000000f5', '1', 90, now());
INSERT INTO coverage (series_id, range_start, range_end, rows)
  VALUES ('00000000-0000-0000-0000-0000000000f4', now(), now(), 1);
INSERT INTO datasets (id, org_id, workspace_id, name)
  VALUES ('00000000-0000-0000-0000-0000000000f6', '{ORG_X}', '00000000-0000-0000-0000-0000000000f2', 'd');
INSERT INTO dataset_series (dataset_id, series_id)
  VALUES ('00000000-0000-0000-0000-0000000000f6', '00000000-0000-0000-0000-0000000000f4');
INSERT INTO series_groups (id, org_id, workspace_id, name, kind)
  VALUES ('00000000-0000-0000-0000-0000000000f7', '{ORG_X}', '00000000-0000-0000-0000-0000000000f2', 'g',
          'related');
INSERT INTO series_group_members (group_id, series_id, role, position)
  VALUES ('00000000-0000-0000-0000-0000000000f7', '00000000-0000-0000-0000-0000000000f4', 'member', 0);
"""
BACKFILLED = ("uploads", "metrics", "scores", "coverage", "dataset_series", "series_group_members")


@pytest.mark.parametrize("mode", ["auto", "off"])
def test_0004_backfills_org_ids_and_downgrades(db_url, fresh_schema, mode):
    """Child rows written before 0004 get their parent's org; 0004 downgrades and upgrades cleanly."""
    fresh_schema(mode)
    migrate.downgrade(db_url, "0003", timescale=mode)
    engine = create_engine(db_url)
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(SEED_0003)
        migrate.upgrade(db_url, "head", timescale=mode)
        with engine.connect() as conn:
            for table in BACKFILLED:
                orgs = conn.execute(text(f"SELECT DISTINCT org_id::text FROM {table}")).scalars().all()  # noqa: S608
                assert orgs == [ORG_X], table
        migrate.downgrade(db_url, "0003", timescale=mode)
        assert "org_id" not in {c["name"] for c in inspect(engine).get_columns("uploads")}
        assert "users" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()
    migrate.upgrade(db_url, "head", timescale=mode)
    assert _current_revision(db_url) == migrate.head_revision()
    migrate.check(db_url)
