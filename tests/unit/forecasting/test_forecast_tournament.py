"""Tournament promotion logic."""

from __future__ import annotations

import yaml

from reva.models.forecasting import LastValue, LocalLevelLog, LogDrift
from reva.models.tournament import moving_block_bootstrap_means, run_tournament

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
    assert d.bootstrap_interval is not None and d.bootstrap_interval[1] < 0
    assert all(v <= 0.10 for v in d.subgroup_degradation.values())
    # naive baseline always evaluated and reported
    base = res.metrics["forecast.last_value"]
    assert base.is_baseline and base.n_predictions > 0 and base.coverage is None
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
