import pytest
from pydantic import ValidationError

from tabayyun.settings import Settings


def test_timescale_mode_values(monkeypatch):
    assert Settings().timescale == "auto"
    for mode in ("auto", "on", "off"):
        assert Settings(timescale=mode).timescale == mode
    with pytest.raises(ValidationError):
        Settings(timescale="maybe")
    monkeypatch.setenv("TABAYYUN_TIMESCALE", "off")
    assert Settings().timescale == "off"


def test_pool_settings(monkeypatch):
    s = Settings()
    assert (s.db_pool_size, s.db_pool_max_overflow) == (5, 10)
    monkeypatch.setenv("TABAYYUN_DB_POOL_SIZE", "12")
    monkeypatch.setenv("TABAYYUN_DB_POOL_MAX_OVERFLOW", "0")
    s = Settings()
    assert (s.db_pool_size, s.db_pool_max_overflow) == (12, 0)
    with pytest.raises(ValidationError):
        Settings(db_pool_size=0)


def test_test_database_url_defaults_to_unset(monkeypatch):
    monkeypatch.delenv("TABAYYUN_TEST_DATABASE_URL", raising=False)
    assert Settings().test_database_url is None
