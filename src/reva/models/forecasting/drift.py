"""forecast.log_drift 1.0.0 — random walk with drift on natural logs.

Model (fitted only on the training window ``y_1..y_n``, consecutive years)::

    z_t = log(y_t),   d_t = z_t - z_{t-1}   (t = 2..n, T = n - 1 differences)
    drift  c   = mean(d)                     = (z_n - z_1) / T
    sigma^2    = sample variance of d (ddof=1)

Point forecast at horizon h: ``exp(z_n + h * c)`` (the exponentiated log-scale
mean, i.e. the predictive *median* under log-normality, not the mean).

Prediction interval (log scale, Gaussian quantiles), accounting for both the
accumulated innovation variance and the estimation error in the drift::

    Var[z_{n+h} - (z_n + h c)] = sigma^2 * h * (1 + h / T)

    log-PI = z_n + h c  +/-  q_{(1+level)/2} * sigma * sqrt(h * (1 + h / T))

and the bounds are exponentiated. ``sigma`` is plugged in (no Student-t
correction). Strictly positive series only: any value <= 0 makes the model
ineligible (``non_positive_values``); values are never clipped.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from scipy.stats import norm

from reva.models.forecasting.base import (
    Forecast,
    IneligibleReport,
    Observation,
    check_horizons,
    check_level,
    prediction_interval_spec,
    prepare,
)

KEY = "forecast.log_drift"
VERSION = "1.0.0"
METHOD_VERSION = f"{KEY}@{VERSION}"
ASSUMPTIONS = (
    "random walk with constant drift on natural-log values",
    "Gaussian i.i.d. log-differences; sigma plugged in (no t correction)",
    "log transform explicit; non-positive values make the model ineligible (no clipping)",
    "point is exp of the log-scale mean (predictive median)",
    "parameters estimated only within the training window",
)


@dataclass(frozen=True)
class FittedLogDrift:
    model_key: str
    model_version: str
    origin_year: int
    log_last: float
    drift_mean: float
    resid_sd: float
    n_diffs: int

    def predict(self, horizons: Sequence[int], level: float = 0.8) -> list[Forecast]:
        lvl = check_level(level)
        q = float(norm.ppf(0.5 + lvl / 2.0))
        out: list[Forecast] = []
        for h in check_horizons(horizons):
            centre = self.log_last + h * self.drift_mean
            half = q * self.resid_sd * math.sqrt(h * (1.0 + h / self.n_diffs))
            point = math.exp(centre)
            lo, hi = math.exp(centre - half), math.exp(centre + half)
            out.append(
                Forecast(
                    model_key=self.model_key,
                    model_version=self.model_version,
                    origin_year=self.origin_year,
                    target_year=self.origin_year + h,
                    horizon=h,
                    point=point,
                    interval=(lo, hi),
                    interval_kind="prediction_interval",
                    level=lvl,
                    uncertainty=prediction_interval_spec(
                        method_version=METHOD_VERSION,
                        level=lvl,
                        lower=lo,
                        upper=hi,
                        description=(
                            "Gaussian log-scale prediction interval, "
                            "sd*sqrt(h*(1+h/T)), exponentiated"
                        ),
                        conditioning=ASSUMPTIONS,
                    ),
                    assumptions=ASSUMPTIONS,
                )
            )
        return out


class LogDrift:
    key = KEY
    version = VERSION
    min_train = 3

    def fit(self, train: Sequence[Observation]) -> FittedLogDrift | IneligibleReport:
        s = prepare(train)
        if s.problems:
            return IneligibleReport(self.key, self.version, s.problems[0], s.n)
        if any(v <= 0.0 for v in s.values):
            return IneligibleReport(self.key, self.version, "non_positive_values", s.n)
        if s.n < self.min_train:
            return IneligibleReport(self.key, self.version, "insufficient_train", s.n)
        if not s.consecutive:
            return IneligibleReport(self.key, self.version, "non_consecutive_years", s.n)
        z = np.log(np.asarray(s.values, dtype=float))
        d = np.diff(z)
        return FittedLogDrift(
            model_key=self.key,
            model_version=self.version,
            origin_year=s.years[-1],
            log_last=float(z[-1]),
            drift_mean=float(d.mean()),
            resid_sd=float(d.std(ddof=1)),
            n_diffs=int(d.size),
        )
