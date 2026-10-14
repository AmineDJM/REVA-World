"""Unit tests for the annual forecasters (last_value, log_drift, local_level_log)."""

from __future__ import annotations

import math
from decimal import Decimal

import numpy as np
import pytest

from reva.kernel.uncertainty import UncertaintyKind, UncertaintyStatus
from reva.models.forecasting import (
    ForecastModel,
    IneligibleReport,
    LastValue,
    LocalLevelLog,
    LogDrift,
)


def geometric(n: int, start: int = 2000, base: float = 100.0, g: float = 1.05):
    return [(start + t, base * g**t) for t in range(n)]


def noisy_positive(n: int, seed: int = 7):
    rng = np.random.default_rng(seed)
    z = np.log(100.0) + np.cumsum(0.02 + 0.03 * rng.standard_normal(n))
    z = z + 0.02 * rng.standard_normal(n)
    return [(1990 + t, float(math.exp(v))) for t, v in enumerate(z)]


def test_models_satisfy_protocol():
    for m in (LastValue(), LogDrift(), LocalLevelLog()):
        assert isinstance(m, ForecastModel)
    assert (LastValue.min_train, LogDrift.min_train, LocalLevelLog.min_train) == (1, 3, 6)


def test_last_value_point_is_last_train_value_and_unquantified():
    train = [(2001, 5.0), (2002, -3.0), (2003, 0.0), (2004, 12.5)]
    fitted = LastValue().fit(train)
    assert not isinstance(fitted, IneligibleReport)
    fcs = fitted.predict([1, 2, 5])
    assert [f.point for f in fcs] == [12.5, 12.5, 12.5]
    assert [f.target_year for f in fcs] == [2005, 2006, 2009]
    for f in fcs:
        assert f.interval is None and f.interval_kind is None and f.level is None
        assert f.uncertainty.status == UncertaintyStatus.unquantified
        assert f.uncertainty.kind == UncertaintyKind.unknown
        assert f.model_key == "forecast.last_value" and f.model_version == "1.0.0"


def test_last_value_single_point_and_decimal_input():
    fitted = LastValue().fit([(2020, Decimal("1.25"))])
    assert not isinstance(fitted, IneligibleReport)
    (f,) = fitted.predict([1])
    assert f.point == 1.25 and isinstance(f.point, float)
    assert isinstance(LastValue().fit([]), IneligibleReport)


def test_log_drift_recovers_exact_geometric_growth():
    train = geometric(12)
    fitted = LogDrift().fit(train)
    assert not isinstance(fitted, IneligibleReport)
    assert fitted.drift_mean == pytest.approx(math.log(1.05), rel=1e-12)
    for f in fitted.predict([1, 2, 3], level=0.8):
        expected = 100.0 * 1.05 ** (11 + f.horizon)
        assert abs(f.point - expected) / expected < 1e-9
        lo, hi = f.interval
        assert lo <= f.point <= hi
        assert f.interval_kind == "prediction_interval" and f.level == 0.8
        assert f.uncertainty.status == UncertaintyStatus.quantified
        assert f.uncertainty.kind == UncertaintyKind.prediction_interval
        assert f.uncertainty.method_version == "forecast.log_drift@1.0.0"


def test_log_drift_interval_formula_and_widening():
    train = noisy_positive(15)
    fitted = LogDrift().fit(train)
    assert not isinstance(fitted, IneligibleReport)
    z = np.log([v for _, v in train])
    d = np.diff(z)
    sd, T = d.std(ddof=1), d.size
    q = 1.2815515655446004  # Phi^-1(0.9) for an 80% central interval
    f1, f3 = fitted.predict([1, 3], level=0.8)
    for f, h in ((f1, 1), (f3, 3)):
        half = q * sd * math.sqrt(h * (1 + h / T))
        centre = z[-1] + h * d.mean()
        assert f.interval[0] == pytest.approx(math.exp(centre - half), rel=1e-9)
        assert f.interval[1] == pytest.approx(math.exp(centre + half), rel=1e-9)
    assert (f3.interval[1] - f3.interval[0]) > (f1.interval[1] - f1.interval[0])


@pytest.mark.parametrize("bad", [0.0, -1.0, Decimal("0")])
@pytest.mark.parametrize("model", [LogDrift(), LocalLevelLog()])
def test_log_models_refuse_non_positive_values_without_clipping(model, bad):
    train = [(2000 + t, v) for t, v in enumerate([10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0])]
    train[3] = (train[3][0], bad)
    out = model.fit(train)
    assert isinstance(out, IneligibleReport)
    assert out.reason == "non_positive_values"
    assert out.n_train == 7


def test_log_drift_needs_three_points():
    out = LogDrift().fit(geometric(2))
    assert isinstance(out, IneligibleReport) and out.reason == "insufficient_train"


def test_local_level_ineligible_below_six_points():
    out = LocalLevelLog().fit(noisy_positive(5))
    assert isinstance(out, IneligibleReport)
    assert out.reason == "insufficient_train" and out.n_train == 5


def test_local_level_finite_forecasts_with_prediction_interval():
    fitted = LocalLevelLog().fit(noisy_positive(20))
    assert not isinstance(fitted, IneligibleReport)
    fcs = fitted.predict([1, 2, 3], level=0.8)
    assert len(fcs) == 3
    for f in fcs:
        lo, hi = f.interval
        assert all(math.isfinite(v) for v in (f.point, lo, hi))
        assert lo < f.point < hi
        assert f.interval_kind == "prediction_interval"
        assert f.uncertainty.kind == UncertaintyKind.prediction_interval
        assert f.uncertainty.level == 0.8
    widths = [f.interval[1] - f.interval[0] for f in fcs]
    assert widths == sorted(widths)


def test_local_level_is_deterministic():
    train = noisy_positive(14, seed=3)
    a = LocalLevelLog().fit(train).predict([1, 2])
    b = LocalLevelLog().fit(train).predict([1, 2])
    assert [(f.point, f.interval) for f in a] == [(f.point, f.interval) for f in b]
