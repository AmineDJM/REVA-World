"""V6 PRED-MODELS: direct AR/ARDL, Minnesota BVAR, DFM nowcast, combinations / conformal densities, the
real-time case builder (no look-ahead) and the locked-holdout gate."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import numpy as np
import polars as pl
import pytest

from reva.adapters.connectors.vintage_common import Rows, vintage_flags, year_bounds
from reva.evaluation.pred_models import derived as D
from reva.evaluation.pred_models import gate as G
from reva.evaluation.pred_models import realtime as R
from reva.evaluation.pred_models import service as S
from reva.evaluation.vintages import cells_from_atlas_frame
from reva.kernel.errors import PolicyDenied
from reva.models.forecasting import bvar as BV
from reva.models.forecasting import combination as CB
from reva.models.forecasting import dfm as DF
from reva.models.forecasting.direct import direct_forecast
from reva.models.forecasting.registry_baselines import Abstain, Predictive


def _ar1(n: int, phi: float, c: float, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    y = np.empty(n)
    y[0] = c / (1 - phi)
    for t in range(1, n):
        y[t] = c + phi * y[t - 1] + rng.standard_normal()
    return y


# ----------------------------------------------------------------------------- direct
def test_direct_ar_recovers_the_conditional_mean() -> None:
    y = _ar1(400, 0.7, 1.0, 1)
    p = direct_forecast(y, 2, pmax=3)
    assert isinstance(p, Predictive) and p.family == "normal"
    mean2 = 1.0 + 0.7 + 0.49 * y[-1]  # E[y_{T+2} | y_T] for an AR(1)
    assert abs(p.point - mean2) < 0.35
    assert 1.0 < p.sigma < 1.5  # type: ignore[operator]  # sqrt(1 + 0.49) = 1.22


def test_direct_ardl_uses_the_exogenous_series() -> None:
    rng = np.random.default_rng(2)
    x = rng.standard_normal(300)
    y = np.r_[0.0, 2.0 * x[:-1]] + 0.1 * rng.standard_normal(300)
    with_x = direct_forecast(y, 1, exog={"x": x}, pmax=1, qmax=1)
    without = direct_forecast(y, 1, pmax=1)
    assert isinstance(with_x, Predictive) and isinstance(without, Predictive)
    assert abs(with_x.point - 2.0 * x[-1]) < 0.1 and with_x.sigma < 0.2  # type: ignore[operator]


def test_direct_abstains_instead_of_repairing() -> None:
    assert isinstance(direct_forecast([1.0] * 5, 1), Abstain)
    y = list(_ar1(60, 0.5, 0, 3))
    y[30] = math.nan
    assert "never imputed" in direct_forecast(y, 1).reason  # type: ignore[union-attr]


# ----------------------------------------------------------------------------- BVAR
def test_bvar_posterior_close_to_truth_and_deterministic() -> None:
    rng = np.random.default_rng(4)
    A = np.array([[0.5, 0.1], [0.0, 0.8]])
    Y = np.zeros((300, 2))
    for t in range(1, 300):
        Y[t] = A @ Y[t - 1] + 0.5 * rng.standard_normal(2)
    fit = BV.fit_bvar(Y, own_lag_mean=(0.0, 1.0), p=1)
    assert isinstance(fit, BV.BvarFit)
    assert np.allclose(fit.coef[1:].T, A, atol=0.1)
    a = BV.bvar_forecast(Y, own_lag_mean=(0.0, 1.0), horizons=3, seed=7, n_draws=200, p=1)
    b = BV.bvar_forecast(Y, own_lag_mean=(0.0, 1.0), horizons=3, seed=7, n_draws=200, p=1)
    assert not isinstance(a, Abstain) and not isinstance(b, Abstain)
    assert a[2][1].sample == b[2][1].sample and list(a[0][0].sample) == sorted(a[0][0].sample)
    assert abs(a[0][1].point - (A @ Y[-1])[1]) < 0.15


def test_bvar_refuses_short_or_missing_samples() -> None:
    assert isinstance(BV.fit_bvar(np.ones((10, 3)), own_lag_mean=(0, 0, 0)), Abstain)
    Y = np.random.default_rng(0).standard_normal((80, 2))
    Y[5, 1] = np.nan
    assert isinstance(BV.fit_bvar(Y, own_lag_mean=(0, 0)), Abstain)


# ----------------------------------------------------------------------------- DFM
def test_dfm_nowcast_tracks_the_factor_with_a_ragged_edge() -> None:
    rng = np.random.default_rng(5)
    Q = 60
    M = 3 * Q
    f = np.zeros(M)
    for m in range(1, M):
        f[m] = 0.6 * f[m - 1] + rng.standard_normal()
    X = np.column_stack([f + 0.5 * rng.standard_normal(M) for _ in range(3)])
    mq = np.arange(M) // 3
    Fq = np.array([f[mq == q].mean() for q in range(Q)])
    g = 0.5 + 1.0 * Fq + 0.2 * rng.standard_normal(Q)
    X[-2:, :] = np.nan  # only the first month of the target quarter is published
    X[-3, 1] = np.nan
    gg = g.copy()
    gg[-1] = np.nan
    p = DF.dfm_nowcast(X, mq, gg, Q - 1)
    assert isinstance(p, Predictive)
    # the nowcast is closer than the previous quarter's value (persistence) on this design
    assert abs(p.point - g[-1]) < abs(g[-2] - g[-1]) + 1.0
    # previous quarter unpublished: nowcast recursively, wider
    gg2 = gg.copy()
    gg2[-2] = np.nan
    p2 = DF.dfm_nowcast(X, mq, gg2, Q - 1)
    assert isinstance(p2, Predictive) and p2.sigma > p.sigma  # type: ignore[operator]


# ----------------------------------------------------------------------------- combinations / conformal
def test_conformal_interval_coverage_on_exchangeable_errors() -> None:
    rng = np.random.default_rng(6)
    hits = 0
    for _ in range(400):
        past = rng.standard_normal(200)
        p = CB.conformal(0.0, past)
        assert isinstance(p, Predictive)
        iv = p.interval(0.8)
        assert iv is not None
        hits += iv[0] <= rng.standard_normal() <= iv[1]
    assert 0.74 <= hits / 400 <= 0.86
    assert isinstance(CB.conformal(0.0, [0.1] * 3), Abstain)


def test_equal_weight_point_and_pool() -> None:
    a = Predictive("normal", 0.0, mu=0.0, sigma=1.0)
    b = Predictive("normal", 2.0, mu=2.0, sigma=1.0)
    c = CB.equal_weight([a, b])
    assert c.point == 1.0 and c.family == "empirical" and len(c.sample) == 2 * CB.POOL_QUANTILES
    assert CB.equal_weight([a, Predictive("point", 3.0)]).family == "point"
    assert CB.bias_corrected(1.0, [0.5] * 20) == 1.5


# ----------------------------------------------------------------------------- derived: only known errors
def _case(origin: datetime, known: datetime, outcome: float, point: float) -> R.Case:
    c = R.Case("weo", "v", "X", origin, "h0", "2000", outcome, known, 1.0)
    c.preds[R.WEO] = Predictive("point", point)
    return c


def test_derived_models_use_only_errors_published_at_the_origin() -> None:
    t0 = datetime(2000, 1, 1, tzinfo=UTC)
    cases = [_case(t0 + timedelta(days=i), t0 + timedelta(days=i + 100), 1.0, 0.0) for i in range(30)]
    late = _case(t0 + timedelta(days=200), t0 + timedelta(days=10_000), 1.0, 0.0)
    probe = _case(t0 + timedelta(days=120), t0 + timedelta(days=500), 0.0, 0.0)
    spec = D.DerivedSpec("forecast.weo_conformal", "conformal", (R.WEO,), "t")
    D.add_derived([*cases, late, probe], [spec])
    p = probe.preds["forecast.weo_conformal"]
    # at day 120 only cases 0..20 (known at day 100..120) are admissible: 21 errors, all equal to +1
    assert isinstance(p, Abstain) or (len(p.sample) == 21 and set(p.sample) == {1.0})
    early = cases[5].preds["forecast.weo_conformal"]
    assert isinstance(early, Abstain)  # no error was published before day 105


# ----------------------------------------------------------------------------- gate
def test_kde_log_density_matches_the_gaussian_for_large_samples() -> None:
    x = np.random.default_rng(8).standard_normal(4000)
    assert abs(G.kde_log_density(x, 0.3) - (-0.5 * math.log(2 * math.pi) - 0.045)) < 0.05  # type: ignore[operator]


def test_paired_test_detects_a_better_candidate_and_holm() -> None:
    rows = []
    t0 = datetime(2000, 1, 1, tzinfo=UTC)
    rng = np.random.default_rng(9)
    for i in range(40):
        y = float(rng.standard_normal())
        c = R.Case("g7", "v", "X", t0 + timedelta(days=90 * i), "k0", "x", y, t0, 1.0)
        c.preds["good"] = Predictive("point", y + 0.1 * float(rng.standard_normal()))
        c.preds["bad"] = Predictive("point", y + float(rng.standard_normal()))
        rows.append(G.Scored(c, {k: G.score(p, y) for k, p in c.preds.items()}))  # type: ignore[misc]
    t = G.paired_test(rows, "good", "bad", "ae", 2)
    assert t["tested"] and t["p_better"] < 0.001 and t["relative"] < 0.5
    tests = [t, G.paired_test(rows, "bad", "good", "ae", 2)]
    G.holm(tests)
    assert tests[0]["holm_rejected"] and not tests[1]["holm_rejected"]


def test_holdout_opens_once_per_protocol() -> None:
    proto = {"model_version": "1.0.0", "hypotheses": [{"task": "g7", "variable": "v", "horizon": "k0",
             "candidate": "c", "benchmark": "b", "loss": "crps"}]}  # fmt: skip
    sha = S.check_holdout_unopened(proto, {})
    with pytest.raises(PolicyDenied):
        S.check_holdout_unopened(proto, {"openings": [{"protocol_sha256": sha}]})
    other = {**proto, "written_at": "later"}
    with pytest.raises(PolicyDenied):  # same hypothesis, same model version, new protocol: refused
        S.check_holdout_unopened(other, {"openings": [{"model_version": "1.0.0", "hypotheses": ["g7|v|k0|c|b|crps"]}]})


# ----------------------------------------------------------------------------- real-time WEO cases
def _weo_cells(vintages: list[tuple[str, datetime, dict[int, tuple[str, str]]]]) -> pl.DataFrame:
    rows = Rows()
    for v, avail, years in vintages:
        for y, (val, kind) in years.items():
            s, e = year_bounds(y)
            rows.add(
                source="vintage_weo",
                dataset="weo_dbnomics",
                indicator=f"NGDP_RPCH|v={v}",
                provider_geo="FRA",
                start=s,
                end=e,
                value=val,
                unit="pcent",
                raw_sha256="a" * 64,
                selector={"v": v, "y": y},
                flags=vintage_flags(
                    family="imf_weo",
                    vintage=v,
                    available_at=avail,
                    basis="weo_release_month_end",
                    variable="real_gdp_growth",
                    provider_variable="NGDP_RPCH",
                    estimate_kind=kind,
                ),
            )
    return cells_from_atlas_frame(
        rows.frame(), dataset_key="atlas.vintage_weo.weo_dbnomics", table_recorded_at=datetime(2026, 10, 17, tzinfo=UTC)
    ).with_columns(pl.lit("FRA").alias("iso3"))


def _hist(v: str, last: int, seed: int) -> dict[int, tuple[str, str]]:
    rng = np.random.default_rng(seed)
    d = {y: (f"{1.5 + rng.standard_normal():.3f}", "published_history_boundary_unknown") for y in range(1980, last + 1)}
    d[last + 1] = ("1.1", "projection")
    d[last + 2] = ("1.7", "projection")
    return d


def test_weo_cases_ignore_vintages_published_after_the_origin() -> None:
    base = [
        ("2010-04", datetime(2010, 5, 1, tzinfo=UTC), _hist("2010-04", 2009, 1)),
        ("2010-10", datetime(2010, 11, 1, tzinfo=UTC), _hist("2010-10", 2009, 2)),
    ]
    later = [("2011-04", datetime(2011, 5, 1, tzinfo=UTC), {y: ("99", "published_history_boundary_unknown")
                                                             for y in range(1980, 2011)})]  # fmt: skip
    origins = [datetime(2010, 6, 1, tzinfo=UTC), datetime(2010, 12, 1, tzinfo=UTC)]
    a = R.weo_cases(_weo_cells(base), "real_gdp_growth", origins)
    b = R.weo_cases(_weo_cells(base + later), "real_gdp_growth", origins)
    assert len(a) == len(b) == 4
    for x, y in zip(a, b, strict=True):
        assert x.preds.keys() == y.preds.keys()
        for k in x.preds:
            assert repr(x.preds[k]) == repr(y.preds[k])
        assert x.info["vintage_in_force"] in ("2010-04", "2010-10")
    # the outcome of 2010 (first vintage on/after 1 Oct 2011) does not exist in either set: no score possible
    assert all(c.outcome is None for c in a)
    assert a[0].preds[R.WEO].point == 1.1  # type: ignore[union-attr]


# ----------------------------------------------------------------------------- registry issuance of winners
def test_registered_weo_forecasters_issue_calibrated_densities() -> None:
    from reva.evaluation.pred_models import issue as I

    hist = [(y, 2.0 + 0.5 * math.sin(y)) for y in range(1990, 2026)]
    errs = list(np.random.default_rng(10).standard_normal(300))
    p = I.forecast_weo_registered("forecast.weo_conformal", hist, 3.0, errs)
    assert isinstance(p, Predictive) and p.point == 3.0 and len(p.sample) == 200
    b = I.forecast_weo_registered("forecast.weo_bias_corrected", hist, 3.0, [e + 1.0 for e in errs])
    assert isinstance(b, Predictive) and b.point > 3.0
    assert isinstance(I.forecast_weo_registered("forecast.weo_conformal", hist, None, errs), Abstain)
    assert isinstance(I.forecast_weo_registered("forecast.weo_conformal", hist, 3.0, errs[:5]), Abstain)


def test_only_holdout_confirmed_scopes_with_a_family_issue() -> None:
    from reva.evaluation.pred_models import issue as I

    scopes = I.issuing_scopes()
    for mk, fams in scopes.items():
        assert mk in I.ISSUERS and all(f in I.FAMILY_OF.values() for f in fams)
    reg = {(r["candidate"], r["task"], r["variable"], r["horizon"]) for r in I.registered()}
    for mk, fams in scopes.items():
        for fam in fams:
            key = next(k for k, v in I.FAMILY_OF.items() if v == fam)
            assert (mk, *key) in reg
