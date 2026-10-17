"""FIX7 (QA-PRED review V6): fair density benchmark, prospective gate, two-way DM, admission labels."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any, ClassVar

import pytest

from reva.evaluation.conflict.stats import dm_test
from reva.evaluation.pred_models import fair_benchmark as FB
from reva.evaluation.pred_models import issue as I
from reva.evaluation.pred_models import realtime as R
from reva.evaluation.registry.questions import issue_timing, provider_published_on
from reva.models.forecasting.registry_baselines import Abstain, Predictive


def _case(origin: datetime, geo: str, point: float, outcome: float, known: datetime) -> R.Case:
    c = R.Case("weo", "real_gdp_growth", geo, origin, "h0", str(origin.year), outcome, known, 1.0)
    c.preds[R.WEO] = Predictive("point", point)
    return c


def test_error_fan_is_gaussian_around_the_official_point_with_rms_of_own_errors() -> None:
    p = FB.error_fan(2.0, [1.0, -1.0, 2.0, -2.0], min_n=4)
    assert isinstance(p, Predictive) and p.point == 2.0 and p.sigma == pytest.approx(math.sqrt(2.5))
    assert isinstance(FB.error_fan(2.0, [1.0], min_n=4), Abstain)


def test_error_fans_use_only_errors_published_at_the_origin() -> None:
    cases = []
    for k in range(12):
        o = datetime(2008 + k, 6, 1, tzinfo=UTC)
        known = datetime(2009 + k, 10, 1, tzinfo=UTC)  # outcome published 16 months later
        cases.append(_case(o, "FRA", 1.0, 1.0 + (k % 3 - 1), known))
    FB.add_error_fans(cases)
    own = [c.preds[FB.OWN["weo"]] for c in cases]
    # at origin k, usable errors are those of origins j with known_j <= origin_k, i.e. j <= k - 2 (k - 1 errors)
    assert all(isinstance(p, Abstain) for p in own[:9])
    assert isinstance(own[9], Predictive) and own[9].point == 1.0
    errs = [(j % 3 - 1) for j in range(10)]
    assert own[11].sigma == pytest.approx(math.sqrt(sum(e * e for e in errs) / 10))  # type: ignore[union-attr]


def test_fair_development_never_scores_holdout_origins() -> None:
    cases = [_case(datetime(2010 + k, 6, 1, tzinfo=UTC), "FRA", 1.0, 1.5, datetime(2011 + k, 10, 1, tzinfo=UTC))
             for k in range(12)]  # fmt: skip
    FB.add_error_fans(cases)
    out = FB.compare(cases, "weo", "2019-01-01")
    assert out["n_origins"] == 9  # 2010..2018 only
    purged = FB.compare(cases, "weo", "2019-01-01", purge=True)
    assert purged["n_origins"] == 8  # outcomes published on/after the split are dropped (QA-PRED X5)


def test_prospective_gate_pending_confirmed_suspended() -> None:
    few = [("FRA", 0.5, 1.0)] * 5
    assert I.fair_gate(few)["status"] == "pending"
    better = [(f"C{i % 10}", 0.5 + 0.01 * (i % 7), 1.0 + 0.01 * (i % 5)) for i in range(40)]
    assert I.fair_gate(better)["status"] == "confirmed"
    worse = [(f"C{i % 10}", 1.0 + 0.01 * (i % 7), 0.9 + 0.01 * (i % 5)) for i in range(40)]
    assert I.fair_gate(worse)["status"] == "suspended"


def test_registered_densities_are_labelled_and_the_fair_benchmark_issues_with_them() -> None:
    I.registered.cache_clear()
    scopes = I.issuing_scopes()
    assert I.FAIR_BENCHMARK in scopes and scopes[I.FAIR_BENCHMARK] == [("weo.gdp_growth", 0)]
    models = I.registry_models()
    for k in ("forecast.weo_conformal", "forecast.weo_bias_corrected"):
        assert "not a better point forecast" in models[k][1] and "weak benchmark" in models[k][1]
    for r in I.registered():
        assert r["claim"] == "calibrated_interval_around_official_point" and r["not_a_better_point_forecast"]
    p = I.forecast_weo_registered(I.FAIR_BENCHMARK, [], 2.0, [0.5] * 10)
    assert isinstance(p, Predictive) and p.point == 2.0 and p.sigma == pytest.approx(0.5)


def test_two_way_clustered_dm_is_wider_under_a_common_year_shock() -> None:
    d, cl, yr = [], [], []
    for y in range(10):
        shock = 1.0 if y % 2 else -1.0  # the same shock hits every country in a year
        for c in range(30):
            d.append(-0.1 + shock + 0.02 * c * (y % 3))
            cl.append(f"C{c}")
            yr.append(y)
    one = dm_test(d, cl)
    two = dm_test(d, cl, yr)
    assert two["variance"].startswith(("two_way", "max_one_way")) and two["n_time_clusters"] == 10
    assert abs(two["z"]) < abs(one["z"])  # country-only clustering overstates significance
    assert two["z_country_only"] == pytest.approx(one["z"])


def test_issue_timing_and_provider_publication_date() -> None:
    from datetime import date

    t = issue_timing(date(2026, 1, 1), date(2027, 1, 1), date(2026, 10, 17))
    assert t["kind"] == "near_nowcast" and 0.79 < t["elapsed_fraction_at_issue"] < 0.8
    assert issue_timing(date(2027, 1, 1), date(2028, 1, 1), date(2026, 10, 17))["kind"] == "forecast"
    refs = [{"flags": {"release": "April 2026", "as_of": "2026-01"}}, {"flags": '{"last_updated": "2026-11-03T10:00"}'}]
    assert provider_published_on(refs) == ("last_updated", date(2026, 11, 3))
    assert provider_published_on([{"flags": {"release": "2026-04"}}]) is None  # labels are never guessed


def test_health_final_vintage_admission_is_labelled() -> None:
    from reva.evaluation.health_registry import service as HS

    class F:
        key = "ghe.deaths"
        raw: ClassVar[dict[str, Any]] = {"backtest": {"kind": "final_vintage"}}

    a = HS.admission_basis(F(), ["forecast.health_blend"])  # type: ignore[arg-type]
    assert a is not None and a["final_vintage"] and "FINAL-VINTAGE RESULT" in a["note"]
    assert "next WHO Global Health Estimates release" in a["target_note"]
    assert HS.admission_basis(F(), []) is None  # type: ignore[arg-type]
