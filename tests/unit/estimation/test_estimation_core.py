"""Event-study / local-projection core: synthetic panels with known effects (the estimators must recover the truth),
exactness of the fixed-effect OLS against explicit dummy regressions, HP filter, determinism."""

from __future__ import annotations

import numpy as np
import pytest

from reva.estimation.core import (
    EstimationError,
    Grid,
    LPSpec,
    bin_means,
    event_study,
    hp_filter,
    lhs_matrix,
    local_projection,
    ols_fe,
    permutation_placebo,
)


def synthetic_growth(
    *,
    c: int = 150,
    t: int = 60,
    theta: tuple[float, ...] = (-2.0, -1.5, -1.0, -0.5),
    p_onset: float = 0.03,
    anticipation: float = 0.0,
    seed: int = 1,
) -> tuple[Grid, np.ndarray]:
    """Growth rates g = a_i + g_t + sum_k theta_k D[t-k] (+ anticipation at t-2) + N(0, 1)."""
    rng = np.random.default_rng(seed)
    alpha = rng.normal(3.0, 1.0, c)
    tau = rng.normal(0.0, 1.0, t)
    d = (rng.random((c, t)) < p_onset).astype(float)
    g = alpha[:, None] + tau[None, :] + rng.normal(0.0, 1.0, (c, t))
    for k, th in enumerate(theta):
        g[:, k:] += th * d[:, : t - k]
    if anticipation:
        g[:, : t - 2] += anticipation * d[:, 2:]
    out = {(f"C{i:03d}", 1950 + j): float(g[i, j]) for i in range(c) for j in range(t)}
    ons = {(f"C{i:03d}", 1950 + j): 1 for i in range(c) for j in range(t) if d[i, j]}
    truth = np.cumsum(np.r_[theta, np.zeros(3)])
    return Grid.build(out, ons), truth


def test_local_projection_recovers_cumulative_effect() -> None:
    grid, truth = synthetic_growth()
    res = local_projection(grid, LPSpec(horizons=5, lags=2, lhs="cumsum"))
    for e in res.irf:
        assert abs(e.point - truth[e.h]) < max(0.6, 3 * e.se), (e.h, e.point, truth[e.h])
        assert e.n_clusters == 150
    covered = sum(1 for e in res.irf if e.ci_low <= truth[e.h] <= e.ci_high)
    assert covered >= 4
    # no anticipation in the DGP: placebo leads are not significant
    assert all(e.ci_low <= 0 <= e.ci_high for e in res.leads)


def test_local_projection_level_outcome_diff_mode() -> None:
    """A permanent +2 pp level shift (e.g. unemployment rate) after onset, recovered in 'diff' mode."""
    rng = np.random.default_rng(5)
    c, t = 120, 50
    d = (rng.random((c, t)) < 0.03).astype(float)
    step = np.cumsum(d, axis=1) * 2.0
    y = rng.normal(6.0, 2.0, c)[:, None] + rng.normal(0, 0.5, t)[None, :] + step + rng.normal(0, 0.5, (c, t))
    out = {(f"K{i}", 2000 + j): float(y[i, j]) for i in range(c) for j in range(t)}
    ons = {(f"K{i}", 2000 + j): 1 for i in range(c) for j in range(t) if d[i, j]}
    res = local_projection(Grid.build(out, ons), LPSpec(horizons=4, lags=1, lhs="diff"))
    for e in res.irf:
        assert abs(e.point - 2.0) < max(0.3, 3 * e.se)


def test_event_study_pretrend_test_detects_anticipation() -> None:
    clean, _ = synthetic_growth(seed=3)
    es = event_study(clean, leads=3, lags=4)
    assert es["pretrend_test"]["p"] > 0.01
    k0 = next(c for c in es["coefficients"] if c["k"] == 0)
    assert k0["point"] == pytest.approx(-2.0, abs=0.4)
    antic, _ = synthetic_growth(seed=3, anticipation=-1.5)
    es2 = event_study(antic, leads=3, lags=4)
    assert es2["pretrend_test"]["p"] < 0.001
    lp = local_projection(antic, LPSpec(horizons=2, lags=0, lhs="cumsum", treat_lags=False))
    # lead coefficient = Y[t+h] - Y[t-1]: a dip at t-2 leaves Y[t-3] above Y[t-1]
    assert any(e.ci_low > 0 for e in lp.leads)


