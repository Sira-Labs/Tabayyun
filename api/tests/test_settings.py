import pytest
from pydantic import ValidationError

from tabayyun.settings import Settings

DB_ENV = ("TABAYYUN_TIMESCALE", "TABAYYUN_DB_POOL_SIZE", "TABAYYUN_DB_POOL_MAX_OVERFLOW")


def _clear_db_env(monkeypatch) -> None:
    """Remove the database variables so defaults are observable."""
    for name in DB_ENV:
        monkeypatch.delenv(name, raising=False)


def test_timescale_mode_values(monkeypatch):
    """auto is the default; only auto, on and off are accepted; the env var overrides."""
    _clear_db_env(monkeypatch)
    assert Settings().timescale == "auto"
    for mode in ("auto", "on", "off"):
        assert Settings(timescale=mode).timescale == mode
    with pytest.raises(ValidationError):
        Settings(timescale="maybe")
    monkeypatch.setenv("TABAYYUN_TIMESCALE", "off")
    assert Settings().timescale == "off"


def test_pool_settings(monkeypatch):
    """Pool defaults are 5/10, env vars override, and a zero pool size is rejected."""
    _clear_db_env(monkeypatch)
    s = Settings()
    assert (s.db_pool_size, s.db_pool_max_overflow) == (5, 10)
    monkeypatch.setenv("TABAYYUN_DB_POOL_SIZE", "12")
    monkeypatch.setenv("TABAYYUN_DB_POOL_MAX_OVERFLOW", "0")
    s = Settings()
    assert (s.db_pool_size, s.db_pool_max_overflow) == (12, 0)
    with pytest.raises(ValidationError):
        Settings(db_pool_size=0)


def test_job_settings(monkeypatch):
    """Jobs are enqueued by default; inline mode and worker concurrency come from the env."""
    monkeypatch.delenv("TABAYYUN_INLINE_JOBS", raising=False)
    monkeypatch.delenv("TABAYYUN_WORKER_CONCURRENCY", raising=False)
    s = Settings()
    assert (s.inline_jobs, s.worker_concurrency) == (False, 2)
    monkeypatch.setenv("TABAYYUN_INLINE_JOBS", "true")
    monkeypatch.setenv("TABAYYUN_WORKER_CONCURRENCY", "4")
    s = Settings()
    assert (s.inline_jobs, s.worker_concurrency) == (True, 4)
    with pytest.raises(ValidationError):
        Settings(worker_concurrency=0)


def test_test_database_url_defaults_to_unset(monkeypatch):
    """The database tests skip unless the URL is set explicitly."""
    monkeypatch.delenv("TABAYYUN_TEST_DATABASE_URL", raising=False)
    assert Settings().test_database_url is None
