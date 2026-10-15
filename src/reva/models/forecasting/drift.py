"""forecast.log_drift 1.0.0 — random walk with drift on natural logs.

Model (fitted only on the training window ``y_1..y_n``, consecutive years)::

    z_t = log(y_t),   d_t = z_t - z_{t-1}   (t = 2..n, T = n - 1 differences)
    drift  c   = mean(d)                     = (z_n - z_1) / T
    s^2        = sample variance of d (ddof=1), T - 1 degrees of freedom

Point forecast at horizon h: ``exp(z_n + h * c)`` (the exponentiated log-scale
mean, i.e. the predictive *median* under log-normality, not the mean).

Prediction interval (log scale). Under Gaussian i.i.d. log-differences the
forecast error ``z_{n+h} - (z_n + h c)`` is normal with variance
``sigma^2 * h * (1 + h / T)`` (accumulated innovations plus drift estimation
error; Hyndman & Athanasopoulos, *FPP*, drift method) and is independent of
``s^2``, so the studentised error is **exactly** Student-t with ``T - 1``
degrees of freedom::

    log-PI = z_n + h c  +/-  t_{T-1, (1+level)/2} * s * sqrt(h * (1 + h / T))

and the bounds are exponentiated. A normal quantile with a plug-in ``s`` (the
1.0.0 draft) under-covers badly in short windows (nominal 0.80 covered 0.58 at
n=3 and 0.72 at n=6 in simulation; review R3-04).

Uncertainty is reported as **unquantified** (point forecast only, no interval,
no predictive distribution) when

* ``T - 1 < 2`` (fewer than 3 log-differences, i.e. n < 4): a t_1 (Cauchy)
  pivot has no finite variance and the interval is uninformative; or
* the training log-differences have zero sample variance (up to
  :data:`ZERO_SD_TOL`): a constant-growth window gives no information about the
  innovation variance, and a zero-width interval must not pass as quantified.

Strictly positive series only: any value <= 0 makes the model ineligible
(``non_positive_values``); values are never clipped.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from scipy.special import stdtrit

from reva.kernel.uncertainty import UncertaintySpec
from reva.models.forecasting.base import (
    Forecast,
    IneligibleReport,
    Observation,
    PredictiveDistribution,
    check_horizons,
    check_level,
    prediction_interval_spec,
    prepare,
)

KEY = "forecast.log_drift"
VERSION = "1.0.0"
METHOD_VERSION = f"{KEY}@{VERSION}"
MIN_INTERVAL_DF = 2
"""Minimum Student-t degrees of freedom (``T - 1``) for a quantified interval."""
ZERO_SD_TOL = 1e-12
"""Sample sd of log-differences at or below this is treated as zero (float noise)."""
ASSUMPTIONS = (
    "random walk with constant drift on natural-log values",
    "Gaussian i.i.d. log-differences; Student-t predictive with T-1 df (exact under the model)",
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

    @property
    def df(self) -> int:
        """Student-t degrees of freedom of the predictive pivot (``T - 1``)."""
        return self.n_diffs - 1

    @property
    def unquantified_reason(self) -> str | None:
        if self.df < MIN_INTERVAL_DF:
            return (
                f"too few log-differences for a calibrated interval: T={self.n_diffs}, df={self.df} < {MIN_INTERVAL_DF}"
            )
        if self.resid_sd <= ZERO_SD_TOL:
            return "zero sample variance of training log-differences; innovation variance not identified"
        return None

    def predict(self, horizons: Sequence[int], level: float = 0.8) -> list[Forecast]:
        lvl = check_level(level)
        hs = check_horizons(horizons)
        reason = self.unquantified_reason
        q = None if reason else float(stdtrit(self.df, 0.5 + lvl / 2.0))
        out: list[Forecast] = []
        for h in hs:
            centre = self.log_last + h * self.drift_mean
            point = math.exp(centre)
            if q is None:
                out.append(
                    Forecast(
                        model_key=self.model_key,
                        model_version=self.model_version,
                        origin_year=self.origin_year,
                        target_year=self.origin_year + h,
                        horizon=h,
                        point=point,
                        interval=None,
                        interval_kind=None,
                        level=None,
                        uncertainty=UncertaintySpec.unknown(f"{METHOD_VERSION}: uncertainty not quantified ({reason})"),
                        assumptions=ASSUMPTIONS,
                    )
                )
                continue
            scale = self.resid_sd * math.sqrt(h * (1.0 + h / self.n_diffs))
            half = q * scale
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
                            f"Student-t (df={self.df}) log-scale prediction interval, s*sqrt(h*(1+h/T)), exponentiated"
                        ),
                        conditioning=ASSUMPTIONS,
                    ),
                    assumptions=ASSUMPTIONS,
                    predictive=PredictiveDistribution(family="student_t", loc=centre, scale=scale, df=float(self.df)),
                )
            )
        return out


class LogDrift:
    key = KEY
    version = VERSION
    min_train = 3  # point forecasts from 3 points; intervals need >= 4 (df >= 2)

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
