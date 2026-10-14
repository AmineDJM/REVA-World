"""Tournament promotion logic."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pytest
import yaml

from reva.kernel.uncertainty import UncertaintySpec
from reva.models.forecasting import LastValue, LocalLevelLog, LogDrift
from reva.models.forecasting.base import Forecast, prepare
from reva.models.tournament import (
    NOT_APPLICABLE,
    dm_hln_test,
    holdout_non_inferiority,
    holm_rejections,
    moving_block_bootstrap_means,
    resolve_policy,
    run_tournament,
)

POLICY_PATH = "registry/policies/promotion.yaml"


def load_policy():
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    return yaml.safe_load((root / POLICY_PATH).read_text())


def geometric_panel(n_series: int, n_years: int):
    return {
        f"s{i:02d}": [(2000 + t, (50.0 + 10 * i) * (1.02 + 0.005 * i) ** t) for t in range(n_years)]
        for i in range(n_series)
    }


def test_short_panel_keeps_incumbent_insufficient_evaluation():
    panel = geometric_panel(2, 6)
    res = run_tournament(
        panel, [LogDrift()], replay_mode="revised_data_retrospective", seed=3, policy=load_policy()
    )
    d = res.decisions["forecast.log_drift"]
    assert d.decision == "keep_incumbent"
    assert d.n_pairs < 30
    assert any(r.startswith("insufficient_evaluation") for r in d.reasons)
    assert "forecast.last_value" in res.metrics and res.metrics["forecast.last_value"].is_baseline


def test_long_geometric_panel_promotes_log_drift():
    panel = geometric_panel(12, 20)
    kw = dict(replay_mode="revised_data_retrospective", seed=20261014, policy=load_policy())
    res = run_tournament(panel, [LastValue(), LogDrift(), LocalLevelLog()], **kw)
    assert res.replay_mode == "revised_data_retrospective"
    assert res.policy["policy_version"] == "promotion-1.0.0"
    d = res.decisions["forecast.log_drift"]
    assert d.decision == "promote", d.reasons
    assert d.n_pairs >= 30
    assert d.test_method == "dm_hln_holm" and d.alpha == 0.025
    assert any(t.rejected for t in d.horizon_tests.values())
    assert d.n_origins > 0 and d.n_series == 12 and d.n_subgroups_tested == 12 + 3
    assert d.bootstrap_interval is not None and d.bootstrap_interval[1] < 0  # descriptive
    assert d.bootstrap_block_length >= 3
    assert all(v <= 0.10 for v in d.subgroup_degradation.values())
    assert all(t.status == "ok" for t in d.subgroup_tests.values())
    assert d.paired_mase is not None and d.paired_mase[0] < d.paired_mase[1]
    # naive baseline always evaluated and reported
    base = res.metrics["forecast.last_value"]
    assert base.is_baseline and base.n_predictions > 0 and base.coverage is None
    # R3-11: no probabilistic forecast -> proper scores are not applicable, never 0
    assert base.interval_score_scaled == NOT_APPLICABLE and base.crps_log == NOT_APPLICABLE
    assert base.n_without_probabilistic == base.n_predictions
    ll = res.metrics["forecast.local_level_log"]
    assert isinstance(ll.interval_score_scaled, float) and isinstance(ll.crps_log, float)
    assert ll.nominal_level == 0.8 and ll.coverage_gap == pytest.approx(ll.coverage - 0.8)
    # R3-06: common-case metrics over every compared model
    assert set(res.paired_models) == set(res.metrics)
    assert res.n_paired_cases > 0
    assert {m.n_scaled for m in res.paired_metrics.values()} == {res.n_paired_cases}
    assert res.paired_metrics["forecast.local_level_log"].n_scaled < base.n_scaled
    assert res.metrics["forecast.log_drift"].mase < base.mase
    # subgroups by series and horizon exist
    assert ("series_id", "s00") in res.subgroup_metrics
    assert ("horizon", "3") in res.subgroup_metrics
    # per-prediction records are complete
    r = next(r for r in res.records if r.model == "forecast.log_drift")
    assert r.target > 0 and r.abs_error >= 0 and r.mase_scaled_error is not None
    # local level ineligible at short origins is reported, not hidden
    assert any(i.report.model_key == "forecast.local_level_log" for i in res.ineligible)
    # determinism
    again = run_tournament(panel, [LastValue(), LogDrift(), LocalLevelLog()], **kw)
    assert again.decisions == res.decisions


def test_naive_baseline_added_when_not_a_candidate():
    res = run_tournament(
        geometric_panel(3, 8), [LogDrift()], replay_mode="vintage_strict", seed=0, policy={}
    )
    assert res.replay_mode == "vintage_strict"
    assert res.metrics["forecast.last_value"].n_predictions > 0
    assert set(res.decisions) == {"forecast.log_drift"}


def test_moving_block_bootstrap_is_seeded():
    import numpy as np

    x = np.arange(10, dtype=float)
    a = moving_block_bootstrap_means(x, 3, 50, np.random.default_rng(1))
    b = moving_block_bootstrap_means(x, 3, 50, np.random.default_rng(1))
    assert (a == b).all() and a.shape == (50,)


def test_driftless_random_walks_do_not_promote_log_drift():
    import math

    import numpy as np

    rng = np.random.default_rng(5)
    panel = {
        f"s{i}": [
            (2000 + t, float(100 * math.exp(v)))
            for t, v in enumerate(np.cumsum(0.05 * rng.standard_normal(20)))
        ]
        for i in range(12)
    }
    res = run_tournament(panel, [LogDrift()], replay_mode="synthetic", seed=1, policy={})
    d = res.decisions["forecast.log_drift"]
    assert d.decision == "keep_incumbent"
    assert any(r.startswith("loss_difference_not_significant") for r in d.reasons)
    assert not any(r.startswith("insufficient_evaluation") for r in d.reasons)


def test_origins_need_three_consecutive_training_years():
    res = run_tournament(geometric_panel(2, 8), [], replay_mode="synthetic", seed=0, policy={})
    assert min(r.origin for r in res.records) == 2002  # 3 training points
    assert all(r.mase_scaled_error is not None for r in res.records)
    gapped = {"g": [(y, 100.0 + y % 7) for y in (2000, 2001, 2002, 2004, 2005, 2006, 2007, 2008)]}
    res = run_tournament(gapped, [], replay_mode="synthetic", seed=0, policy={})
    after_gap = [r for r in res.records if r.origin >= 2004]
    assert after_gap and all(r.mase_scaled_error is None for r in after_gap)
    assert all(r.scale_reason == "gapped_years" for r in after_gap)


def test_log_drift_records_carry_proper_scores_when_quantified():
    rng = np.random.default_rng(3)
    panel = {
        f"s{i}": [(2000 + t, float(math.exp(v))) for t, v in enumerate(
            np.log(50.0) + np.cumsum(rng.normal(0.02, 0.03, 15)))]
        for i in range(3)
    }
    res = run_tournament(panel, [LogDrift()], replay_mode="synthetic", seed=0, policy={})
    recs = [r for r in res.records if r.model == "forecast.log_drift"]
    n3 = [r for r in recs if r.origin == 2002]  # 3 training points: df = 1 -> unquantified
    assert n3 and all(r.interval is None and r.crps_log is None for r in n3)
    later = [r for r in recs if r.origin >= 2003]
    assert all(r.interval_score_scaled is not None and r.crps_log is not None for r in later)
    m = res.metrics["forecast.log_drift"]
    assert m.n_crps == len(later) and m.n_without_probabilistic == len(n3)


def test_dm_hln_matches_hand_computation():
    x = [0.3, -0.1, 0.4, 0.2, -0.3, 0.5, 0.1, 0.0, 0.2, 0.6]
    t = dm_hln_test(x, horizon=2)
    a = np.asarray(x)
    T, e = a.size, a - a.mean()
    g0, g1 = float(e @ e) / T, float(e[1:] @ e[:-1]) / T
    dm = a.mean() / math.sqrt((g0 + 2 * g1) / T)
    hln = dm * math.sqrt((T + 1 - 4 + 2 / T) / T)
    from scipy.stats import t as student_t

    assert t.kernel == "rectangular" and t.lag == 1 and t.df == T - 1
    assert t.statistic == pytest.approx(hln)
    assert t.p_greater == pytest.approx(student_t.sf(hln, T - 1))
    assert t.p_less + t.p_greater == pytest.approx(1.0)


def test_dm_negative_rectangular_variance_falls_back_to_bartlett():
    x = [1.0, -1.0] * 6  # gamma0 = 1, gamma1 = -11/12: rectangular LRV < 0
    t = dm_hln_test(x, horizon=2)
    assert t.kernel == "bartlett" and t.statistic == pytest.approx(0.0)
    assert t.p_less == pytest.approx(0.5)


def test_holm_step_down():
    assert holm_rejections({1: 0.001, 2: 0.02, 3: 0.04}, 0.05) == {1: 0.05 / 3, 2: 0.025, 3: 0.05}
    assert holm_rejections({1: 0.001, 2: 0.03, 3: 0.04}, 0.05) == {1: 0.05 / 3}
    assert holm_rejections({1: 0.02, 2: 0.03}, 0.025) == {}


def test_policy_defaults_and_validation():
    pol = resolve_policy(load_policy())
    assert pol["policy_version"] == "promotion-1.0.0"
    assert pol["paired_test"] == {"method": "dm_hln_holm", "alpha": 0.025, "min_origins_per_horizon": 8}
    assert pol["subgroup_test"] == {"alpha": 0.05, "min_origins": 5}
    assert pol["holdout_non_inferiority"]["margin"] == 0.0
    assert pol["bootstrap"]["block_length"] == 3  # still read, descriptive interval only
    with pytest.raises(ValueError):
        resolve_policy({"paired_test": {"method": "naive_iid"}})
    with pytest.raises(ValueError):
        resolve_policy({"holdout_non_inferiority": {"margin": -0.1}})


@dataclass(frozen=True)
class _FittedOffset:
    model_key: str
    origin_year: int
    last: float
    slope: float
    model_version: str = "0.0.0"

    def predict(self, horizons, level=0.8):
        return [
            Forecast(self.model_key, self.model_version, self.origin_year, self.origin_year + h, h,
                     self.last + self.slope * h, None, None, None, UncertaintySpec.unknown("synthetic"))
            for h in horizons
        ]


class Offset:
    """Synthetic point forecaster: last value + slope * h (no interval)."""

    version = "0.0.0"
    min_train = 1

    def __init__(self, key: str, slope: float) -> None:
        self.key, self.slope = key, slope

    def fit(self, train):
        s = prepare(train)
        return _FittedOffset(self.key, s.years[-1], s.values[-1], self.slope)


def test_subgroup_point_breach_without_credibility_does_not_block_and_credible_breach_needs_review():
    # Challenger "up" is much better overall (series drift up) except on one series
    # that drifts down, where it is credibly worse -> needs_review, not promote.
    rng = np.random.default_rng(11)
    panel = {f"s{i}": [(2000 + t, float(v)) for t, v in enumerate(100 + np.cumsum(rng.normal(1.5, 0.5, 24)))]
             for i in range(8)}
    panel["down"] = [(2000 + t, float(v)) for t, v in enumerate(100 + np.cumsum(rng.normal(-1.5, 0.5, 24)))]
    res = run_tournament(panel, [Offset("up", 1.5), Offset("flat", 0.0)], "flat", (1, 2, 3),
                         replay_mode="synthetic", seed=1, policy={})
    d = res.decisions["up"]
    assert d.decision == "needs_review", d.reasons
    assert d.subgroup_tests["series_id=down"].status == "credible_degradation"
    assert any(r.startswith("subgroup_degradation: series_id=down") for r in d.reasons)


def test_holdout_non_inferiority_margin_and_test():
    better = [(f"s{i}", 0.8 + 0.01 * i, 1.0) for i in range(8)]
    ok = holdout_non_inferiority(better)
    assert ok.passed and ok.rule == "point_estimate" and ok.margin == 0.0
    assert ok.relative_change < 0 and ok.p_value is not None and ok.statistically_established
    assert ok.reasons[0].startswith("locked_holdout_confirmed")
    worse = [(f"s{i}", 1.05, 1.0) for i in range(8)]  # 5% worse: rejected at margin 0
    w = holdout_non_inferiority(worse)
    assert not w.passed and w.reasons[0].startswith("locked_holdout_regression")
    assert holdout_non_inferiority(worse, margin=0.10).passed  # explicit tolerance from policy
    noisy = [("a", 0.9, 1.0), ("b", 1.05, 1.0), ("c", 0.95, 1.0)]
    n = holdout_non_inferiority(noisy, require_statistical=True)
    assert not n.passed and n.rule == "one_sided_test"
    assert n.reasons[0].startswith("locked_holdout_non_inferiority_not_established")
    undefined = holdout_non_inferiority([("a", None, 1.0), ("b", 1.0, None)])
    assert not undefined.passed and undefined.n_pairs == 0
