"""Database tests run only when TABAYYUN_TEST_DATABASE_URL points at a PostgreSQL 17 (with
or without TimescaleDB); otherwise every test in this package skips. Each test that needs a
known state calls `fresh_schema(mode)`, which empties the schema and migrates to head."""

from collections.abc import Callable

import pytest
from sqlalchemy import create_engine, make_url, text

from tabayyun.db import migrate
from tabayyun.settings import Settings

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def assert_test_database(url: str) -> None:
    """Refuse to reset a database that is not clearly dedicated to tests.

    `fresh_schema` downgrades to base, which drops every table. Accepted targets: a server on
    the local host, or a database whose name contains `test`. Anything else raises before a
    single statement runs.
    """
    parsed = make_url(url)
    host = (parsed.host or "").lower()
    database = (parsed.database or "").lower()
    if host in LOCAL_HOSTS or "test" in database:
        return
    raise RuntimeError(
        "TABAYYUN_TEST_DATABASE_URL must point at a local server or a database named *test*: "
        f"refusing to reset {host}/{database}"
    )


@pytest.fixture(scope="session")
def db_url() -> str:
    """Test database URL from settings; skips the package when unset."""
    url = Settings().test_database_url
    if not url:
        pytest.skip("TABAYYUN_TEST_DATABASE_URL not set")
    assert_test_database(url)
    return url


@pytest.fixture(scope="session")
def timescale_available(db_url: str) -> bool:
    """Whether the server can install the timescaledb extension."""
    engine = create_engine(db_url)
    try:
        with engine.connect() as conn:
            found = conn.execute(
                text("SELECT 1 FROM pg_available_extensions WHERE name = 'timescaledb'")
            ).scalar()
    finally:
        engine.dispose()
    return bool(found)


@pytest.fixture
def fresh_schema(db_url: str) -> Callable[[str], None]:
    """Callable that empties the schema and migrates to head with the given TimescaleDB mode."""

    def _fresh(mode: str = "auto") -> None:
        """Downgrade to base and upgrade to head with the given TimescaleDB mode."""
        assert_test_database(db_url)
        migrate.downgrade(db_url, "base", timescale=mode)
        migrate.upgrade(db_url, "head", timescale=mode)

    return _fresh
