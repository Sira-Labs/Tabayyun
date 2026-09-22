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


def test_procrastinate_schema_snapshot_matches_installed_package():
    """Migration 0002 applies a repository snapshot; a Procrastinate upgrade needs a new revision."""
    from importlib import resources

    snapshot = (MIGRATIONS_DIR / "sql" / "procrastinate_schema_3.9.0.sql").read_text()
    installed = resources.files("procrastinate.sql").joinpath("schema.sql").read_text()
    assert snapshot == installed, "Procrastinate schema changed: add a revision with its migration SQL"
