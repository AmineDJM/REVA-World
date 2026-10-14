"""Metrics: MASE scale, coverage-kind discipline, Gaussian scores."""

from __future__ import annotations

import math

import numpy as np
import pytest
from scipy import integrate
from scipy.stats import norm

from reva.evaluation.scoring import coverage_of_specs, score_forecast
from reva.kernel.uncertainty import UncertaintyKind, UncertaintySpec, UncertaintyStatus
from reva.models.forecasting import LastValue, LogDrift
from reva.models.metrics import (
    UNDEFINED_SCALE,
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
)


def test_absolute_error_and_mae():
    assert absolute_error(3.0, 5.5) == 2.5
    assert mae([1.0, 2.0, 3.0]) == 2.0
    with pytest.raises(ValueError):
        mae([])


def test_mase_scale_uses_training_window_only():
    s = mase_scale([10.0, 12.0, 11.0, 15.0])  # |2|,|1|,|4| -> 7/3
    assert s.value == pytest.approx(7 / 3) and s.flag is None
    r = mase([16.0], [18.0], [10.0, 12.0, 11.0, 15.0])
    assert r.value == pytest.approx(2.0 / (7 / 3))


def test_mase_undefined_on_constant_or_short_training_series():
    for train, reason in (([5.0, 5.0, 5.0, 5.0], "zero_scale"), ([5.0], "too_few_points"),
                          ([5.0, 9.0], "too_few_points")):
        s = mase_scale(train)
        assert s.value is None and s.flag == UNDEFINED_SCALE and s.reason == reason
        r = mase([6.0], [7.0], train)
        assert r.value is None and r.flag == UNDEFINED_SCALE


def test_mase_scale_needs_three_consecutive_years():
    """R3-17: a 2-point scale is one |difference|; gaps mix 1- and multi-year differences."""
    assert mase_scale([10.0, 12.0, 11.0], years=[2000, 2001, 2002]).value == pytest.approx(1.5)
    gapped = mase_scale([10.0, 12.0, 11.0, 15.0], years=[2000, 2001, 2003, 2004])
    assert gapped.value is None and gapped.flag == UNDEFINED_SCALE and gapped.reason == "gapped_years"
    two = mase_scale([10.0, 12.0], years=[2000, 2001])
    assert two.value is None and two.reason == "too_few_points"
    with pytest.raises(ValueError):
        mase_scale([1.0, 2.0, 3.0], years=[2000, 2001])


def test_score_forecast_flags_undefined_scale():
    train = [(2000 + t, 5.0) for t in range(4)]
    (f,) = LastValue().fit(train).predict([1])
    sc = score_forecast(f, 6.0, [v for _, v in train])
    assert sc.abs_error == 1.0 and sc.scaled_error is None and sc.scale_flag == UNDEFINED_SCALE
    assert sc.covered is None


def test_interval_coverage_and_width():
    assert interval_covers((1.0, 2.0), 2.0) and not interval_covers((1.0, 2.0), 2.01)
    assert interval_width((1.0, 3.5)) == 2.5
    pairs = [("prediction_interval", True), ("prediction_interval", False)]
    assert aggregate_coverage(pairs) == 0.5
    assert mean_interval_width([(UncertaintyKind.prediction_interval, (0.0, 2.0))]) == 2.0


@pytest.mark.parametrize(
    "kind",
    ["sensitivity_range", "bounds", "credible_interval", "confidence_interval", "unknown"],
)
def test_coverage_aggregation_rejects_non_prediction_intervals(kind):
    with pytest.raises(ValueError):
        aggregate_coverage([("prediction_interval", True), (kind, True)])
    with pytest.raises(ValueError):
        mean_interval_width([(kind, (0.0, 1.0))])


def test_coverage_of_specs_rejects_sensitivity_range():
    sens = UncertaintySpec.sensitivity("1", "2", "m@1", "scenario range")
    with pytest.raises(ValueError, match="prediction intervals"):
        coverage_of_specs([(sens, 1.5)])
    train = [(2000 + t, 100 * 1.04**t + (t % 2)) for t in range(8)]
    (f,) = LogDrift().fit(train).predict([1])
    assert f.uncertainty.status == UncertaintyStatus.quantified
    assert coverage_of_specs([(f.uncertainty, f.point)]) == 1.0


