"""Conflict-risk forecasting (V6 PRED-CONFLICT): families, features, models, DM/Holm, back-test, VIEWS sums (offline).

Synthetic panel only (no provider data): the numbers below test the mechanics, not any real-world claim.
"""

from __future__ import annotations

import json
import math
from datetime import date

import polars as pl
import pytest

from reva.evaluation.conflict import families as F
from reva.evaluation.conflict import models as M
from reva.evaluation.conflict.backtest import run_backtest
from reva.evaluation.conflict.panel import Panel
from reva.evaluation.conflict.service import build_questions_and_forecasts
from reva.evaluation.conflict.stats import dm_test, holm
from reva.evaluation.conflict.views import ViewsRun, annual_table, december_runs, latest_covering, runs_from_frame
from reva.evaluation.registry.scores import score_forecast
from reva.evaluation.registry.specs import _family


def synthetic_panel(n_geo: int = 40, first: int = 1989, last: int = 2025) -> Panel:
    """Deterministic toy panel: a quarter of the countries alternate peace and conflict spells."""
    p = Panel()
    for i in range(n_geo):
        g = f"gw:{100 + i}"
        p.deaths[g] = {}
        p.iso3[g] = {}
        for y in range(first, last + 1):
            phase = (y + 3 * i) % 12
            v = 0.0
            if i % 4 == 0 and phase < 5:
                v = float(30 * (phase + 1) * (1 + i % 3))
            elif i % 4 == 1 and phase == 0:
                v = 5.0
            if i == 0 and phase in (2, 3):
                v = 1500.0
            p.deaths[g][y] = v
            p.iso3[g][y] = ["FRA", "DEU", "SDN", "COL", "NOR", "KEN", "MLI", "NGA"][i % 8] if i < 8 else None
        p.acd_years[g] = {y for y in range(1950, last + 1) if p.deaths[g].get(y, 0) >= 25}
    p.borders = [(f"gw:{100 + i}", f"gw:{101 + i}", 1946, 2019) for i in range(n_geo - 1)]
    p.cov["polyarchy"] = {("FRA", y): 0.8 for y in range(first, last + 1)}
    return p


def test_family_docs_parse_with_the_registry_validator() -> None:
    for fam in F.FAMILIES:
        doc = F.family_doc(fam, ["SDN", "COL"])
        f = _family(doc, {})
        assert f.dataset_key == "atlas.ucdp.orgvio_cy" and f.indicator == "sb_total_deaths_best"
        if fam.kind == "binary":
            assert f.threshold == str(fam.threshold)
    assert F.BY_KEY["ucdp.sb_conflict25_cy"].event(25.0) == 1
    assert F.BY_KEY["ucdp.sb_conflict25_cy"].event(24.0) == 0
    assert F.BY_KEY["ucdp.sb_war1000_cy"].event(1000.0) == 1
    assert F.BY_KEY["ucdp.sb_onset25_cy"].applies(0) and not F.BY_KEY["ucdp.sb_onset25_cy"].applies(25)
    assert F.BY_KEY["ucdp.sb_escalation1000_cy"].applies(999) and not F.BY_KEY["ucdp.sb_escalation1000_cy"].applies(
        1000
    )
    # verbatim UCDP quotes travel with the definition, with the risk note
    d = F.BY_KEY["ucdp.sb_war1000_cy"].definition()
    assert F.Q_WAR in d and F.Q_STATE_BASED in d and "not a prediction of any specific attack" in d


def test_features_use_only_the_past() -> None:
    p = synthetic_panel()
    x = M.features(p, "gw:100", 2010)
    assert x is not None
    p2 = synthetic_panel()
    for y in range(2011, 2026):
        p2.deaths["gw:100"][y] = 99999.0
        p2.deaths["gw:101"][y] = 99999.0
    assert M.features(p2, "gw:100", 2010) == x
    assert M.features(p, "gw:100", 2030) is None
    assert x["poly"] == 0.8  # gw:100 carries ISO3 FRA in the toy panel: V-Dem read by ISO3
    xd = M.features(p, "gw:101", 2010)
    assert xd is not None and xd["poly"] is None and xd["dpoly"] is None  # no covariate: None, never 0


