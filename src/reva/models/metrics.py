"""Forecast accuracy metrics (pure numerics).

* Point accuracy: absolute error, MAE, MASE.
* MASE scale (non-seasonal, m = 1) = mean |y_t - y_{t-1}| computed **within the
  training window only** (Hyndman & Koehler 2006). It is undefined (``None``
  with flag ``"undefined_scale"`` and a ``reason``) when

  - the training window has fewer than :data:`MIN_SCALE_POINTS` (3) values
    (``too_few_points``): a 2-point scale is a single |difference| that can be
    arbitrarily small, giving heavy-tailed scaled errors (review R3-17);
  - the training years are not consecutive (``gapped_years``): a gap would mix
    1-year and multi-year differences in one scale. Callers that know the years
    must pass them (``years=``); without years the values are taken to be
    consecutive annual observations;
  - the scale is exactly zero (``zero_scale``, constant training series).

  It is never replaced by an epsilon.
* Interval metrics: coverage indicator and width (sharpness). Coverage is a
  property of probabilistic *prediction intervals* only; aggregating coverage
  over anything else (sensitivity ranges, bounds, credible/confidence intervals,
  unknown kinds) raises ``ValueError`` so interval kinds are never mixed.
* Proper scores (Gneiting & Raftery 2007): the interval score of a central
  ``level`` prediction interval (their eq. 43), closed-form Gaussian and
  Student-t CRPS, and the Gaussian log score. A model without a probabilistic
  forecast has no proper score ("not_applicable"), never a score of 0.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from scipy.special import stdtr

from reva.kernel.uncertainty import UncertaintyKind

UNDEFINED_SCALE = "undefined_scale"
MIN_SCALE_POINTS = 3
COVERAGE_KIND = UncertaintyKind.prediction_interval


def _finite(x: float, name: str) -> float:
    v = float(x)
    if not math.isfinite(v):
        raise ValueError(f"{name} must be finite")
    return v


def absolute_error(point: float, actual: float) -> float:
    return abs(_finite(point, "point") - _finite(actual, "actual"))


def mae(errors: Sequence[float]) -> float:
    """Mean absolute error of a sequence of absolute errors (non-empty)."""
    if len(errors) == 0:
        raise ValueError("MAE of an empty sequence is undefined")
    return math.fsum(abs(_finite(e, "error")) for e in errors) / len(errors)


@dataclass(frozen=True)
class MaseScale:
    value: float | None
    flag: str | None  # None when defined, "undefined_scale" otherwise
    n_train: int
    reason: str | None = None  # too_few_points | gapped_years | zero_scale (when undefined)

    @property
    def defined(self) -> bool:
        return self.value is not None


def mase_scale(train_values: Sequence[float], years: Sequence[int] | None = None) -> MaseScale:
    """In-sample mean absolute one-step naive difference over the training window.

    Requires at least :data:`MIN_SCALE_POINTS` values and, when ``years`` is
    given, strictly consecutive calendar years (no gaps). Otherwise the scale
    is undefined (``value=None``, ``flag="undefined_scale"``, ``reason`` set).
    """
    vals = [_finite(v, "train value") for v in train_values]
    n = len(vals)
    if years is not None:
        ys = [int(y) for y in years]
        if len(ys) != n:
            raise ValueError("years and train_values must have the same length")
        if any(b - a != 1 for a, b in itertools.pairwise(ys)):
            return MaseScale(None, UNDEFINED_SCALE, n, "gapped_years")
    if n < MIN_SCALE_POINTS:
        return MaseScale(None, UNDEFINED_SCALE, n, "too_few_points")
    scale = math.fsum(abs(b - a) for a, b in itertools.pairwise(vals)) / (n - 1)
    if scale == 0.0:
        return MaseScale(None, UNDEFINED_SCALE, n, "zero_scale")
    return MaseScale(scale, None, n)


def scaled_absolute_error(point: float, actual: float, scale: MaseScale) -> float | None:
    """|point - actual| / scale, or None when the scale is undefined."""
    if scale.value is None:
        return None
    return absolute_error(point, actual) / scale.value


@dataclass(frozen=True)
class MaseResult:
    value: float | None
    flag: str | None


def mase(points: Sequence[float], actuals: Sequence[float], train_values: Sequence[float]) -> MaseResult:
    """MASE of forecasts made from one training window (shared scale)."""
    if len(points) != len(actuals) or len(points) == 0:
        raise ValueError("points and actuals must be equal-length and non-empty")
    scale = mase_scale(train_values)
    if scale.value is None:
        return MaseResult(None, UNDEFINED_SCALE)
    errs = [absolute_error(p, a) for p, a in zip(points, actuals, strict=True)]
    return MaseResult(mae(errs) / scale.value, None)


def interval_covers(interval: tuple[float, float], actual: float) -> bool:
    """Closed-interval coverage indicator."""
    lo, hi = _finite(interval[0], "lower"), _finite(interval[1], "upper")
    if lo > hi:
        raise ValueError("interval lower > upper")
    a = _finite(actual, "actual")
    return lo <= a <= hi


def interval_width(interval: tuple[float, float]) -> float:
    lo, hi = _finite(interval[0], "lower"), _finite(interval[1], "upper")
    if lo > hi:
        raise ValueError("interval lower > upper")
    return hi - lo


def _require_prediction_interval(kind: object) -> None:
    try:
        k = UncertaintyKind(str(kind.value if isinstance(kind, UncertaintyKind) else kind))
    except ValueError as exc:
        raise ValueError(f"unknown interval kind {kind!r}") from exc
    if k != COVERAGE_KIND:
        raise ValueError(
            f"coverage is defined only for prediction intervals; got {k.value!r} "
            "(sensitivity ranges and bounds have no coverage)"
        )


def aggregate_coverage(items: Iterable[tuple[str | UncertaintyKind, bool]]) -> float:
    """Empirical coverage rate of ``(interval_kind, covered)`` pairs.

    Every item must be a ``prediction_interval``; any other kind raises
    ``ValueError`` (mixing kinds is a semantic error, not a filter).
    """
    flags: list[bool] = []
    for kind, covered in items:
        _require_prediction_interval(kind)
        flags.append(bool(covered))
    if not flags:
        raise ValueError("coverage of an empty set is undefined")
    return sum(flags) / len(flags)


def mean_interval_width(
    items: Iterable[tuple[str | UncertaintyKind, tuple[float, float]]],
) -> float:
    """Mean width (sharpness) of prediction intervals; rejects other kinds."""
    widths: list[float] = []
    for kind, interval in items:
        _require_prediction_interval(kind)
        widths.append(interval_width(interval))
    if not widths:
        raise ValueError("mean width of an empty set is undefined")
    return math.fsum(widths) / len(widths)


def _beta(a: float, b: float) -> float:
    return math.exp(math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b))


def _norm_cdf(z: float) -> float:
    return 0.5 * math.erfc(-z / math.sqrt(2.0))


def _norm_pdf(z: float) -> float:
    return math.exp(-0.5 * z * z) / math.sqrt(2.0 * math.pi)


def interval_score(interval: tuple[float, float], actual: float, level: float) -> float:
    """Interval score of a central ``level`` prediction interval (lower is better).

    Gneiting & Raftery (2007, eq. 43) with ``alpha = 1 - level``::

        IS = (u - l) + (2/alpha) (l - y) 1{y < l} + (2/alpha) (y - u) 1{y > u}

    It is proper for the (alpha/2, 1 - alpha/2) quantile pair, so (unlike
    coverage and width separately) it cannot be gamed by trading one for the
    other. Divided by the in-sample MASE scale it is the M4 "MSIS".
    """
    lo, hi = _finite(interval[0], "lower"), _finite(interval[1], "upper")
    if lo > hi:
        raise ValueError("interval lower > upper")
    lv = float(level)
    if not (0.0 < lv < 1.0):
        raise ValueError("level must be in (0, 1)")
    a = _finite(actual, "actual")
    alpha = 1.0 - lv
    return (hi - lo) + (2.0 / alpha) * max(lo - a, 0.0) + (2.0 / alpha) * max(a - hi, 0.0)


def crps_student_t(actual: float, loc: float, scale: float, df: float) -> float:
    """Closed-form CRPS of a location-scale Student-t (``df > 1``; lower is better).

    With ``z = (y - loc)/scale``, ``F``/``f`` the standard t cdf/pdf::

        CRPS = scale * [ z (2F(z) - 1) + 2 f(z) (df + z^2)/(df - 1)
                         - 2 sqrt(df) B(1/2, df - 1/2) / ((df - 1) B(1/2, df/2)^2) ]

    (as implemented in the R package ``scoringRules``; Jordan, Krüger & Lerch
    2019). The CRPS is infinite for ``df <= 1`` (no finite mean), so that case
    is refused.
    """
    s = _finite(scale, "scale")
    v = _finite(df, "df")
    if s <= 0:
        raise ValueError("scale must be > 0")
    if v <= 1.0:
        raise ValueError("CRPS of a Student-t requires df > 1")
    z = (_finite(actual, "actual") - _finite(loc, "loc")) / s
    cdf = float(stdtr(v, z))
    log_norm = math.lgamma((v + 1.0) / 2.0) - math.lgamma(v / 2.0) - 0.5 * math.log(v * math.pi)
    pdf = math.exp(log_norm - (v + 1.0) / 2.0 * math.log1p(z * z / v))
    const = 2.0 * math.sqrt(v) * _beta(0.5, v - 0.5) / ((v - 1.0) * _beta(0.5, v / 2.0) ** 2)
    return s * (z * (2.0 * cdf - 1.0) + 2.0 * pdf * (v + z * z) / (v - 1.0) - const)


def crps_gaussian(actual: float, mu: float, sigma: float) -> float:
    """Closed-form CRPS of N(mu, sigma^2) at ``actual`` (lower is better).

    CRPS = sigma * [ z (2 Phi(z) - 1) + 2 phi(z) - 1/sqrt(pi) ],  z = (y - mu)/sigma.
    """
    s = _finite(sigma, "sigma")
    if s <= 0:
        raise ValueError("sigma must be > 0")
    z = (_finite(actual, "actual") - _finite(mu, "mu")) / s
    return s * (z * (2.0 * _norm_cdf(z) - 1.0) + 2.0 * _norm_pdf(z) - 1.0 / math.sqrt(math.pi))


def log_score_gaussian(actual: float, mu: float, sigma: float) -> float:
    """Log predictive density log N(actual; mu, sigma^2) (higher is better).

    The negatively oriented loss is ``-log_score_gaussian(...)``.
    """
    s = _finite(sigma, "sigma")
    if s <= 0:
        raise ValueError("sigma must be > 0")
    z = (_finite(actual, "actual") - _finite(mu, "mu")) / s
    return -0.5 * math.log(2.0 * math.pi) - math.log(s) - 0.5 * z * z
