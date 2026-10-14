"""Scoring of forecasts against realised (released) values.

Built on :mod:`reva.models.metrics`. The MASE scale always comes from the
training window the forecast was fitted on, never from the evaluation period.
Coverage is only computed for prediction intervals; specs of other kinds are
rejected by :func:`coverage_of_specs` (``ValueError``).
"""

from __future__ import annotations

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
    interval_covers,
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
    "interval_covers",
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


def score_forecast(
    forecast: Forecast, actual: float | Decimal, train_values: Sequence[float | Decimal]
) -> ScoredForecast:
    a = to_float(actual)
    scale: MaseScale = mase_scale([to_float(v) for v in train_values])
    covered = width = None
    if forecast.interval is not None:
        # Forecast guarantees interval_kind == "prediction_interval" here.
        covered = interval_covers(forecast.interval, a)
        width = interval_width(forecast.interval)
    return ScoredForecast(
        forecast=forecast,
        actual=a,
        abs_error=absolute_error(forecast.point, a),
        scaled_error=scaled_absolute_error(forecast.point, a, scale),
        scale_flag=scale.flag,
        covered=covered,
        width=width,
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