def test_logit_and_hurdle_are_sane() -> None:
    import numpy as np

    rng = np.random.default_rng(0)
    x = rng.normal(size=(400, 2))
    y = (x[:, 0] + 0.3 * rng.normal(size=400) > 0).astype(float)
    b = M.fit_logit(x, y)
    assert b[1] > 1.0 and abs(b[2]) < 0.5
    s = M.hurdle_sample(0.3, math.log(100), 0.5)
    assert len(s) == M.N_QUANTILES and s == tuple(sorted(s))
    assert sum(1 for v in s if v == 0) == 70
    assert M.prob_above([0, 0, 10, 30], 24) == pytest.approx(2 / 6)


def test_dm_and_holm() -> None:
    d = [-1.0, -0.5, -0.8, -1.2, 0.1, -0.9] * 5
    cl = [f"c{i % 6}" for i in range(30)]
    r = dm_test(d, cl)
    assert r["mean_diff"] < 0 and r["p_one_sided"] < 0.05 and r["n_clusters"] == 6
    assert dm_test([0.0], ["a"])["z"] is None
    adj = holm({"a": 0.01, "b": 0.04, "c": 0.03, "d": None})
    assert adj["a"] == pytest.approx(0.03) and adj["c"] == pytest.approx(0.06) and adj["b"] == pytest.approx(0.06)
    assert adj["d"] is None


def test_backtest_runs_as_of_and_declares_rule() -> None:
    p = synthetic_panel()
    res = run_backtest(p, horizons=(1,), first_origin=2015)
    keys = {(f["family"], f["horizon"]) for f in res["families"]}
    assert keys == {(f.key, 1) for f in F.FAMILIES}
    conf = next(f for f in res["families"] if f["family"] == "ucdp.sb_conflict25_cy")
    pers = next(r for r in conf["results"] if r["model"] == "forecast.conflict_persistence")
    assert pers["n_scored"] == 40 * (2025 - 2015)
    for r in conf["results"]:
        assert 0 <= r["brier"] <= 1
    assert all(t["p_holm"] is None or t["p_holm"] >= (t["p_one_sided"] or 0) for t in res["dm_tests"].values())
    assert res["decision_rule"]["alpha"] == 0.05
    assert "never real-time skill" in res["label"]


def _views_frame() -> pl.DataFrame:
    rows = []
    for run, rel in (("fatalities002_2024_12_t01", "2025-01-27"), ("fatalities003_2025_12_t01", "2026-01-30")):
        y0 = int(run.split("_")[1])
        for m in range(1, 13):
            rows.append(
                {
                    "indicator": f"{run}|main_mean",
                    "provider_geo": "gw:100",
                    "period_start": date(y0 + 1, m, 1),
                    "value": "10.5",
                    "flags": json.dumps({"projection_upstream": {"release_date": rel}}),
                    "raw_sha256": "0" * 64,
                    "selector": "{}",
                }
            )
        rows.append({**rows[-1], "indicator": f"{run}|main_dich", "value": "0.3"})
    return pl.DataFrame(rows)


def test_views_annual_sums_and_run_choice() -> None:
    runs = runs_from_frame(_views_frame())
    assert set(runs) == {"fatalities002_2024_12_t01", "fatalities003_2025_12_t01"}
    vr = runs["fatalities002_2024_12_t01"]
    assert vr.annual("gw:100", 2025) == pytest.approx(126.0)
    assert vr.annual("gw:100", 2026) is None
    assert annual_table(vr) == {("gw:100", 2025): pytest.approx(126.0)}
    assert december_runs(runs)[2024].run == "fatalities002_2024_12_t01"
    assert latest_covering(runs, 2026, "2026-10-17").run == "fatalities003_2025_12_t01"  # type: ignore[union-attr]
    assert latest_covering(runs, 2026, "2026-01-01") is None  # released after the issue date: not usable


