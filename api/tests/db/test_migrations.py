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
    # Spec 013.
    "user_identities",
    "sessions",
    "login_flows",
    # Spec 014.
    "invitations",
    "audit_events",
    # Spec 015.
    "rate_limits",
    # Spec 021.
    "source_credentials",
    "source_fetches",
    # Spec 026.
    "source_files",
    # Spec 027.
    "check_suites",
    # Spec 028.
    "series_baselines",
    # Spec 029.
    "finding_filters",
    # Spec 031.
    "alert_rules",
    "alert_notifications",
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


def test_0009_downgrades_keeping_source_jobs(db_url, fresh_schema):
    """0009 adds `params` and `result`; its downgrade drops them and keeps search rows (spec 022)."""
    fresh_schema("auto")
    engine = create_engine(db_url)
    source = "00000000-0000-0000-0000-0000000000e1"
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO sources (id, org_id, workspace_id, type, name) "
                    "VALUES (:id, :org, :ws, 'synthetic', 'syn')"
                ),
                {"id": source, "org": str(DEFAULT_ORG_ID), "ws": str(DEFAULT_WORKSPACE_ID)},
            )
            conn.execute(
                text(
                    "INSERT INTO source_fetches (id, org_id, workspace_id, source_id, trigger, window_start, "
                    "window_end, params) VALUES (gen_random_uuid(), :org, :ws, :src, 'search', now(), now(), "
                    '\'{"query": "flow"}\')'
                ),
                {"org": str(DEFAULT_ORG_ID), "ws": str(DEFAULT_WORKSPACE_ID), "src": source},
            )
        migrate.downgrade(db_url, "0008")
        assert _current_revision(db_url) == "0008"
        columns = {c["name"] for c in inspect(engine).get_columns("source_fetches")}
        assert {"params", "result"}.isdisjoint(columns)
        with engine.connect() as conn:
            assert conn.execute(text("SELECT trigger FROM source_fetches")).scalars().all() == ["search"]
        with pytest.raises(Exception, match="ck_source_fetches_trigger"), engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO source_fetches (id, org_id, workspace_id, source_id, trigger, window_start, "
                    "window_end) VALUES (gen_random_uuid(), :org, :ws, :src, 'metadata', now(), now())"
                ),
                {"org": str(DEFAULT_ORG_ID), "ws": str(DEFAULT_WORKSPACE_ID), "src": source},
            )
    finally:
        engine.dispose()
    migrate.upgrade(db_url, "head", timescale="auto")
    assert _current_revision(db_url) == migrate.head_revision()
    migrate.check(db_url)


def test_0010_downgrades_keeping_ingest_jobs(db_url, fresh_schema):
    """0010 adds `source_files` and the `ingest` trigger; its downgrade drops the table and keeps
    ingest rows (spec 026)."""
    fresh_schema("auto")
    engine = create_engine(db_url)
    source = "00000000-0000-0000-0000-0000000000e2"
    params = {"org": str(DEFAULT_ORG_ID), "ws": str(DEFAULT_WORKSPACE_ID), "src": source}
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO sources (id, org_id, workspace_id, type, name) "
                    "VALUES (:src, :org, :ws, 'csv_dir', 'drop')"
                ),
                params,
            )
            conn.execute(
                text(
                    "INSERT INTO source_fetches (id, org_id, workspace_id, source_id, trigger, window_start, "
                    "window_end) VALUES (gen_random_uuid(), :org, :ws, :src, 'ingest', now(), now())"
                ),
                params,
            )
            conn.execute(
                text(
                    "INSERT INTO source_files (id, org_id, workspace_id, source_id, path, size, mtime, "
                    "sha256, status) VALUES (gen_random_uuid(), :org, :ws, :src, 'a.csv', 1, now(), 'x', "
                    "'ingested')"
                ),
                params,
            )
        migrate.downgrade(db_url, "0009")
        assert _current_revision(db_url) == "0009"
        assert "source_files" not in inspect(engine).get_table_names()
        with engine.connect() as conn:
            assert conn.execute(text("SELECT trigger FROM source_fetches")).scalars().all() == ["ingest"]
        with pytest.raises(Exception, match="ck_source_fetches_trigger"), engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO source_fetches (id, org_id, workspace_id, source_id, trigger, window_start, "
                    "window_end) VALUES (gen_random_uuid(), :org, :ws, :src, 'ingest', now(), now())"
                ),
                params,
            )
    finally:
        engine.dispose()
    migrate.upgrade(db_url, "head", timescale="auto")
    assert _current_revision(db_url) == migrate.head_revision()
    migrate.check(db_url)


