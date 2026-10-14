"""forecast.local_level_log 1.0.0 — Gaussian local-level model on natural logs.

Model (statsmodels ``UnobservedComponents(endog=log(y), level="local level")``)::

    z_t = mu_t + eps_t,          eps_t ~ N(0, sigma_eps^2)
    mu_{t+1} = mu_t + eta_t,     eta_t ~ N(0, sigma_eta^2)

fitted by maximum likelihood with deterministic settings (L-BFGS, fixed
``maxiter``, default deterministic start parameters, ``disp=False``) on the
training window only. Forecasts come from the Kalman-filter predictive
distribution of the *next observation* (``get_forecast(h)``), so the interval is
a state-space prediction interval for the future released statistic (not a
confidence interval for the latent level) on the log scale; bounds and the
predictive mean are exponentiated (the point is the log-normal median).

Estimand: the future released statistic. The measurement model for a latent
"true" value is not identified; parameter uncertainty is not propagated.

A failed or non-converged optimisation returns ``IneligibleReport('fit_failed: ...')``.
"""

from __future__ import annotations

import math
import warnings
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from reva.models.forecasting.base import (
    Forecast,
    IneligibleReport,
    Observation,
    check_horizons,
    check_level,
    prediction_interval_spec,
    prepare,
)

KEY = "forecast.local_level_log"
VERSION = "1.0.0"
METHOD_VERSION = f"{KEY}@{VERSION}"
MAXITER = 200
FIT_METHOD = "lbfgs"
ASSUMPTIONS = (
    "Gaussian local level (random walk plus noise) on natural-log values",
    "MLE plug-in parameters; parameter uncertainty not propagated",
    "estimand is the future released statistic; latent-truth measurement model not identified",
    "point is exp of the log-scale predictive mean (predictive median)",
    "parameters estimated only within the training window",
)


@dataclass(frozen=True)
class FittedLocalLevel:
    model_key: str
    model_version: str
    origin_year: int
    results: Any  # statsmodels UnobservedComponentsResultsWrapper (fitted on train only)

    def predict(self, horizons: Sequence[int], level: float = 0.8) -> list[Forecast]:
        lvl = check_level(level)
        hs = check_horizons(horizons)
        if not hs:
            return []
        fc = self.results.get_forecast(steps=max(hs))
        mean = np.asarray(fc.predicted_mean, dtype=float)
        ci = np.asarray(fc.conf_int(alpha=1.0 - lvl), dtype=float)
        out: list[Forecast] = []
        for h in hs:
            centre, lo_log, hi_log = mean[h - 1], ci[h - 1, 0], ci[h - 1, 1]
            point, lo, hi = math.exp(centre), math.exp(lo_log), math.exp(hi_log)
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
                            "state-space predictive interval for the next released value "
                            "(log scale, Gaussian), exponentiated"
                        ),
                        conditioning=ASSUMPTIONS,
                    ),
                    assumptions=ASSUMPTIONS,
                )
            )
        return out


class LocalLevelLog:
    key = KEY
    version = VERSION
    min_train = 6

    def fit(self, train: Sequence[Observation]) -> FittedLocalLevel | IneligibleReport:
        s = prepare(train)
        if s.problems:
            return IneligibleReport(self.key, self.version, s.problems[0], s.n)
        if any(v <= 0.0 for v in s.values):
            return IneligibleReport(self.key, self.version, "non_positive_values", s.n)
        if s.n < self.min_train:
            return IneligibleReport(self.key, self.version, "insufficient_train", s.n)
        if not s.consecutive:
            return IneligibleReport(self.key, self.version, "non_consecutive_years", s.n)
        # Local import keeps statsmodels off the import path of lighter models.
        from statsmodels.tsa.statespace.structural import UnobservedComponents

        z = np.log(np.asarray(s.values, dtype=float))
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                model = UnobservedComponents(endog=z, level="local level")
                res = model.fit(disp=False, maxiter=MAXITER, method=FIT_METHOD)
        except Exception as exc:  # statsmodels/numpy linear-algebra failures
            return IneligibleReport(
                self.key, self.version, f"fit_failed: {type(exc).__name__}: {exc}", s.n
            )
        retvals = getattr(res, "mle_retvals", None) or {}
        if not retvals.get("converged", False):
            return IneligibleReport(
                self.key, self.version, "fit_failed: optimizer did not converge", s.n
            )
        if not np.all(np.isfinite(np.asarray(res.params, dtype=float))):
            return IneligibleReport(
                self.key, self.version, "fit_failed: non-finite parameters", s.n
            )
        return FittedLocalLevel(self.key, self.version, s.years[-1], res)
