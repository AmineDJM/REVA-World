"""No look-ahead: predictions depend only on the training window."""

from __future__ import annotations

import math

import numpy as np
import pytest

from reva.evaluation.splits import CutoffScopedSeries, rolling_origins
from reva.kernel.errors import LeakageBlocked
from reva.models.forecasting import IneligibleReport, LastValue, LocalLevelLog, LogDrift
from reva.models.tournament import run_tournament


def series(n: int, seed: int):
    rng = np.random.default_rng(seed)
    z = np.log(50.0) + np.cumsum(0.03 + 0.04 * rng.standard_normal(n))
    return [(2000 + t, float(math.exp(v))) for t, v in enumerate(z)]


@pytest.mark.parametrize("model", [LastValue(), LogDrift(), LocalLevelLog()])
def test_fit_on_scoped_window_ignores_later_data(model):
    full = series(25, seed=11)
    cutoff = 2012
    truncated = [o for o in full if o[0] <= cutoff]
    # Later data changed drastically: must not matter.
    perturbed = [(y, v * 10.0 if y > cutoff else v) for y, v in full]
    preds = []
    for data in (truncated, full, perturbed):
        scoped = CutoffScopedSeries(data, cutoff=cutoff)
        fitted = model.fit(scoped.observations())
        assert not isinstance(fitted, IneligibleReport)
        preds.append([(f.point, f.interval) for f in fitted.predict([1, 2, 3])])
    assert preds[0] == preds[1] == preds[2]


def test_cutoff_scoped_series_blocks_future_access():
    scoped = CutoffScopedSeries(series(10, seed=1), cutoff=2004)
    assert scoped.years()[-1] == 2004
    assert scoped.value(2004) > 0
    with pytest.raises(LeakageBlocked):
        scoped.value(2005)
    with pytest.raises(LeakageBlocked):
        scoped.window(2000, 2006)


def test_rolling_origins_deterministic_and_bounded():
    years = list(range(2000, 2010))
    a = rolling_origins(years, min_train=3, horizons=(1, 2))
    assert a == rolling_origins(reversed(years), min_train=3, horizons=(2, 1))
    for ro in a:
        assert len(ro.train_years) == ro.origin_index + 1 >= 3
        assert all(t > ro.train_years[-1] for t in ro.target_years)
        assert all(t in years for t in ro.target_years)
    assert a[0].train_years == (2000, 2001, 2002) and a[0].target_years == (2003, 2004)
    assert a[-1].target_years == (2009,)
    emb = rolling_origins(years, min_train=3, horizons=(1,), embargo=1)
    assert emb[0].target_years == (2004,) and emb[0].steps == (2,)


def test_tournament_records_unchanged_by_future_panel_data():
    full = {f"s{i}": series(16, seed=i) for i in range(3)}
    end = 2010
    short = {k: [o for o in v if o[0] <= end] for k, v in full.items()}
    kw = dict(
        candidates=[LastValue(), LogDrift(), LocalLevelLog()],
        replay_mode="revised_data_retrospective",
        seed=1,
        policy={},
    )
    r_short = run_tournament(short, **kw)
    r_full = run_tournament(full, **kw)
    full_subset = [r for r in r_full.records if r.target_year <= end]
    assert len(r_short.records) > 0
    assert sorted(r_short.records, key=repr) == sorted(full_subset, key=repr)
