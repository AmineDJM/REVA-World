"""Registry baselines (PRED-REGISTRY): pure numerics."""

from __future__ import annotations

import math
import random

import pytest

from reva.models.forecasting.registry_baselines import (
    Abstain,
    Predictive,
    _norm_ppf,
    ar,
    final_run,
    hist_quantile,
    persistence,
    run_baseline,
    seasonal_naive,
)


def series(vals: list[float], start: int = 2000 * 12) -> list[tuple[int, float]]:
    return [(start + i, v) for i, v in enumerate(vals)]


def test_norm_ppf_matches_known_quantiles() -> None:
    assert _norm_ppf(0.975) == pytest.approx(1.959963985, abs=1e-8)
    assert _norm_ppf(0.5) == pytest.approx(0.0, abs=1e-12)
    assert _norm_ppf(0.01) == pytest.approx(-2.326347874, abs=1e-8)


def test_final_run_keeps_last_consecutive_segment_and_rejects_unsorted() -> None:
    h = [(1, 1.0), (2, 2.0), (5, 5.0), (6, 6.0), (7, 7.0)]
    assert final_run(h, 100) == [(5, 5.0), (6, 6.0), (7, 7.0)]
    assert final_run(h, 2) == [(6, 6.0), (7, 7.0)]
    with pytest.raises(ValueError):
        final_run([(2, 1.0), (1, 1.0)], 10)


def test_persistence_point_and_spread() -> None:
    vals = [float(i % 3) for i in range(30)]
    h = series(vals)
    p = persistence(h, h[-1][0] + 1, 12)
    assert isinstance(p, Predictive) and p.family == "normal"
    assert p.point == vals[-1]
    d = [vals[i + 1] - vals[i] for i in range(29)]
    assert p.sigma == pytest.approx(math.sqrt(sum(x * x for x in d) / len(d)))
    assert isinstance(persistence(series(vals[:5]), 2000 * 12 + 6, 12), Abstain)
    assert isinstance(persistence(h, h[-1][0], 12), Abstain)  # target not after the last observation


def test_seasonal_naive_uses_same_month_last_year() -> None:
    vals = [float(m % 12) + 0.1 * y for y in range(5) for m in range(12)]
    h = series(vals)
    target = h[-1][0] + 3  # 3 months ahead: same month one year earlier = index -12+3
    p = seasonal_naive(h, target, 12)
    assert isinstance(p, Predictive)
    assert p.point == vals[len(vals) - 1 + 3 - 12]
    assert isinstance(seasonal_naive([(y, 1.0 + y) for y in range(2000, 2030)], 2031, 1), Abstain)


def test_ar_recovers_mean_reversion_and_abstains_when_short() -> None:
    rnd = random.Random(7)
    y, vals = 0.0, []
    for _ in range(200):
        y = 2.0 + 0.5 * (y - 2.0) + rnd.gauss(0, 0.3)
        vals.append(y)
    h = series(vals)
    p = ar(h, h[-1][0] + 24, 12)
    assert isinstance(p, Predictive) and p.family == "normal"
    assert p.point == pytest.approx(2.0, abs=0.3)  # far horizon -> unconditional mean
    p1 = ar(h, h[-1][0] + 1, 12)
    assert isinstance(p1, Predictive) and p1.sigma is not None and p.sigma is not None
    assert p1.sigma < p.sigma  # h-step variance grows with the horizon
    assert isinstance(ar(series(vals[:10]), 2000 * 12 + 11, 12), Abstain)


def test_hist_quantile_empirical_and_rule_of_succession() -> None:
    vals = [float(i) for i in range(25)]  # every 1-step change is +1
    vals[10] = 9.5  # one different change pair
    h = series(vals)
    p = hist_quantile(h, h[-1][0] + 1, 12)
    assert isinstance(p, Predictive) and p.family == "empirical"
    assert p.sample == tuple(sorted(p.sample))
    n = len(p.sample)
    assert p.prob_above(1e9) == pytest.approx(1 / (n + 2))  # never certainty
    assert p.prob_above(-1e9) == pytest.approx((n + 1) / (n + 2))
    flat = series([3.0] * 30)
    assert isinstance(hist_quantile(flat, flat[-1][0] + 1, 12), Abstain)


def test_predictive_validation_and_interval() -> None:
    with pytest.raises(ValueError):
        Predictive("normal", 1.0, mu=1.0, sigma=0.0)
    with pytest.raises(ValueError):
        Predictive("empirical", 1.0, sample=(2.0, 1.0))
    p = Predictive("normal", 0.0, mu=0.0, sigma=1.0)
    lo, hi = p.interval(0.8) or (0, 0)
    assert lo == pytest.approx(-1.2815516, abs=1e-6) and hi == pytest.approx(1.2815516, abs=1e-6)
    pt = Predictive("point", 3.0)
    assert pt.cdf(1.0) is None and pt.interval(0.8) is None and pt.prob_above(1.0) is None
    with pytest.raises(KeyError):
        run_baseline("forecast.nope", [], 1, 12)