def test_0011_replaces_the_job_fetch_function_and_downgrades(db_url, fresh_schema):
    """0011 installs Procrastinate 3.11's `procrastinate_fetch_job_v2`; its downgrade puts the
    3.9.0 definition back, and both fetch a queued job."""
    fresh_schema("auto")
    engine = create_engine(db_url)
    source = text("SELECT prosrc FROM pg_proc WHERE proname = 'procrastinate_fetch_job_v2'")
    fetch = text("SELECT id, status FROM procrastinate_fetch_job_v2(ARRAY['q0011']::varchar[], NULL)")
    queue = text(
        "INSERT INTO procrastinate_jobs (queue_name, task_name, lock) "
        "VALUES ('q0011', 't', :lock) RETURNING id"
    )
    try:
        with engine.begin() as conn:
            assert "jobs.lock IS NULL" in conn.execute(source).scalar_one()
            unlocked = conn.execute(queue, {"lock": None}).scalar_one()
            assert conn.execute(fetch).one() == (unlocked, "doing")
        migrate.downgrade(db_url, "0010")
        assert _current_revision(db_url) == "0010"
        with engine.begin() as conn:
            assert "jobs.lock IS NOT NULL" in conn.execute(source).scalar_one()
            # The first job of a lock runs; a second one with the same lock waits behind it.
            first = conn.execute(queue, {"lock": "L"}).scalar_one()
            conn.execute(queue, {"lock": "L"})
            assert conn.execute(fetch).one() == (first, "doing")
            assert conn.execute(fetch).one() == (None, None)
        migrate.upgrade(db_url, "head", timescale="auto")
        with engine.begin() as conn:
            assert "jobs.lock IS NULL" in conn.execute(source).scalar_one()
            assert conn.execute(fetch).one() == (None, None)
    finally:
        engine.dispose()
        fresh_schema("auto")


def test_0013_can_be_run_again_after_an_interrupted_index_build(db_url, fresh_schema):
    """0013 works outside a transaction, so an interrupted upgrade leaves the database at 0012
    with part of the work done, at worst an invalid index. Running it again completes it."""
    fresh_schema("auto")
    engine = create_engine(db_url)
    state = text(
        "SELECT indisvalid, (SELECT convalidated FROM pg_constraint "
        "WHERE conname = 'fk_runs_suite_id_check_suites') "
        "FROM pg_index JOIN pg_class ON pg_class.oid = indexrelid "
        "WHERE relname = 'ix_runs_suite_id_created_at'"
    )
    try:
        migrate.downgrade(db_url, "0012")
        with engine.connect() as conn:
            assert conn.execute(state).first() is None
        # The build was cancelled: an index of that name exists and is invalid.
        with engine.begin() as conn:
            conn.execute(text("CREATE INDEX ix_runs_suite_id_created_at ON runs (suite_id)"))
            conn.execute(
                text(
                    "UPDATE pg_index SET indisvalid = false "
                    "WHERE indexrelid = 'ix_runs_suite_id_created_at'::regclass"
                )
            )
        migrate.upgrade(db_url, "0013", timescale="auto")
        with engine.connect() as conn:
            assert conn.execute(state).one() == (True, True)
            definition = "SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_runs_suite_id_created_at'"
            assert "created_at DESC" in conn.execute(text(definition)).scalar_one()
        # Complete already: stamped back to 0012, the same steps change nothing and do not fail.
        with engine.begin() as conn:
            conn.execute(text("UPDATE alembic_version SET version_num = '0012'"))
        migrate.upgrade(db_url, "0013", timescale="auto")
        with engine.connect() as conn:
            assert conn.execute(state).one() == (True, True)
    finally:
        engine.dispose()
        fresh_schema("auto")


