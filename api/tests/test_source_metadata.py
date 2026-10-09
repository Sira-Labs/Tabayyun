"""The metadata-import rules (spec 022): fill empty fields, overwrite on request, never clear,
skip what does not fit, keep valid limits, and the connector's blob."""

from __future__ import annotations

import math
from datetime import UTC, datetime

from tabayyun.connectors import PointMetadata
from tabayyun.db.models import Series
from tabayyun.services.source_metadata import apply

NOW = datetime(2026, 10, 10, 8, 0, tzinfo=UTC)


def series(**values) -> Series:
    defaults = {
        "unit": None,
        "physical_min": None,
        "physical_max": None,
        "operational_min": None,
        "operational_max": None,
        "asset_path": None,
        "metadata_": {},
    }
    return Series(**{**defaults, **values})


def test_fills_empty_fields_and_keeps_edited_ones():
    s = series(unit="t/h", metadata_={"note": "kept"})
    meta = PointMetadata(
        unit="m3/h", physical_min=0.0, physical_max=200.0, asset_path="\\\\AF\\DB\\E", extra={"compdev": 0.2}
    )
    applied = apply(s, meta, source_type="pi_web_api", overwrite=False, now=NOW)
    assert applied.changed == ["physical_min", "physical_max", "asset_path", "metadata"]
    assert (s.unit, s.physical_min, s.physical_max) == ("t/h", 0.0, 200.0)
    assert s.metadata_["note"] == "kept"
    assert s.metadata_["pi_web_api"] == {"compdev": 0.2, "imported_at": NOW.isoformat()}


def test_overwrite_replaces_but_never_clears():
    s = series(unit="t/h", physical_max=500.0, operational_max=80.0)
    applied = apply(
        s, PointMetadata(unit="m3/h", physical_max=120.0), source_type="x", overwrite=True, now=NOW
    )
    assert applied.changed == ["unit", "physical_max"]  # an empty blob was empty before too
    assert (s.unit, s.physical_max, s.operational_max) == ("m3/h", 120.0, 80.0)


def test_unchanged_import_changes_nothing_but_the_time():
    s = series()
    meta = PointMetadata(unit="m", description="Tank", extra={"step": False})
    apply(s, meta, source_type="x", overwrite=False, now=NOW)
    again = apply(s, meta, source_type="x", overwrite=False, now=NOW.replace(hour=9))
    assert again.changed == [] and s.metadata_["x"]["imported_at"] == NOW.replace(hour=9).isoformat()
    assert s.metadata_["x"]["description"] == "Tank"


def test_invalid_limits_are_skipped_and_the_rest_applies():
    s = series(physical_max=10.0)
    meta = PointMetadata(unit="m", physical_min=50.0, operational_min=1.0, operational_max=5.0)
    applied = apply(s, meta, source_type="x", overwrite=False, now=NOW)
    assert applied.changed == ["unit"]
    assert applied.skipped == ["limits: physical_min must be below physical_max"]
    assert (s.physical_min, s.operational_min) == (None, None)


def test_values_that_do_not_fit_are_skipped():
    s = series()
    meta = PointMetadata(unit="x" * 33, physical_min=math.inf, asset_path="a" * 513)
    applied = apply(s, meta, source_type="x", overwrite=False, now=NOW)
    assert applied.changed == []
    assert applied.skipped == [
        "unit: longer than 32 characters",
        "physical_min: not a finite number",
        "asset_path: longer than 512 characters",
    ]
    assert applied.as_result() == {"changed": [], "skipped": applied.skipped}


def test_a_blob_over_8_kib_is_not_stored():
    s = series()
    applied = apply(
        s, PointMetadata(unit="m", extra={"big": "x" * 9000}), source_type="x", overwrite=False, now=NOW
    )
    assert applied.changed == ["unit"] and applied.skipped[0].startswith("metadata: larger than")
    assert s.metadata_ == {}
