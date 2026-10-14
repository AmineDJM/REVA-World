"""Calibration of the tournament promotion rule (review R3-02 / R3-07).

Size: exact-null panels in which challenger and incumbent have *equal expected
loss* by construction. Both are mirror-image point forecasters on an additive
random walk with symmetric innovations: challenger ``last + d*h``, incumbent
``last - d*h``. For symmetric forecast errors ``e``, ``E|e - dh| = E|e + dh|``
conditionally on the past, and both share the same MASE scale, so the
expected scaled-loss differential is exactly zero at every case. Innovations
share a common factor across series (correlation ``rho``), so a rule that
treated series-cases as independent would be oversized. The false-promotion
rate must stay at the nominal one-sided level (alpha = 0.025) up to Monte-Carlo
error: the old moving-block bootstrap (L=3, cases ordered by series) promoted
11-15% of the time in this design.

Power: a log random walk with true drift (0.02, sd 0.03), where the log-drift
model is truly better than the naive baseline. The probability of promotion
must be substantial and must *increase* with the number of series (the old
point-estimate subgroup gate made it fall from 0.63 to 0.15 as the panel grew
from 8 to 32 series).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from reva.kernel.uncertainty import UncertaintySpec
from reva.models.forecasting import LastValue, LogDrift
from reva.models.forecasting.base import Forecast, prepare
from reva.models.tournament import run_tournament

POLICY = {"bootstrap": {"replicates": 100}}  # descriptive interval only; the test is DM-HLN + Holm
ALPHA = 0.025


@dataclass(frozen=True)
class _FittedOffset:
    model_key: str
    origin_year: int
    last: float
    slope: float
    model_version: str = "0.0.0"

    def predict(self, horizons, level=0.8):
        return [
            Forecast(self.model_key, self.model_version, self.origin_year, self.origin_year + h, h,
                     self.last + self.slope * h, None, None, None, UncertaintySpec.unknown("synthetic"))
            for h in horizons
        ]


class Offset:
    version = "0.0.0"
    min_train = 1

    def __init__(self, key: str, slope: float) -> None:
        self.key, self.slope = key, slope

    def fit(self, train):
        s = prepare(train)
        return _FittedOffset(self.key, s.years[-1], s.values[-1], self.slope)


def _null_panel(rng: np.random.Generator, n_series: int, n_years: int, rho: float):
    common = rng.standard_normal(n_years)
    return {
        f"s{i}": [
            (2000 + t, float(v))
            for t, v in enumerate(
                100.0 + np.cumsum(math.sqrt(rho) * common
                                  + math.sqrt(1.0 - rho) * rng.standard_normal(n_years))
            )
        ]
        for i in range(n_series)
    }


def _false_promotion_rate(reps: int, n_series: int, n_years: int, rho: float, seed: int) -> float:
    rng = np.random.default_rng(seed)
    promoted = 0
    for r in range(reps):
        res = run_tournament(
            _null_panel(rng, n_series, n_years, rho),
            [Offset("challenger", +0.5), Offset("incumbent", -0.5)],
            "incumbent",
            (1, 2, 3),
            replay_mode="synthetic_exact_null",
            seed=r,
            policy=POLICY,
        )
        d = res.decisions["challenger"]
        assert d.alpha == ALPHA and d.test_method == "dm_hln_holm"
        promoted += d.decision == "promote"
    return promoted / reps


def test_false_promotion_rate_at_nominal_level_under_exact_null():
    # 400 panels: MC SE at p = 0.025 is 0.0078, so 0.05 is > 3 SE above nominal.
    # (With seed 20261014 the observed rate is reported in the assertion message.)
    rate = _false_promotion_rate(400, n_series=8, n_years=23, rho=0.5, seed=20261014)
    assert rate <= 0.05, f"false-promotion rate {rate:.3f} > 0.05 (nominal {ALPHA})"


def test_false_promotion_rate_short_panel_few_series():
    rate = _false_promotion_rate(200, n_series=3, n_years=20, rho=0.0, seed=7)
    # MC SE at 0.025 with 200 panels is 0.011.
    assert rate <= 0.06, f"false-promotion rate {rate:.3f} > 0.06 (nominal {ALPHA})"


def _drift_power(n_series: int, reps: int, seed: int) -> tuple[float, float]:
    rng = np.random.default_rng(seed)
    promote = review = 0
    for r in range(reps):
        panel = {
            f"s{i}": [(2000 + t, float(math.exp(v)))
                      for t, v in enumerate(10.0 + np.cumsum(rng.normal(0.02, 0.03, 23)))]
            for i in range(n_series)
        }
        d = run_tournament(panel, [LastValue(), LogDrift()], "forecast.last_value", (1, 2, 3),
                           replay_mode="synthetic", seed=r, policy=POLICY).decisions["forecast.log_drift"]
        promote += d.decision == "promote"
        review += d.decision == "needs_review"
    return promote / reps, review / reps


def test_power_under_true_improvement_grows_with_panel_size():
    p8, r8 = _drift_power(8, 40, seed=8)
    p24, r24 = _drift_power(24, 40, seed=24)
    assert p8 >= 0.45, p8
    assert p24 >= 0.85, p24
    assert p24 >= p8
    # Noisy subgroup point estimates above +10% no longer block a truly better model.
    assert r8 <= 0.1 and r24 <= 0.1, (r8, r24)