def test_0012_adds_check_suites_and_downgrades(db_url, fresh_schema):
    """0012 brings `check_suites`, the runs' suite columns and the claim function, which only
    the app role may call; its downgrade removes all three."""
    fresh_schema("auto")
    engine = create_engine(db_url)
    function = text("SELECT count(*) FROM pg_proc WHERE proname = 'tabayyun_claim_due_suites'")

    def run_columns() -> set[str]:
        return {c["name"] for c in inspect(engine).get_columns("runs")} & {"suite_id", "checks"}

    try:
        with engine.connect() as conn:
            assert conn.execute(function).scalar_one() == 1
            may_call = (
                "SELECT has_function_privilege(:role, 'tabayyun_claim_due_suites(timestamptz)', 'EXECUTE')"
            )
            assert conn.execute(text(may_call), {"role": "tabayyun_app"}).scalar_one() is True
            assert conn.execute(text(may_call), {"role": "public"}).scalar_one() is False
            policies = text("SELECT policyname FROM pg_policies WHERE tablename = 'check_suites'")
            assert conn.execute(policies).scalars().all() == ["tenant"]
        assert run_columns() == {"suite_id", "checks"}
        # 0012 adds the foreign key unvalidated; 0013 validates it and builds the history index
        # concurrently. Both must be complete at head.
        with engine.connect() as conn:
            validated = (
                "SELECT convalidated FROM pg_constraint WHERE conname = 'fk_runs_suite_id_check_suites'"
            )
            assert conn.execute(text(validated)).scalar_one() is True
            index = (
                "SELECT indisvalid FROM pg_index JOIN pg_class ON pg_class.oid = indexrelid "
                "WHERE relname = 'ix_runs_suite_id_created_at'"
            )
            assert conn.execute(text(index)).scalar_one() is True
        migrate.downgrade(db_url, "0011")
        assert _current_revision(db_url) == "0011"
        assert "check_suites" not in inspect(engine).get_table_names() and run_columns() == set()
        with engine.connect() as conn:
            assert conn.execute(function).scalar_one() == 0
        migrate.upgrade(db_url, "head", timescale="auto")
        assert "check_suites" in inspect(engine).get_table_names() and run_columns() == {"suite_id", "checks"}
    finally:
        engine.dispose()
        fresh_schema("auto")


def test_0014_adds_series_baselines_and_downgrades(db_url, fresh_schema):
    """0014 brings `series_baselines` with its policy, `series.baseline_checked_at` and the
    claim function, which only the app role may call; its downgrade removes all three."""
    fresh_schema("auto")
    engine = create_engine(db_url)
    function = text("SELECT count(*) FROM pg_proc WHERE proname = 'tabayyun_claim_series_for_baseline'")
    signature = "tabayyun_claim_series_for_baseline(timestamptz, integer)"

    def series_column() -> bool:
        return "baseline_checked_at" in {c["name"] for c in inspect(engine).get_columns("series")}

    try:
        with engine.connect() as conn:
            assert conn.execute(function).scalar_one() == 1
            may_call = text("SELECT has_function_privilege(:role, :f, 'EXECUTE')")
            assert conn.execute(may_call, {"role": "tabayyun_app", "f": signature}).scalar_one() is True
            assert conn.execute(may_call, {"role": "public", "f": signature}).scalar_one() is False
            policies = text("SELECT policyname FROM pg_policies WHERE tablename = 'series_baselines'")
            assert conn.execute(policies).scalars().all() == ["tenant"]
        assert series_column()
        migrate.downgrade(db_url, "0013")
        assert _current_revision(db_url) == "0013"
        assert "series_baselines" not in inspect(engine).get_table_names() and not series_column()
        with engine.connect() as conn:
            assert conn.execute(function).scalar_one() == 0
        migrate.upgrade(db_url, "head", timescale="auto")
        assert "series_baselines" in inspect(engine).get_table_names() and series_column()
    finally:
        engine.dispose()
        fresh_schema("auto")


def test_0015_adds_the_triage_columns_and_downgrades(db_url, fresh_schema):
    """0015 brings the findings' triage columns, `finding_filters` with its policy and the
    function that opens expired mutes, which only the app role may call; its downgrade
    removes all three and keeps the findings."""
    fresh_schema("auto")
    engine = create_engine(db_url)
    function = text("SELECT count(*) FROM pg_proc WHERE proname = 'tabayyun_reopen_expired_mutes'")
    signature = "tabayyun_reopen_expired_mutes(timestamptz)"
    triage = {"muted_until", "assignee_id", "feedback", "feedback_at"}

    def finding_columns() -> set[str]:
        return {c["name"] for c in inspect(engine).get_columns("findings")} & triage

    try:
        with engine.connect() as conn:
            assert conn.execute(function).scalar_one() == 1
            may_call = text("SELECT has_function_privilege(:role, :f, 'EXECUTE')")
            assert conn.execute(may_call, {"role": "tabayyun_app", "f": signature}).scalar_one() is True
            assert conn.execute(may_call, {"role": "public", "f": signature}).scalar_one() is False
            policies = text("SELECT policyname FROM pg_policies WHERE tablename = 'finding_filters'")
            assert conn.execute(policies).scalars().all() == ["tenant"]
        assert finding_columns() == triage
        migrate.downgrade(db_url, "0014")
        assert _current_revision(db_url) == "0014"
        assert "finding_filters" not in inspect(engine).get_table_names() and finding_columns() == set()
        with engine.connect() as conn:
            assert conn.execute(function).scalar_one() == 0
        migrate.upgrade(db_url, "head", timescale="auto")
        assert "finding_filters" in inspect(engine).get_table_names() and finding_columns() == triage
    finally:
        engine.dispose()
        fresh_schema("auto")


