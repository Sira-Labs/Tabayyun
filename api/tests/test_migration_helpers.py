"""Pure helpers of the migration scripts, tested without a database."""

import importlib.util

from tabayyun.db.migrate import MIGRATIONS_DIR


def _load_0001():
    """Import migration 0001 as a module (the alembic `op` proxy is inert outside a run)."""
    path = MIGRATIONS_DIR / "versions" / "0001_initial.py"
    spec = importlib.util.spec_from_file_location("migration_0001", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_preloaded_libraries_accepts_paths_quotes_and_suffixes():
    """Bare names, $libdir paths, quoted entries and .so suffixes all resolve to the library name."""
    m = _load_0001()
    assert m._preloaded_libraries("timescaledb") == {"timescaledb"}
    assert m._preloaded_libraries("pg_stat_statements, $libdir/timescaledb") == {
        "pg_stat_statements",
        "timescaledb",
    }
    assert m._preloaded_libraries("'/usr/lib/postgresql/17/lib/timescaledb.so'") == {"timescaledb"}
    assert m._preloaded_libraries("") == set()
    assert m._preloaded_libraries(None) == set()


def _load(name: str):
    """Import a migration as a module (the alembic `op` proxy is inert outside a run)."""
    path = MIGRATIONS_DIR / "versions" / name
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# Procrastinate's migrations since the 3.9.0 snapshot of revision 0002, and our copy of each.
# None: not applied yet (see revision 0019 for why the 3.12 "post" steps wait).
PROCRASTINATE_MIGRATIONS = {
    "03.11.00_01_pre_fetch_job_skip_null_lock.sql": "procrastinate_3.11.0_fetch_job_skip_null_lock.sql",
    "03.12.00_01_pre_add_lock_mode.sql": "procrastinate_3.12.0_01_add_lock_mode.sql",
    "03.12.00_02_pre_retry_job_fail_on_queueing_lock_conflict.sql": (
        "procrastinate_3.12.0_02_retry_job_fail_on_queueing_lock_conflict.sql"
    ),
    "03.12.00_03_pre_add_todo_doing_lock_index.sql": "procrastinate_3.12.0_03_add_todo_doing_lock_index.sql",
    "03.12.00_50_post_add_lock_mode.sql": None,
    "03.12.00_51_post_drop_id_lock_index.sql": None,
}


def test_procrastinate_migrations_since_the_snapshot_are_all_accounted_for():
    """Revision 0002 applies the 3.9.0 snapshot, 0011 and 0019 the migrations shipped since.

    A Procrastinate upgrade that ships another migration file fails here: add a revision that
    runs it, with a copy under `migrations/sql/`, or list it as waiting with the reason.
    """
    from importlib import resources

    shipped = resources.files("procrastinate.sql").joinpath("migrations")
    since_snapshot = sorted(
        f.name for f in shipped.iterdir() if f.name.endswith(".sql") and f.name[:8] > "03.09.00"
    )
    assert since_snapshot == sorted(PROCRASTINATE_MIGRATIONS)
    for name, copy in PROCRASTINATE_MIGRATIONS.items():
        if copy is not None:
            assert (MIGRATIONS_DIR / "sql" / copy).read_text() == shipped.joinpath(name).read_text(), name


def test_0019_restores_the_functions_of_3_11_from_the_snapshot():
    """The downgrade of 0019 finds each function it puts back, whole, in the 3.9.0 snapshot."""
    m = _load("0019_procrastinate_3_12.py")
    snapshot = m.SCHEMA_3_9.read_text()
    for header in m.RESTORED_FROM_3_9:
        block = m.function_block(snapshot, header)
        assert block.startswith(header) and block.rstrip().endswith("$$;")
        assert "lock_mode" not in block and "procrastinate_defer_jobs_v2" not in block


def test_function_block_reports_a_missing_function():
    """A schema without the function is an error, not an empty downgrade."""
    import pytest

    for name in ("0011_procrastinate_3_11.py", "0019_procrastinate_3_12.py"):
        m = _load(name)
        with pytest.raises(ValueError, match="no complete function"):
            m.function_block("CREATE TABLE x ();\n", "CREATE FUNCTION procrastinate_fetch_job_v2(")
