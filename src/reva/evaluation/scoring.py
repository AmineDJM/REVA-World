"""Scoring of forecasts against realised (released) values.

Built on :mod:`reva.models.metrics`. The MASE scale always comes from the
training window the forecast was fitted on, never from the evaluation period,
and needs >= 3 consecutive-year training points (pass ``train_years`` so gaps
are detected). Coverage is only computed for prediction intervals; specs of
other kinds are rejected by :func:`coverage_of_specs` (``ValueError``).

Proper scores: the interval score (and its MASE-scaled form) for forecasts
with a prediction interval, and the log-scale CRPS for forecasts carrying a
predictive distribution. A forecast without them gets ``None`` here and is
reported as ``"not_applicable"`` in aggregates, never as 0.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal

from reva.kernel.uncertainty import UncertaintyKind, UncertaintySpec
from reva.models.forecasting.base import Forecast, to_float
from reva.models.metrics import (
    UNDEFINED_SCALE,
    MaseScale,
    absolute_error,
    aggregate_coverage,
    crps_gaussian,
    crps_student_t,
    interval_covers,
    interval_score,
    interval_width,
    log_score_gaussian,
    mae,
    mase,
    mase_scale,
    mean_interval_width,
    scaled_absolute_error,
)

__all__ = [
    "UNDEFINED_SCALE",
    "ScoredForecast",
    "absolute_error",
    "aggregate_coverage",
    "coverage_of_specs",
    "crps_gaussian",
    "crps_student_t",
    "interval_covers",
    "interval_score",
    "interval_width",
    "log_score_gaussian",
    "mae",
    "mase",
    "mase_scale",
    "mean_interval_width",
    "scaled_absolute_error",
    "score_forecast",
]


@dataclass(frozen=True)
class ScoredForecast:
    forecast: Forecast
    actual: float
    abs_error: float
    scaled_error: float | None
    scale_flag: str | None
    covered: bool | None
    width: float | None
    interval_score: float | None = None
    interval_score_scaled: float | None = None
    crps_log: float | None = None
    scale_reason: str | None = None


def score_forecast(
    forecast: Forecast,
    actual: float | Decimal,
    train_values: Sequence[float | Decimal],
    train_years: Sequence[int] | None = None,
) -> ScoredForecast:
    a = to_float(actual)
    scale: MaseScale = mase_scale([to_float(v) for v in train_values], years=train_years)
    covered = width = isc = isc_scaled = crps = None
    if forecast.interval is not None:
        # Forecast guarantees interval_kind == "prediction_interval" here.
        covered = interval_covers(forecast.interval, a)
        width = interval_width(forecast.interval)
        if forecast.level is not None:
            isc = interval_score(forecast.interval, a, forecast.level)
            isc_scaled = None if scale.value is None else isc / scale.value
    pred = forecast.predictive
    if pred is not None and a > 0.0:
        z = math.log(a)
        if pred.family == "normal":
            crps = crps_gaussian(z, pred.loc, pred.scale)
        elif pred.df is not None and pred.df > 1.0:
            crps = crps_student_t(z, pred.loc, pred.scale, pred.df)
    return ScoredForecast(
        forecast=forecast,
        actual=a,
        abs_error=absolute_error(forecast.point, a),
        scaled_error=scaled_absolute_error(forecast.point, a, scale),
        scale_flag=scale.flag,
        covered=covered,
        width=width,
        interval_score=isc,
        interval_score_scaled=isc_scaled,
        crps_log=crps,
        scale_reason=scale.reason,
    )


def coverage_of_specs(items: Iterable[tuple[UncertaintySpec, float | Decimal]]) -> float:
    """Empirical coverage of ``(spec, actual)`` pairs using the spec's lower/upper.

    Raises ``ValueError`` if any spec is not a prediction interval (e.g. a
    sensitivity range or bounds) or lacks numeric bounds.
    """
    pairs: list[tuple[UncertaintyKind, bool]] = []
    for spec, actual in items:
        if spec.kind != UncertaintyKind.prediction_interval:
            raise ValueError(
                f"coverage is defined only for prediction intervals; got {spec.kind.value!r}"
            )
        if spec.lower is None or spec.upper is None:
            raise ValueError("prediction interval spec lacks lower/upper")
        interval = (float(Decimal(spec.lower)), float(Decimal(spec.upper)))
        pairs.append((spec.kind, interval_covers(interval, to_float(actual))))
    return aggregate_coverage(pairs)