def test_crps_gaussian_matches_numerical_integral():
    mu, sigma, y = 1.0, 2.0, 2.5
    num, _ = integrate.quad(
        lambda x: (norm.cdf(x, mu, sigma) - (x >= y)) ** 2, -40, 40, points=[y], limit=200
    )
    assert crps_gaussian(y, mu, sigma) == pytest.approx(num, rel=1e-6)
    assert crps_gaussian(mu, mu, 1.0) == pytest.approx((math.sqrt(2) - 1) / math.sqrt(math.pi))


def test_interval_score_gneiting_raftery():
    # alpha = 0.2 -> penalty 2/alpha = 10 per unit outside.
    assert interval_score((1.0, 3.0), 2.0, 0.8) == pytest.approx(2.0)
    assert interval_score((1.0, 3.0), 0.5, 0.8) == pytest.approx(2.0 + 10 * 0.5)
    assert interval_score((1.0, 3.0), 4.0, 0.8) == pytest.approx(2.0 + 10 * 1.0)
    with pytest.raises(ValueError):
        interval_score((1.0, 3.0), 2.0, 1.0)


def test_interval_score_is_proper_for_the_central_quantiles():
    """Expected score under N(0,1) is minimised by the true 10%/90% quantiles."""
    y = norm.ppf((np.arange(20000) + 0.5) / 20000)  # deterministic N(0,1) sample
    q = norm.ppf(0.9)

    def expected(lo, hi):
        return float(np.mean([interval_score((lo, hi), v, 0.8) for v in y[::10]]))

    best = expected(-q, q)
    for lo, hi in ((-0.8 * q, 0.8 * q), (-1.2 * q, 1.2 * q), (-q + 0.2, q + 0.2)):
        assert expected(lo, hi) > best


@pytest.mark.parametrize(("y", "loc", "scale", "df"), [(0.3, 0.1, 0.5, 3.0), (-2.0, 0.0, 1.0, 2.5),
                                                       (1.0, 0.2, 0.05, 10.0), (5.0, 0.0, 1.0, 1.5)])
def test_crps_student_t_matches_numerical_integral(y, loc, scale, df):
    from scipy.stats import t as student_t

    def cdf(x):
        return student_t.cdf((x - loc) / scale, df)

    num = integrate.quad(lambda x: cdf(x) ** 2, -np.inf, y, limit=500)[0] + integrate.quad(
        lambda x: (1 - cdf(x)) ** 2, y, np.inf, limit=500
    )[0]
    assert crps_student_t(y, loc, scale, df) == pytest.approx(num, rel=1e-7)
    with pytest.raises(ValueError):
        crps_student_t(y, loc, scale, 1.0)  # infinite CRPS


def test_score_forecast_reports_proper_scores_only_when_probabilistic():
    train = [(2000 + t, 100 * 1.04**t + (t % 2)) for t in range(8)]
    vals = [v for _, v in train]
    years = [y for y, _ in train]
    (f,) = LogDrift().fit(train).predict([1])
    sc = score_forecast(f, 140.0, vals, years)
    assert sc.interval_score is not None and sc.interval_score_scaled is not None
    assert sc.crps_log == pytest.approx(
        crps_student_t(math.log(140.0), f.predictive.loc, f.predictive.scale, f.predictive.df)
    )
    (g,) = LastValue().fit(train).predict([1])
    sg = score_forecast(g, 140.0, vals, years)
    assert sg.interval_score is None and sg.crps_log is None  # not applicable, never 0
    gapped = score_forecast(g, 140.0, vals, [*years[:-1], years[-1] + 1])
    assert gapped.scaled_error is None and gapped.scale_reason == "gapped_years"


def test_log_score_gaussian():
    assert log_score_gaussian(0.3, 0.0, 1.5) == pytest.approx(norm.logpdf(0.3, 0.0, 1.5))
    with pytest.raises(ValueError):
        log_score_gaussian(0.0, 0.0, 0.0)
    assert np.isfinite(crps_gaussian(100.0, 0.0, 1.0))
