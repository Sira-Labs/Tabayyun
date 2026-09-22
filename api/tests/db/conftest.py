"""Database tests run only when TABAYYUN_TEST_DATABASE_URL points at a PostgreSQL 17 (with
or without TimescaleDB); otherwise every test in this package skips. Each test that needs a
known state calls `fresh_schema(mode)`, which empties the schema and migrates to head."""

from collections.abc import Callable

import pytest
from sqlalchemy import create_engine, text

from tabayyun.db import migrate
from tabayyun.settings import Settings


@pytest.fixture(scope="session")
def db_url() -> str:
    url = Settings().test_database_url
    if not url:
        pytest.skip("TABAYYUN_TEST_DATABASE_URL not set")
    return url


@pytest.fixture(scope="session")
def timescale_available(db_url: str) -> bool:
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
    def _fresh(mode: str = "auto") -> None:
        migrate.downgrade(db_url, "base", timescale=mode)
        migrate.upgrade(db_url, "head", timescale=mode)

    return _fresh
