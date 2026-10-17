"""Forecast combinations and calibrated (conformal) predictive densities (V6, agent PRED-MODELS). Pure numerics.

* :func:`equal_weight` — point = mean of the component points (Bates & Granger 1969; the "forecast combination
  puzzle": equal weights are hard to beat, Smith & Wallis 2009); density = equally weighted linear pool of the
  component predictives (mixture, represented by quantile samples).
* :func:`conformal` — split-conformal style empirical predictive around a point: the point plus the
  *out-of-sample* errors of the same forecaster at the same horizon whose outcomes were already published at the
  origin (Vovk, Gammerman & Shafer 2005; Lei et al. 2018, JASA 113:1094-1111). Optional per-series scale
  (normalised errors, "locally weighted" conformal). Exchangeability of past and future errors is the
  assumption; it fails under regime changes, which the coverage tables of the evaluation report show.
* :func:`bias_corrected` — point plus the mean of those past errors (an intercept-correction of a reference
  forecast such as the WEO, whose next-year projections have a documented optimism bias).

Past errors must come from the evaluation layer with their outcome availability instant ≤ the origin.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np

from reva.models.forecasting.registry_baselines import Abstain, Predictive

KEY_EQUAL = "forecast.combo_equal"
KEY_CONFORMAL = "forecast.conformal"
KEY_BIAS = "forecast.bias_corrected"
VERSION = "1.0.0"
POOL_QUANTILES = 100
MIN_ERRORS = 15


def quantile_sample(p: Predictive, n: int = POOL_QUANTILES) -> np.ndarray:
    """``n`` equally spaced quantiles (mid-points) of a distributional predictive."""
    if not p.distributional:
        raise ValueError("a point forecast has no quantiles")
    probs = (np.arange(n) + 0.5) / n
    if p.family == "empirical":
        return np.quantile(np.asarray(p.sample), probs)
    return np.asarray([p.quantile(float(u)) for u in probs], dtype=float)


def equal_weight(preds: Sequence[Predictive], *, density: bool = True) -> Predictive:
    if not preds:
        raise ValueError("nothing to combine")
    point = math.fsum(p.point for p in preds) / len(preds)
    if density and all(p.distributional for p in preds):
        smp = np.sort(np.concatenate([quantile_sample(p) for p in preds]))
        return Predictive(
            "empirical", point, sample=tuple(float(v) for v in smp), detail=(("components", str(len(preds))),)
        )
    return Predictive("point", point, detail=(("components", str(len(preds))),))


def conformal(
    point: float,
    past_errors: Sequence[float],
    *,
    scale: float = 1.0,
    min_errors: int = MIN_ERRORS,
    center: bool = False,
) -> Predictive | Abstain:
    """Empirical predictive ``point + scale * e`` over past out-of-sample errors ``e = outcome - forecast``
    (already divided by their own series scale when ``scale`` is not 1). With ``center`` the errors are
    de-meaned (a pure spread model around an unbiased point)."""
    e = np.asarray([x for x in past_errors if math.isfinite(x)], dtype=float)
    if len(e) < min_errors:
        return Abstain(f"fewer than {min_errors} past out-of-sample errors known at the origin")
    if not (math.isfinite(scale) and scale > 0):
        return Abstain("non-positive series scale")
    if center:
        e = e - e.mean()
    smp = np.sort(point + scale * e)
    if smp[0] == smp[-1]:
        return Abstain("degenerate past errors")
    return Predictive(
        "empirical",
        float(point),
        sample=tuple(float(v) for v in smp),
        detail=(("n_errors", str(len(e))), ("scale", f"{scale:.6g}")),
    )


def bias_corrected(
    point: float, past_errors: Sequence[float], *, min_errors: int = MIN_ERRORS, robust: bool = False
) -> float | Abstain:
    e = [x for x in past_errors if math.isfinite(x)]
    if len(e) < min_errors:
        return Abstain(f"fewer than {min_errors} past out-of-sample errors known at the origin")
    adj = float(np.median(e)) if robust else math.fsum(e) / len(e)
    return float(point + adj)


__all__ = [
    "KEY_BIAS",
    "KEY_CONFORMAL",
    "KEY_EQUAL",
    "VERSION",
    "bias_corrected",
    "conformal",
    "equal_weight",
    "quantile_sample",
]
