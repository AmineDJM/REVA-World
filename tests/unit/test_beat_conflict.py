"""V7 BEAT-CONFLICT pure functions (synthetic data, no database)."""

from __future__ import annotations

import math

import numpy as np

from reva.evaluation.conflict import beat as B


def _data(hi: int, *, spike_at: int | None = None) -> B.CMData:
    rng = np.random.default_rng(0)
    geos = [f"gw:{k}" for k in range(1, 13)]
    i0 = B.midx(2000, 1)
    cells: dict[str, dict[tuple[str, int], float]] = {"sb": {}, "ns": {}, "os": {}, "ev": {}}
    for k, g in enumerate(geos):
        for i in range(i0, hi + 1):
            if k < 4 and rng.random() < 0.6:
                cells["sb"][(g, i)] = float(rng.integers(1, 200))
                cells["ev"][(g, i)] = 3.0
    if spike_at is not None:
        cells["sb"][("gw:12", spike_at)] = 5000.0
    uni = {y: set(geos[:-1]) | ({"gw:12"} if y >= 2001 else set()) for y in range(2000, 2031)}
    return B.build_cmdata(
        cells=cells,
        spread_sets={"adm1": {("gw:1", i0 + 5): {"X-1", "X-2"}}, "pg": {}},
        econ={},
        universe=uni,
        covered=(i0, hi),
        neighbours=lambda g, _y: {"gw:1"} if g == "gw:2" else set(),
        i0=i0,
    )


def test_absence_zero_within_coverage_and_nan_outside_universe() -> None:
    d = _data(B.midx(2010, 12))
    g12 = d.geos.index("gw:12")
    assert math.isnan(d.y(g12, B.midx(2000, 6)))  # outside the universe in 2000: missing, never 0
    assert d.y(g12, B.midx(2005, 6)) == 0.0  # in the universe, no cell, covered: 0 recorded
    assert math.isnan(d.y(0, B.midx(2011, 1)))  # beyond coverage: missing
    assert d.adm3[0, 5 + 2] == 2 and d.adm3[0, 5 + 3] == 0  # 3-month spread window


def test_features_do_not_look_ahead() -> None:
    t = B.midx(2008, 6)
    a = B.features_at(_data(B.midx(2010, 12)), t)
    b = B.features_at(_data(B.midx(2010, 12), spike_at=t + 1), t)
    np.testing.assert_array_equal(np.nan_to_num(a, nan=-9), np.nan_to_num(b, nan=-9))


def test_forecasts_at_origin_independent_of_future() -> None:
    o = B.midx(2009, 3)
    r1, _ = B.run_origins(_data(B.midx(2010, 12)), [o], [1, 2])
    r2, _ = B.run_origins(_data(B.midx(2010, 12), spike_at=o + 5), [o], [1, 2])
    for k in r1:
        assert r1[k][B.CAND] == r2[k][B.CAND]
        assert r1[k][B.PERS] == r2[k][B.PERS]


def test_crps_point_mass_is_abs_error_and_fit_month() -> None:
    assert abs(B.crps_sample(np.array([2.0] * 10), 0.5) - 1.5) < 1e-12
    assert B.fit_month_for(B.midx(2012, 5)) == B.midx(2011, 12)
    assert B.fit_month_for(B.midx(2012, 12)) == B.midx(2012, 12)


def test_family_holm_counts_pending_as_one() -> None:
    tests = {"a": {"p_one_sided_t": 0.001, "mean_diff": -1.0}}
    out = B.family_holm(tests, ["a", "b", "c"])
    assert out["b"]["p_holm"] == 1.0 and not out["b"]["win"]
    assert abs(out["a"]["p_holm"] - 0.003) < 1e-12 and out["a"]["win"]


def test_stack_weight_uses_past_outcomes_only() -> None:
    perfect = B.Fc(math.log1p(30.0), 0.99, None)
    bad = B.Fc(0.0, 0.01, None)
    recs = [{"gw": "g", "origin": 1, "step": 1, B.VIEWS: perfect, B.CAND: bad}]
    w = B.stack_weight(recs, lambda _g, _i: 30.0, "logloss")
    assert w["w"] == 1.0
    assert B.stack_weight(recs, lambda _g, _i: None, "logloss")["n"] == 0