def test_permutation_placebo_null_and_effect() -> None:
    grid, _ = synthetic_growth(c=80, t=40, seed=11)
    spec = LPSpec(horizons=2, lags=1, lhs="cumsum")
    obs = local_projection(grid, spec)
    pl = permutation_placebo(grid, spec, obs, n_perm=30, seed=7)
    assert all(h["perm_p"] <= 1 / 31 + 1e-12 for h in pl["horizons"])
    null, _ = synthetic_growth(c=80, t=40, seed=11, theta=(0.0,))
    obs0 = local_projection(null, spec)
    pl0 = permutation_placebo(null, spec, obs0, n_perm=30, seed=7)
    assert min(h["perm_p"] for h in pl0["horizons"]) > 0.05
    # deterministic: same seed, same draws
    again = permutation_placebo(null, spec, obs0, n_perm=30, seed=7)
    assert again == pl0


def test_determinism() -> None:
    a, _ = synthetic_growth(seed=2)
    b, _ = synthetic_growth(seed=2)
    ra = local_projection(a, LPSpec()).as_dict()
    rb = local_projection(b, LPSpec()).as_dict()
    assert ra == rb


def test_ols_fe_matches_dummy_regression() -> None:
    statsmodels = pytest.importorskip("statsmodels.api")
    rng = np.random.default_rng(0)
    n, g = 600, 30
    grp = rng.integers(0, g, n)
    yr = rng.integers(0, 12, n)
    x = rng.normal(size=(n, 2))
    y = 1.5 * x[:, 0] - 0.5 * x[:, 1] + rng.normal(size=g)[grp] + rng.normal(size=12)[yr] + rng.normal(size=n)
    dummies = np.column_stack(
        [(grp == k).astype(float) for k in range(g)] + [(yr == k).astype(float) for k in range(1, 12)]
    )
    full = statsmodels.OLS(y, np.column_stack([x, dummies])).fit()
    mine = ols_fe(y, x, ["a", "b"], fes=[grp.astype(np.int64), yr.astype(np.int64)], vcov="classical")
    assert np.allclose(mine.coef, full.params[:2], atol=1e-8)
    assert np.allclose(mine.se(), full.bse[:2], rtol=1e-6)
    # cluster-robust without fixed effects equals statsmodels' CR1
    xc = np.column_stack([np.ones(n), x])
    sm = statsmodels.OLS(y, xc).fit(cov_type="cluster", cov_kwds={"groups": grp})
    mc = ols_fe(y, xc, ["c", "a", "b"], cluster=grp.astype(np.int64), vcov="cluster")
    assert np.allclose(mc.se(), sm.bse, rtol=1e-6)


def test_ols_fe_rejects_degenerate() -> None:
    with pytest.raises(EstimationError):
        ols_fe(np.ones(3), np.ones((3, 1)), ["a"], fes=[np.array([0, 1, 2])], vcov="classical")


def test_hp_filter_matches_statsmodels() -> None:
    sm = pytest.importorskip("statsmodels.tsa.filters.hp_filter")
    y = np.log(np.linspace(100, 180, 20) + np.sin(np.arange(20)) * 3)
    _, trend = sm.hpfilter(y, lamb=100)
    assert np.allclose(hp_filter(y, 100.0), trend, atol=1e-9)


def test_bin_means_are_group_means() -> None:
    v = np.array([1.0, 2.0, 3.0, 10.0, 12.0, np.nan])
    r = bin_means(v, ["N", "N", "N", "F", "F", "F"])
    assert r["N"]["point"] == pytest.approx(2.0)
    assert r["F"]["point"] == pytest.approx(11.0)
    assert r["F"]["n"] == 2


def test_lhs_modes() -> None:
    y = np.array([[1.0, 2.0, 4.0, 8.0]])
    assert lhs_matrix(y, 1, "diff")[0, 1] == pytest.approx(4.0 - 1.0)
    assert lhs_matrix(y, 1, "cumsum")[0, 1] == pytest.approx(2.0 + 4.0)
    assert lhs_matrix(y, 0, "logdiff")[0, 1] == pytest.approx(100 * np.log(2.0))
    assert lhs_matrix(y, -2, "cumsum")[0, 2] == pytest.approx(-2.0)  # -(y[t-1]) = level change t-2 -> t-1


def test_coverage_mask_excludes_uncoded_years() -> None:
    grid, _ = synthetic_growth(c=40, t=30, seed=4)
    out = {(c, y): float(grid.y[i, j]) for i, c in enumerate(grid.countries) for j, y in enumerate(grid.years)}
    ons = {(c, y): 1 for i, c in enumerate(grid.countries) for j, y in enumerate(grid.years) if grid.treat[i, j]}
    g2 = Grid.build(out, ons, treat_years=(1960, 1970))
    assert g2.at_risk[:, 0].sum() == 0 and g2.at_risk[:, 10].all()
    r = local_projection(g2, LPSpec(horizons=1, lags=1))
    assert r.sample["onsets_at_risk"] < r.sample["onsets_on_grid"]
