"""Whether a finding or a score matches a rule's condition (spec 031). No I/O."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tabayyun.services.findings import SEVERITIES

OVERALL = "overall"
_RANK = {severity: rank for rank, severity in enumerate(SEVERITIES)}


def finding_matches(
    condition: dict[str, Any], *, severity: str, check_id: str, dimension: str, asset_path: str | None
) -> bool:
    """A new finding matches when it is severe enough and, where the condition names checks,
    dimensions or an asset prefix, it is among them."""
    if _RANK.get(severity, -1) < _RANK[condition["min_severity"]]:
        return False
    checks = condition.get("check_ids") or []
    if checks and check_id not in checks:
        return False
    dimensions = condition.get("dimensions") or []
    if dimensions and dimension not in dimensions:
        return False
    prefix = condition.get("asset_prefix")
    return not prefix or (asset_path or "").startswith(prefix)


@dataclass(frozen=True)
class ScoreValues:
    """The numbers of one stored score."""

    overall: float
    dimensions: dict[str, Any]

    def of(self, metric: str) -> float | None:
        """The overall score or one dimension's; None when the score has no such dimension."""
        value = self.overall if metric == OVERALL else self.dimensions.get(metric)
        return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


@dataclass(frozen=True)
class Crossing:
    """A series whose score fell below a rule's threshold."""

    metric: str
    below: float
    score: float
    previous: float | None


def score_crossing(
    condition: dict[str, Any], current: ScoreValues, previous: ScoreValues | None
) -> Crossing | None:
    """The crossing when `current` is below the threshold and the score before was not (or
    there was none): a series that stays below is told once."""
    metric, below = condition["metric"], float(condition["below"])
    now = current.of(metric)
    if now is None or now >= below:
        return None
    before = None if previous is None else previous.of(metric)
    if before is not None and before < below:
        return None
    return Crossing(metric=metric, below=below, score=now, previous=before)
