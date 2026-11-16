"""The thresholds a baseline gives the checks (spec 028), as the series page shows them."""

from __future__ import annotations

import pytest

from tabayyun.db.models import Series
from tabayyun.services.baselines import learned_thresholds


def _learned(profile: dict, **series) -> dict[tuple[str, str], dict]:
    return {(t["check_id"], t["param"]): t for t in learned_thresholds(profile, Series(**series))}


def test_one_entry_for_every_parameter_a_series_baseline_teaches():
    learned = _learned({"rate_p999": 0.5, "resolution": 0.1, "noise_mad": 0.3})
    assert set(learned) == {("tby.rate_of_change", "max_rate"), ("tby.spikes", "min_scale")}
    assert learned["tby.rate_of_change", "max_rate"]["value"] == pytest.approx(1.5)
    assert learned["tby.spikes", "min_scale"]["value"] == 0.3


@pytest.mark.parametrize(
    ("profile", "declared", "value"),
    [
        ({"resolution": 0.1, "noise_mad": 0.3}, None, 0.3),
        ({"resolution": 0.5, "noise_mad": 0.3}, None, 0.5),
        ({"resolution": 0.5, "noise_mad": None}, None, 0.5),
        # The declared resolution wins over the learned one, as in the check.
        ({"resolution": 0.1, "noise_mad": 0.3}, 2.0, 2.0),
        ({"resolution": None, "noise_mad": 0.0}, 2.0, 2.0),
        # A constant baseline has no resolution: the check reads it from the run's window, so
        # no number is claimed here, whatever the noise floor.
        ({"resolution": None, "noise_mad": 0.0}, None, None),
        ({"resolution": None, "noise_mad": 0.3}, None, None),
        ({}, None, None),
    ],
)
def test_spike_floor_claims_a_number_only_when_the_check_would_use_it(profile, declared, value):
    floor = _learned(profile, resolution=declared)["tby.spikes", "min_scale"]
    assert floor["value"] == value
    if value is None:
        assert "reads it from the window it checks" in floor["basis"]


@pytest.mark.parametrize("rate", [None, 0, 0.0, -1.0])
def test_rate_limit_needs_a_rate(rate):
    limit = _learned({"rate_p999": rate})["tby.rate_of_change", "max_rate"]
    assert limit["value"] is None and "no change between samples" in limit["basis"]