def test_issue_build_freezes_registry_compatible_records() -> None:
    p = synthetic_panel()
    vr = ViewsRun("fatalities003_2025_12_t01", "2026-01-30", (2025, 12), "main_mean")
    for m in range(1, 13):
        vr.monthly[("gw:102", 2026, m)] = 3.0
        vr.refs[("gw:102", 2026, m)] = {"raw_sha256": "0" * 64, "selector": "{}"}
    out = build_questions_and_forecasts(
        p,
        {vr.run: vr},
        cycle="2026-Q4",
        issued_on=date(2026, 10, 17),
        source_pin={"dataset_key": "atlas.ucdp.orgvio_cy"},
        winners={("ucdp.sb_conflict25_cy", 1)},
        already=set(),
    )
    qs = out["questions"]
    assert qs and all(q["geo"] in {"FRA", "DEU", "SDN", "COL", "NOR", "KEN", "MLI", "NGA"} for q in qs)
    assert {q["reference_period"] for q in qs} == {"2026", "2027"}
    assert any(s["reason"].startswith("no ISO3") for s in out["skipped"])
    by_model: dict[str, int] = {}
    for f in out["forecasts"]:
        by_model[f["model"]] = by_model.get(f["model"], 0) + (f["status"] == "issued")
    assert by_model["forecast.conflict_kg_logit"] > 0  # only on the declared winner pair
    kg = [f for f in out["forecasts"] if f["model"] == "forecast.conflict_kg_logit"]
    kg_q = {f["question_id"] for f in kg}
    assert all(
        q["family"] == "ucdp.sb_conflict25_cy" and q["reference_period"] == "2026"
        for q in qs
        if q["question_id"] in kg_q
    )
    views = [f for f in out["forecasts"] if f["model"] == "forecast.reference_views" and f["status"] == "issued"]
    assert len(views) == 1 and views[0]["point"] == "36"
    # every frozen forecast scores with the registry scorer
    qmap = {q["question_id"]: q for q in qs}
    for f in out["forecasts"]:
        if f["status"] != "issued":
            continue
        s = score_forecast(qmap[f["question_id"]], f, "30")
        if qmap[f["question_id"]]["type"] == "binary":
            assert s["brier"] is not None
            assert s["outcome_binary"] == (1 if qmap[f["question_id"]]["threshold"] == "24" else 0)
        else:
            assert s["abs_error"] is not None
    # onset questions only for countries below 25 in the last year
    for q in qs:
        if q["family"] == "ucdp.sb_onset25_cy":
            assert float(q["last_observed"]["value"]) < 25
        assert "not a prediction of any specific attack" in q["risk_note"]


def test_country_month_benchmark_reads_absent_months_as_zero_and_scores_views() -> None:
    from reva.evaluation.conflict import monthly as MO

    rows = [
        {"indicator": MO.INDICATOR, "provider_geo": "gw:625", "period_start": date(y, m, 1), "value": "40"}
        for y in range(2015, 2026)
        for m in range(1, 13)
        if m % 2 == 0
    ]
    frame = pl.DataFrame(rows)
    universe = {y: {"gw:625", "gw:385"} for y in range(2010, 2027)}
    p = MO.build_monthly(frame, universe, (MO.midx(2010, 1), MO.midx(2025, 12)))
    assert p.y("gw:385", MO.midx(2020, 5)) == 0.0  # in the universe, no GED event: 0 under GED's definition
    assert p.y("gw:625", MO.midx(2020, 4)) == 40.0
    assert p.y("gw:625", MO.midx(2026, 1)) is None  # outside the vintage's coverage
    vr = ViewsRun("fatalities002_2024_12_t01", "2025-01-27", (2024, 12), "main_mean")
    for m in range(1, 13):
        for g in ("gw:625", "gw:385"):
            vr.monthly[(g, 2025, m)] = 20.0 if g == "gw:625" else 0.1
            vr.prob[(g, 2025, m)] = 0.5 if g == "gw:625" else 0.0
    res = MO.evaluate_runs({vr.run: vr}, p, p, label="t")
    v = next(s for s in res["summary"] if s["model"] == "forecast.reference_views" and s["steps"] == "1-3")
    assert v["n"] == 6 and v["event_rate"] == pytest.approx(1 / 6)
    assert res["runs"][0]["n"] == 24
    assert any(t["metric"] == "brier" for t in res["tests"].values())
