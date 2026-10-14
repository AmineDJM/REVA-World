"""Forecast accuracy metrics (pure numerics).

* Point accuracy: absolute error, MAE, MASE.
* MASE scale (non-seasonal, m = 1) = mean |y_t - y_{t-1}| computed **within the
  training window only**. It is undefined (``None`` with flag
  ``"undefined_scale"``) when the training window has fewer than 2 values or the
  scale is exactly zero (constant training series). It is never replaced by an
  epsilon.
* Interval metrics: coverage indicator and width (sharpness). Coverage is a
  property of probabilistic *prediction intervals* only; aggregating coverage
  over anything else (sensitivity ranges, bounds, credible/confidence intervals,
  unknown kinds) raises ``ValueError`` so interval kinds are never mixed.
* Gaussian proper scores: closed-form CRPS and log score.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from scipy.stats import norm

from reva.kernel.uncertainty import UncertaintyKind

UNDEFINED_SCALE = "undefined_scale"
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

    @property
    def defined(self) -> bool:
        return self.value is not None


def mase_scale(train_values: Sequence[float]) -> MaseScale:
    """In-sample mean absolute one-step naive difference over the training window."""
    vals = [_finite(v, "train value") for v in train_values]
    n = len(vals)
    if n < 2:
        return MaseScale(None, UNDEFINED_SCALE, n)
    scale = math.fsum(abs(b - a) for a, b in zip(vals, vals[1:], strict=False)) / (n - 1)
    if scale == 0.0:
        return MaseScale(None, UNDEFINED_SCALE, n)
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


def mase(points: Sequence[float], actuals: Sequence[float], train_values: Sequence[float]) -> (
    MaseResult
):
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


def mean_interval_width(items: Iterable[tuple[str | UncertaintyKind, tuple[float, float]]]) -> (
    float
):
    """Mean width (sharpness) of prediction intervals; rejects other kinds."""
    widths: list[float] = []
    for kind, interval in items:
        _require_prediction_interval(kind)
        widths.append(interval_width(interval))
    if not widths:
        raise ValueError("mean width of an empty set is undefined")
    return math.fsum(widths) / len(widths)


def crps_gaussian(actual: float, mu: float, sigma: float) -> float:
    """Closed-form CRPS of N(mu, sigma^2) at ``actual`` (lower is better).

    CRPS = sigma * [ z (2 Phi(z) - 1) + 2 phi(z) - 1/sqrt(pi) ],  z = (y - mu)/sigma.
    """
    s = _finite(sigma, "sigma")
    if s <= 0:
        raise ValueError("sigma must be > 0")
    z = (_finite(actual, "actual") - _finite(mu, "mu")) / s
    return s * (z * (2.0 * float(norm.cdf(z)) - 1.0) + 2.0 * float(norm.pdf(z))
                - 1.0 / math.sqrt(math.pi))


def log_score_gaussian(actual: float, mu: float, sigma: float) -> float:
    """Log predictive density log N(actual; mu, sigma^2) (higher is better).

    The negatively oriented loss is ``-log_score_gaussian(...)``.
    """
    s = _finite(sigma, "sigma")
    if s <= 0:
        raise ValueError("sigma must be > 0")
    z = (_finite(actual, "actual") - _finite(mu, "mu")) / s
    return -0.5 * math.log(2.0 * math.pi) - math.log(s) - 0.5 * z * z
