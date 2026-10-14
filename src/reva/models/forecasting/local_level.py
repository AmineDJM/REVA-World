"""forecast.local_level_log 1.0.0 — Gaussian local-level model on natural logs.

Model (statsmodels ``UnobservedComponents(endog=log(y), level="local level")``)::

    z_t = mu_t + eps_t,          eps_t ~ N(0, sigma_eps^2)
    mu_{t+1} = mu_t + eta_t,     eta_t ~ N(0, sigma_eta^2)

fitted by maximum likelihood with deterministic settings (fixed optimiser chain
L-BFGS then Powell, fixed ``maxiter``, default deterministic start parameters,
``disp=False``) on the
training window only. Forecasts come from the Kalman-filter predictive
distribution of the *next observation* (``get_forecast(h)``), so the interval is
a state-space prediction interval for the future released statistic (not a
confidence interval for the latent level) on the log scale; bounds and the
predictive mean are exponentiated (the point is the log-normal median).

Estimand: the future released statistic. The measurement model for a latent
"true" value is not identified; parameter uncertainty is not propagated.

Calibration (review R3-05). The interval is a **plug-in MLE** interval: the
estimated variances are treated as known. With 6-10 annual points the MLE
variances often sit on the zero boundary, so the interval is too narrow; in a
simulation from the model itself a nominal 0.80 interval covered about 0.70 at
n=6 and 0.66 at n=10. No small-sample correction is applied (none is
established for this model), so the nominal level is **not calibrated**: the
uncertainty spec says so (``description`` and ``conditioning``), and the
tournament reports empirical coverage next to the nominal level
(``ModelMetrics.coverage`` / ``nominal_level`` / ``coverage_gap``, overall and
per horizon) together with the interval score, so the under-coverage is
visible and penalised rather than hidden.

A failed or non-converged optimisation returns ``IneligibleReport('fit_failed: ...')``.
"""

from __future__ import annotations

import math
import warnings
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

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

KEY = "forecast.local_level_log"
VERSION = "1.0.0"
METHOD_VERSION = f"{KEY}@{VERSION}"
MAXITER = 500
# Deterministic optimiser chain: L-BFGS first; if it stops abnormally (common when a
# variance sits on the zero boundary) Powell is tried. Order is fixed and recorded.
FIT_METHODS = ("lbfgs", "powell")
NOMINAL_LEVEL_CALIBRATED = False
ZERO_VAR_TOL = 1e-24  # log-scale predictive variance at or below this is degenerate
PLUGIN_CALIBRATION_NOTE = (
    "plug-in MLE interval; nominal coverage not calibrated "
    "(under-covers in short samples: ~0.66-0.70 at nominal 0.80 for 6-10 points in simulation)"
)
ASSUMPTIONS = (
    "Gaussian local level (random walk plus noise) on natural-log values",
    "MLE plug-in parameters; parameter uncertainty not propagated",
    PLUGIN_CALIBRATION_NOTE,
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
    fit_method: str

    def predict(self, horizons: Sequence[int], level: float = 0.8) -> list[Forecast]:
        lvl = check_level(level)
        hs = check_horizons(horizons)
        if not hs:
            return []
        fc = self.results.get_forecast(steps=max(hs))
        mean = np.asarray(fc.predicted_mean, dtype=float)
        var = np.asarray(fc.var_pred_mean, dtype=float)
        ci = np.asarray(fc.conf_int(alpha=1.0 - lvl), dtype=float)
        out: list[Forecast] = []
        for h in hs:
            centre, v = float(mean[h - 1]), float(var[h - 1])
            point = math.exp(centre)
            if not (math.isfinite(v) and v > ZERO_VAR_TOL):
                # A degenerate (zero-width) predictive must not pass as quantified.
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
                        uncertainty=UncertaintySpec.unknown(
                            f"{METHOD_VERSION}: degenerate predictive variance ({v!r}); "
                            "uncertainty not quantified"
                        ),
                        assumptions=ASSUMPTIONS,
                    )
                )
                continue
            lo, hi = math.exp(float(ci[h - 1, 0])), math.exp(float(ci[h - 1, 1]))
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
                            "(log scale, Gaussian), exponentiated; " + PLUGIN_CALIBRATION_NOTE
                        ),
                        conditioning=ASSUMPTIONS,
                    ),
                    assumptions=ASSUMPTIONS,
                    predictive=PredictiveDistribution(
                        family="normal", loc=centre, scale=math.sqrt(v)
                    ),
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
        failures: list[str] = []
        for method in FIT_METHODS:
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    model = UnobservedComponents(endog=z, level="local level")
                    res = model.fit(disp=False, maxiter=MAXITER, method=method)
            except Exception as exc:  # statsmodels/numpy linear-algebra failures
                failures.append(f"{method}: {type(exc).__name__}: {exc}")
                continue
            retvals = getattr(res, "mle_retvals", None) or {}
            if not retvals.get("converged", False):
                failures.append(f"{method}: optimizer did not converge")
                continue
            if not np.all(np.isfinite(np.asarray(res.params, dtype=float))):
                failures.append(f"{method}: non-finite parameters")
                continue
            return FittedLocalLevel(self.key, self.version, s.years[-1], res, method)
        return IneligibleReport(self.key, self.version, "fit_failed: " + "; ".join(failures), s.n)
