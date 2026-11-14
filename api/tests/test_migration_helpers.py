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


def test_procrastinate_migrations_since_the_snapshot_are_all_applied():
    """Revision 0002 applies the 3.9.0 snapshot and 0011 the one migration shipped since.

    A Procrastinate upgrade that ships another migration file fails here: add a revision that
    runs it, with a copy under `migrations/sql/`.
    """
    from importlib import resources

    shipped = resources.files("procrastinate.sql").joinpath("migrations")
    since_snapshot = sorted(
        f.name for f in shipped.iterdir() if f.name.endswith(".sql") and f.name[:8] > "03.09.00"
    )
    assert since_snapshot == ["03.11.00_01_pre_fetch_job_skip_null_lock.sql"]
    ours = (MIGRATIONS_DIR / "sql" / "procrastinate_3.11.0_fetch_job_skip_null_lock.sql").read_text()
    assert ours == shipped.joinpath(since_snapshot[0]).read_text()


def test_procrastinate_schema_differs_from_the_snapshot_only_in_fetch_job():
    """Outside `procrastinate_fetch_job_v2`, the installed schema is the 3.9.0 snapshot, and
    the installed function is the one revision 0011 installs."""
    from importlib import resources

    m = _load("0011_procrastinate_3_11.py")
    snapshot = (MIGRATIONS_DIR / "sql" / "procrastinate_schema_3.9.0.sql").read_text()
    installed = resources.files("procrastinate.sql").joinpath("schema.sql").read_text()
    old, new = m.function_block(snapshot), m.function_block(installed)
    assert old != new
    assert snapshot.replace(old, "") == installed.replace(new, "")
    migration = m.MIGRATION_3_11.read_text()
    assert m.function_block(
        migration, "CREATE OR REPLACE FUNCTION procrastinate_fetch_job_v2("
    ) == new.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)


def test_function_block_reports_a_missing_function():
    """A schema without the function is an error, not an empty downgrade."""
    import pytest

    m = _load("0011_procrastinate_3_11.py")
    with pytest.raises(ValueError, match="no complete function"):
        m.function_block("CREATE TABLE x ();\n")