def test_0016_replaces_the_reaper_and_downgrades(db_url, fresh_schema):
    """0016 makes the reaper publish `run.completed`; only the app role may still call it, and
    its downgrade puts the silent version back."""
    fresh_schema("auto")
    engine = create_engine(db_url)
    source = text("SELECT prosrc FROM pg_proc WHERE proname = 'tabayyun_reap_stale_runs'")
    signature = "tabayyun_reap_stale_runs(interval)"
    may_call = text("SELECT has_function_privilege(:role, :f, 'EXECUTE')")

    def check_grants(conn) -> None:
        assert conn.execute(may_call, {"role": "tabayyun_app", "f": signature}).scalar_one() is True
        assert conn.execute(may_call, {"role": "public", "f": signature}).scalar_one() is False

    try:
        with engine.connect() as conn:
            assert "pg_notify('tabayyun_events'" in conn.execute(source).scalar_one()
            check_grants(conn)
        migrate.downgrade(db_url, "0015")
        assert _current_revision(db_url) == "0015"
        with engine.connect() as conn:
            assert "pg_notify" not in conn.execute(source).scalar_one()
            check_grants(conn)
        migrate.upgrade(db_url, "head", timescale="auto")
        with engine.connect() as conn:
            assert "pg_notify('tabayyun_events'" in conn.execute(source).scalar_one()
            check_grants(conn)
    finally:
        engine.dispose()
        fresh_schema("auto")


def test_0017_adds_the_alert_tables_and_downgrades(db_url, fresh_schema):
    """0017 brings `alert_rules` and `alert_notifications`, each with the tenant policy, the
    runs' evaluation mark and the function that requeues stuck notifications, which only the
    app role may call; its downgrade removes all of them."""
    fresh_schema("auto")
    engine = create_engine(db_url)
    tables = {"alert_rules", "alert_notifications"}
    policies = text(
        "SELECT tablename FROM pg_policies WHERE policyname = 'tenant' AND tablename LIKE 'alert\\_%'"
    )

    function = text("SELECT count(*) FROM pg_proc WHERE proname = 'tabayyun_requeue_stuck_alerts'")
    signature = "tabayyun_requeue_stuck_alerts(timestamptz)"
    may_call = text("SELECT has_function_privilege(:role, :f, 'EXECUTE')")

    def present() -> set[str]:
        return set(inspect(engine).get_table_names()) & tables

    def run_columns() -> set[str]:
        return {c["name"] for c in inspect(engine).get_columns("runs")} & {"alerts_evaluated_at"}

    try:
        with engine.connect() as conn:
            assert set(conn.execute(policies).scalars()) == tables
        indexes = {i["name"] for i in inspect(engine).get_indexes("alert_notifications")}
        assert "ix_alert_notifications_rule_id_created_at" in indexes
        migrate.downgrade(db_url, "0016")
        assert _current_revision(db_url) == "0016" and present() == set() and run_columns() == set()
        with engine.connect() as conn:
            assert conn.execute(function).scalar_one() == 0
        migrate.upgrade(db_url, "head", timescale="auto")
        assert present() == tables and run_columns() == {"alerts_evaluated_at"}
        with engine.connect() as conn:
            assert conn.execute(may_call, {"role": "tabayyun_app", "f": signature}).scalar_one() is True
            assert conn.execute(may_call, {"role": "public", "f": signature}).scalar_one() is False
        with engine.connect() as conn:
            assert set(conn.execute(policies).scalars()) == tables
    finally:
        engine.dispose()
        fresh_schema("auto")
