"""forecast.last_value 1.0.0 — the naive (no-change) baseline.

Point forecast for every horizon is the last training value. No probabilistic
interval is claimed: uncertainty is ``unquantified`` / ``unknown``. This model
is the permanent benchmark and is always evaluated in tournaments.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from reva.kernel.uncertainty import UncertaintySpec
from reva.models.forecasting.base import (
    Forecast,
    IneligibleReport,
    Observation,
    check_horizons,
    check_level,
    prepare,
)

KEY = "forecast.last_value"
VERSION = "1.0.0"
ASSUMPTIONS = (
    "random-walk point forecast: future value equals last observed training value",
    "no probabilistic interval claimed",
)


@dataclass(frozen=True)
class FittedLastValue:
    model_key: str
    model_version: str
    origin_year: int
    last_value: float

    def predict(self, horizons: Sequence[int], level: float = 0.8) -> list[Forecast]:
        check_level(level)  # validated for interface uniformity; not used
        unc = UncertaintySpec.unknown("naive baseline; no probabilistic interval claimed")
        return [
            Forecast(
                model_key=self.model_key,
                model_version=self.model_version,
                origin_year=self.origin_year,
                target_year=self.origin_year + h,
                horizon=h,
                point=self.last_value,
                interval=None,
                interval_kind=None,
                level=None,
                uncertainty=unc,
                assumptions=ASSUMPTIONS,
            )
            for h in check_horizons(horizons)
        ]


class LastValue:
    key = KEY
    version = VERSION
    min_train = 1

    def fit(self, train: Sequence[Observation]) -> FittedLastValue | IneligibleReport:
        s = prepare(train)
        if s.n < self.min_train:
            return IneligibleReport(self.key, self.version, "insufficient_train", s.n)
        if s.problems:
            return IneligibleReport(self.key, self.version, s.problems[0], s.n)
        return FittedLastValue(self.key, self.version, s.years[-1], s.values[-1])
