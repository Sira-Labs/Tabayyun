"""RunCache turns every ordinary failure into a one-line CacheError (spec 006)."""

import pyarrow as pa
import pytest

from tabayyun.services.cache import CacheError, RunCache

TABLE = pa.table({"ts": pa.array([1_700_000_000_000_000_000], pa.int64()), "value": [1.0]})


def _write(cache: RunCache) -> None:
    cache.write_series(
        source_id="src",
        series_id="s",
        table=TABLE,
        ts_col="ts",
        value_col="value",
        quality_col=None,
        ingest_col=None,
    )


@pytest.mark.parametrize(
    ("exc", "message"),
    [
        (ValueError("cache error: put x: access denied\nmore detail"), "cache error: put x: access denied"),
        (ValueError(""), "ValueError"),
        (RuntimeError("   "), "RuntimeError"),
        (TypeError("bad store config"), "bad store config"),
    ],
)
def test_failures_become_cache_errors(monkeypatch, exc, message):
    """The first line of the message, or the type name when there is none."""
    cache = RunCache({"url": "unused"})

    def boom():
        raise exc

    monkeypatch.setattr(cache, "_open", boom)
    with pytest.raises(CacheError) as err:
        _write(cache)
    assert str(err.value) == message
    assert err.value.__cause__ is exc


def test_message_is_capped(monkeypatch):
    cache = RunCache({"url": "unused"})

    def boom():
        raise ValueError("x" * 2000)

    monkeypatch.setattr(cache, "_open", boom)
    with pytest.raises(CacheError) as err:
        _write(cache)
    assert len(str(err.value)) == 500


def test_bad_url_is_a_cache_error(tmp_path):
    """An unsupported scheme fails the write, not the process."""
    with pytest.raises(CacheError, match="unsupported"):
        _write(RunCache({"url": "gs://bucket"}))
