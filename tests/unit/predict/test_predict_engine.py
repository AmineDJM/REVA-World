"""Decision engine (V5, PRED-ENGINE): propagation, Monte Carlo and honesty gates on SYNTHETIC estimates whose
analytic answer is known. Nothing here is a real-world effect size: every estimate is invented for the test
(source key ``test.synthetic``) and never loaded anywhere.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from reva.predict import estimates as es
from reva.predict.context import (
    NoBaseline,
    NoClaims,
    PluginData,
    StaticBaselineSource,
    StaticClaimSource,
    StaticEffectSource,
)
from reva.predict.decision import Decision, direct_changes, quarter_labels, reference_decisions
from reva.predict.engine import LABEL_UNVALIDATED, Engine, chain_signature, predict

N = 20000


def decision(**over: Any) -> Decision:
    doc: dict[str, Any] = {
        "id": "test_rate",
        "label_fr": "Décision synthétique",
        "actor": {"label_fr": "Banque centrale fictive"},
        "instrument": "policy_rate_change",
        "target": {"label_fr": "Taux directeur"},
        "magnitude": {"value": "50", "unit": "bp"},
        "start": "2027Q1",
        "duration_quarters": None,
        "horizon_quarters": 6,
        "countries": ["FRA"],
    }
    doc.update(over)
    return Decision.model_validate(doc)


def est(key: str, cause: tuple[str, str, str], effect: tuple[str, str, str], **over: Any) -> dict[str, Any]:
    e: dict[str, Any] = {
        "estimate_key": f"pub:test.synthetic:{key}",
        "cause": {"kind": cause[0], "id": cause[1], "transform": cause[2], "definition": "synthetic cause"},
        "effect": {"kind": effect[0], "id": effect[1], "transform": effect[2], "definition": "synthetic effect"},
        "measure": "slope",
        "unit": "pp_per_pp",
        "shock": None,
        "sign": "negative",
        "point": -0.5,
        "point_kind": "point",
        "uncertainty": {"se": 0.1, "status": "reported"},
        "horizon": {"kind": "long_run", "value": None, "unit": None},
        "irf": [],
        "scope": {"countries": ["FRA"], "period": {"from": "1999", "to": "2019"}},
        "method": "local_projection",
        "source": {"kind": "publication", "source_key": "test.synthetic", "citation": {"text": "Synthetic (test)"}},
        "as_of": "2020-01-01",
        "wording_fr": "Selon une source synthétique, … ; REVA n'établit pas cet effet.",
    }
    e.update(over)
    return e


RATE = ("atlas_concept", "imf.policy_rate", "pp_change")
INFL = ("atlas_concept", "imf.inflation_cpi_avg", "pp_change")
UNEMP = ("atlas_concept", "imf.unemployment_rate", "pp_change")


def run(d: Decision, ests: list[dict[str, Any]], **kw: Any) -> dict[str, Any]:
    return predict(
        d,
        StaticEffectSource(ests),
        kw.pop("claims", NoClaims()),
        kw.pop("baseline", NoBaseline()),
        kw.pop("data", None),
        draws=kw.pop("draws", N),
        **kw,
    )


def var(r: dict[str, Any], vid: str) -> dict[str, Any]:
    hits = [v for v in r["variables"] if v["id"] == vid]
    assert hits, [v["id"] for v in r["variables"]]
    return hits[0]


# ----------------------------------------------------------------------------------------- decisions
def test_reference_library_has_the_five_decisions_and_exact_unit_factors() -> None:
    refs = reference_decisions()
    assert sorted(refs) == [
        "ecb_plus_50bp_2027q1",
        "eu_ban_russian_oil_2027q1",
        "france_retirement_65_2027",
        "india_rice_export_ban_2027q1",
        "opec_plus_cut_2mbd_2027q1",
    ]
    opec = direct_changes(refs["opec_plus_cut_2mbd_2027q1"])
    assert len(opec) == 1 and opec[0].geo == "WLD" and str(opec[0].change) == "-2000"  # 2 mb/d = 2000 kb/d
    ecb = direct_changes(refs["ecb_plus_50bp_2027q1"])
    assert [c.geo for c in ecb] == ["DEU", "FRA", "ITA", "ESP", "NLD"]
    assert all(c.change == pytest.approx(0.5) and c.unit == "percentage_points" for c in ecb)  # 50 bp = 0.5 pp
    ret = direct_changes(refs["france_retirement_65_2027"])
    assert ret[0].change == 780 and ret[0].transform == "level"  # 65 years = 780 months, a level
    ban = direct_changes(refs["eu_ban_russian_oil_2027q1"])
    assert ban[0].variable.key == "event_type:sanction_episode" and ban[0].geo == "RUS"
    for d in refs.values():
        assert d.horizon_quarters <= 12 and d.start == "2027Q1"


def test_decision_schema_rejects_bad_input() -> None:
    with pytest.raises(ValueError):
        decision(start="2027-01")
    with pytest.raises(ValueError):
        decision(countries=["fr"])
    with pytest.raises(Exception, match="magnitude"):
        direct_changes(decision(magnitude={"value": "50", "unit": "furlongs"}))
    assert quarter_labels("2027Q3", 4) == ["2027Q3", "2027Q4", "2028Q1", "2028Q2"]


# ----------------------------------------------------------------------------------------- analytic propagation
def test_static_hop_matches_the_normal_distribution_of_the_estimate() -> None:
    """Δinflation = β·Δrate, β ~ N(-0.5, 0.1²), Δrate = +0.5 pp ⇒ N(-0.25, 0.05²) (undated, long-run)."""
    r = run(decision(), [est("a", RATE, INFL)])
    v = var(r, "atlas_concept:imf.inflation_cpi_avg@FRA")
    u = v["undated"]
    assert v["frequency"] == "undated" and u["kind"] == "long_run"
    assert u["p50"] == pytest.approx(-0.25, abs=0.002)
    assert u["p05"] == pytest.approx(-0.25 - 1.6449 * 0.05, abs=0.003)
    assert u["p95"] == pytest.approx(-0.25 + 1.6449 * 0.05, abs=0.003)
    assert v["uncertainty_complete"] and not u["degenerate"]
    assert r["label"] == LABEL_UNVALIDATED and r["validation"]["status"] == "unvalidated"
    hop = next(h for h in r["hops"] if h["kind"] == "estimate")
    assert hop["estimate_key"] == "pub:test.synthetic:a" and hop["source"]["source_key"] == "test.synthetic"


def test_step_response_superposition_with_finite_duration() -> None:
    """Step response R(h) = h + 1 (quarters), decision Δ = 0.5 pp held 2 quarters:
    y_t = 0.5·(R(t) − R(t−2)) = 0.5·(t+1) for t < 2 and 0.5·2 = 1.0 for t ≥ 2 (no uncertainty: exact)."""
    irf = [{"h": h, "unit": "quarter", "point": float(h + 1)} for h in range(6)]
    e = est("irf", RATE, INFL, irf=irf, uncertainty={"status": "not_reported"}, sign="positive", point=1.0)
    r = run(decision(duration_quarters=2), [e], draws=200)
    v = var(r, "atlas_concept:imf.inflation_cpi_avg@FRA")
    assert [p["p50"] for p in v["periods"]] == pytest.approx([0.5, 1.0, 1.0, 1.0, 1.0, 1.0])
    assert all(p["degenerate"] for p in v["periods"])
    assert not v["uncertainty_complete"] and v["point_estimates"] == ["pub:test.synthetic:irf"]


def test_missing_lag_leaves_the_period_missing_never_interpolated() -> None:
    irf = [{"h": 0, "unit": "quarter", "point": 1.0}, {"h": 2, "unit": "quarter", "point": 3.0}]
    e = est("gap", RATE, INFL, irf=irf, uncertainty={"status": "not_reported"}, point=1.0, sign="positive")
    v = var(run(decision(horizon_quarters=4), [e], draws=200), "atlas_concept:imf.inflation_cpi_avg@FRA")
    assert v["periods"][0]["p50"] == pytest.approx(0.5)
    assert v["periods"][1]["missing_reason"].startswith("lag_not_published")
    assert v["periods"][2]["p50"] == pytest.approx(1.5)
    assert v["periods"][3]["missing_reason"].startswith("lag_not_published")


def test_two_hop_chain_composes_linearly() -> None:
    """rate →(β1 = 2, step, constant) inflation →(β2 = −0.25, long-run) unemployment: Δu = 0.5·2·(−0.25)."""
    e1 = est(
        "c1",
        RATE,
        INFL,
        point=2.0,
        sign="positive",
        uncertainty={"status": "not_reported"},
        irf=[{"h": h, "unit": "quarter", "point": 2.0} for h in range(6)],
    )
    e2 = est("c2", INFL, UNEMP, point=-0.25, uncertainty={"status": "not_reported"})
    r = run(decision(), [e1, e2], draws=200)
    v = var(r, "atlas_concept:imf.unemployment_rate@FRA")
    assert v["undated"]["p50"] == pytest.approx(-0.25) and v["depth"] == 2
    assert len(v["path"]) == 3  # decision, c1, c2
    assert r["chain"] == ["pub:test.synthetic:c1", "pub:test.synthetic:c2"]


def test_response_is_scaled_linearly_by_the_published_shock_size() -> None:
    """A response of −1.2 pp to a 100 bp shock gives −0.6 pp for 50 bp; the scaling is stated."""
    e = est(
        "resp",
        RATE,
        INFL,
        measure="response",
        unit="percentage_points",
        shock={"size": 100, "unit": "bp"},
        point=-1.2,
        uncertainty={"status": "not_reported"},
    )
    r = run(decision(), [e], draws=200)
    v = var(r, "atlas_concept:imf.inflation_cpi_avg@FRA")
    assert v["undated"]["p50"] == pytest.approx(-0.6)
    assert any("scaled linearly" in n for n in v["notes"])


# ----------------------------------------------------------------------------------------- honesty gates
def test_one_estimate_per_edge_never_summed() -> None:
    exact = est("exact", RATE, INFL, point=-0.5)
    world = est("world", RATE, INFL, point=-2.0, scope={"countries": ["WLD"]}, method="meta_analysis")
    r = run(decision(), [world, exact])
    v = var(r, "atlas_concept:imf.inflation_cpi_avg@FRA")
    assert v["undated"]["p50"] == pytest.approx(-0.25, abs=0.003)  # the exact-scope estimate only, not -1.25
    hop = next(h for h in r["hops"] if h["kind"] == "estimate")
    assert hop["estimate_key"] == "pub:test.synthetic:exact"
    assert [a["estimate_key"] for a in hop["alternatives"]] == ["pub:test.synthetic:world"]
    assert "never summed" in hop["choice_rule"]


def test_one_path_per_variable_shortest_wins_alternative_listed() -> None:
    direct = est("direct", RATE, UNEMP, point=0.2, sign="positive", uncertainty={"status": "not_reported"})
    a = est("a", RATE, INFL, point=-0.5, uncertainty={"status": "not_reported"})
    b = est("b", INFL, UNEMP, point=-1.0, uncertainty={"status": "not_reported"})
    r = run(decision(), [a, b, direct], draws=200)
    v = var(r, "atlas_concept:imf.unemployment_rate@FRA")
    assert v["undated"]["p50"] == pytest.approx(0.1)  # 0.5 × 0.2 via the direct edge, not + 0.25 via inflation
    assert r["alternative_paths"] and r["alternative_paths"][0]["estimate_key"] == "pub:test.synthetic:b"


def test_scope_and_unit_mismatches_are_excluded_with_reasons() -> None:
    other = est("deu", RATE, INFL, scope={"countries": ["DEU"]})
    wrong_unit = est(
        "unit", ("atlas_concept", "imf.policy_rate", "percent_change"), INFL, measure="elasticity", unit="ratio"
    )
    r = run(decision(), [other, wrong_unit])
    assert not [v for v in r["variables"] if v["id"].startswith("atlas_concept:imf.inflation")]
    codes = sorted(x["code"] for x in r["excluded_links"])
    assert codes == ["out_of_scope", "unit_mismatch"]


def test_sign_only_estimates_and_anchor_claims_stay_unquantified() -> None:
    sign_only = est("sign", RATE, INFL, point=None, uncertainty={"status": "not_reported"})
    claim = {
        "claim_key": "kg:test:1",
        "cause": {"id": "Q1", "label_fr": "Épisode A"},
        "effect": {"id": "Q2", "label_fr": "Épisode B"},
        "relation": "has_effect",
        "wording_fr": "Selon une source, A a eu pour effet B.",
        "source": {"source_key": "wikidata.test", "tier": "T3"},
        "grade": {"grade": "C"},
    }
    r = run(decision(anchors=["Q1"]), [sign_only], claims=StaticClaimSource([claim]))
    assert not [v for v in r["variables"] if "inflation" in v["id"]]
    kinds = sorted(u["kind"] for u in r["unquantified_links"])
    assert kinds == ["causal_claim", "sign_only_estimate"]
    assert all("point" not in u and "p50" not in u for u in r["unquantified_links"])


def test_validation_label_only_with_backtest_and_calibration_records() -> None:
    d = decision()
    e = est("v", RATE, INFL)
    r = run(d, [e], draws=200)
    sig = chain_signature(r["chain"])
    assert r["validation"]["chain_signature"] == sig
    half = {sig: {"signature": sig, "backtest_record": "bt-1"}}
    assert run(d, [e], draws=200, validated=half)["label"] == LABEL_UNVALIDATED
    full = {sig: {"signature": sig, "backtest_record": "bt-1", "calibration_record": "cal-1"}}
    assert run(d, [e], draws=200, validated=full)["validation"]["status"] == "validated"


def test_as_of_filter_excludes_estimates_not_yet_knowable() -> None:
    r = run(decision(), [est("late", RATE, INFL, as_of="2026-06-01")], as_of_max="2025-12-31")
    assert r["excluded_links"][0]["code"] == "not_knowable_at_as_of"
    assert not [v for v in r["variables"] if "inflation" in v["id"]]


def test_seeded_monte_carlo_is_reproducible_and_uses_common_random_numbers() -> None:
    e = est("seed", RATE, INFL)
    a = run(decision(), [e], draws=500, seed=7)
    b = run(decision(), [e], draws=500, seed=7)
    c = run(decision(), [e], draws=500, seed=8)
    va, vb, vc = (var(x, "atlas_concept:imf.inflation_cpi_avg@FRA")["undated"] for x in (a, b, c))
    assert va == vb and va != vc
    # same estimate, a decision twice as large: draws scale exactly (common random numbers)
    d2 = run(decision(magnitude={"value": "100", "unit": "bp"}), [e], draws=500, seed=7)
    v2 = var(d2, "atlas_concept:imf.inflation_cpi_avg@FRA")["undated"]
    assert v2["p05"] == pytest.approx(2 * va["p05"], rel=1e-5)


def test_baseline_level_only_when_units_compose() -> None:
    base = StaticBaselineSource({("atlas_concept:imf.inflation_cpi_avg", "FRA"): {"value": "2.0", "unit": "%"}})
    irf = [{"h": h, "unit": "quarter", "point": -0.5} for h in range(6)]
    e = est("b", RATE, INFL, irf=irf, uncertainty={"status": "not_reported"})
    v = var(run(decision(), [e], draws=200, baseline=base), "atlas_concept:imf.inflation_cpi_avg@FRA")
    assert v["periods"][0]["scenario_level"]["p50"] == pytest.approx(1.75)
    assert "persistence" in v["periods"][0]["scenario_level"]["note"]


def test_normal_approx_fallback_matches_the_interface_definition() -> None:
    e = {"point": 1.0, "uncertainty": {"ci_low": 0.0, "ci_high": 2.0, "ci_level": 0.9, "interval_kind": "confidence"}}
    na = es.normal_approx(e)
    assert na["basis"] == "ci" and na["sd"] == pytest.approx(1.0 / 1.6448536, rel=1e-6)
    assert es.normal_approx({"point": 1.0, "uncertainty": {}})["basis"] is None


# ----------------------------------------------------------------------------------------- plugins
def _io_arrays() -> Any:
    """Two economies × two industries (B, C). A and households chosen by hand."""
    from reva.models.economic.io_price import IOArrays

    a = np.array(
        [
            [0.0, 0.2, 0.0, 0.1],  # FRA:B supplies FRA:C and DEU:C
            [0.1, 0.1, 0.0, 0.05],  # FRA:C
            [0.0, 0.1, 0.0, 0.3],  # DEU:B
            [0.05, 0.05, 0.1, 0.1],  # DEU:C
        ]
    )
    x = np.array([100.0, 200.0, 100.0, 200.0])
    hh = np.array([[5.0, 1.0], [50.0, 10.0], [1.0, 5.0], [10.0, 60.0]])
    return IOArrays(("FR", "DE"), ("FRA", "DEU"), ("B", "C"), a, x, hh, reference_year=2024)


def test_io_plugin_matches_the_leontief_formula() -> None:
    """Shock: B prices +x % in both economies (x = oil-price change); Δp_C = (I − A_CCᵀ)⁻¹ A_BCᵀ π."""
    arr = _io_arrays()
    s, e = [0, 2], [1, 3]
    pi = np.array([0.01, 0.01])
    dp_c = np.linalg.solve(np.eye(2) - arr.A[np.ix_(e, e)].T, arr.A[np.ix_(s, e)].T @ pi)
    dp = np.zeros(4)
    dp[s], dp[e] = pi, dp_c
    fra = float(arr.households[:, 0] @ dp / arr.households[:, 0].sum()) * 100  # % per 1 % oil change
    oil = est(
        "oil",
        ("atlas_concept", "imf.policy_rate", "pp_change"),
        ("variable", "oil_price.global", "percent_change"),
        measure="semi_elasticity",
        unit="percent_per_pp",
        point=10.0,
        sign="positive",
        uncertainty={"status": "not_reported"},
        scope={"countries": ["WLD"]},
    )
    # policy rate +0.5 pp → oil +5 % (synthetic, global variable) → IO basket
    r = run(decision(), [oil], draws=200, data=PluginData(io_arrays=arr, io_provenance={"dataset_id": "syn"}))
    v = var(r, "plugin_output:economic.io_price.household_basket_price_change_percent@FRA")
    assert v["undated"]["p50"] == pytest.approx(5 * fra, rel=1e-5)
    hop = next(h for h in r["hops"] if h["kind"] == "plugin" and h["model"].startswith("economic.io_price"))
    assert "B" in hop["assumption_fr"] and any("not a consumer price index" in n for n in v["notes"])


def test_crude_exposure_plugin_is_the_accounting_identity() -> None:
    oil = est(
        "oil",
        RATE,
        ("variable", "oil_price.global", "percent_change"),
        measure="semi_elasticity",
        unit="percent_per_pp",
        point=10.0,
        sign="positive",
        uncertainty={"se": 2.0, "status": "reported"},
        scope={"countries": ["WLD"]},
    )
    data = PluginData(crude_inputs={"FRA": {"M": "50e9", "X": "1e9", "G": "3000e9", "year": 2024, "provenance": {}}})
    r = run(decision(), [oil], draws=N, data=data)
    v = var(r, "plugin_output:economic.crude_exposure.exposure_percent_baseline_gdp@FRA")
    # exposure = 100 · (M − X) · (x/100) / G with x ~ N(5, 1²): median 100·49e9·0.05/3000e9
    assert v["undated"]["p50"] == pytest.approx(100 * 49e9 * 0.05 / 3000e9, rel=0.01)
    gaps = run(decision(countries=["FRA", "DEU"]), [oil], draws=200, data=data)["gaps"]
    assert any(g["code"] == "missing_input" and g["geo"] == "DEU" for g in gaps)


def test_retirement_plugin_runs_once_and_reports_failures_as_gaps() -> None:
    d = reference_decisions()["france_retirement_65_2027"]
    seen: list[dict[str, Any]] = []

    def ok(spec: dict[str, Any]) -> dict[str, Any]:
        seen.append(spec)
        return {
            "status": "succeeded",
            "rows": [
                {"output_key": "employment_change_55_69", "variant": "L1", "value": {"value": "1000"}, "year": 2027},
                {"output_key": "employment_change_55_69", "variant": "L1", "value": {"value": "2500"}, "year": 2028},
                {"output_key": "employment_change_55_69", "variant": "L2:x", "value": {"value": "9"}, "year": 2028},
            ],
            "provenance": {"release_id": "rel"},
        }

    r = predict(d, StaticEffectSource([]), NoClaims(), NoBaseline(), PluginData(retirement_runner=ok), draws=200)
    assert seen[0]["intervention"]["target_age_months"] == 780 and seen[0]["intervention"]["first_birth_year"] == 1962
    v = var(r, "plugin_output:social.retirement_age.employment_change_55_69@FRA")
    assert [(p["period"], p["p50"]) for p in v["periods"]] == [("2027", 1000.0), ("2028", 2500.0)]
    # a target level is never fed to estimates of a *change*
    assert not [g for g in r["gaps"] if g["code"] == "no_effect_estimate_for_cause"]

    def refused(spec: dict[str, Any]) -> dict[str, Any]:
        return {"status": "missing_input", "problems": [{"input": "population"}]}

    r2 = predict(d, StaticEffectSource([]), NoClaims(), NoBaseline(), PluginData(retirement_runner=refused), draws=200)
    assert not r2["variables"] and r2["gaps"][0]["code"] == "missing_input"


def test_engine_rejects_unreasonable_draw_counts() -> None:
    with pytest.raises(ValueError):
        Engine(decision(), StaticEffectSource([]), NoClaims(), NoBaseline(), draws=10)


def test_annual_impact_response_to_a_user_stated_oil_price_assumption() -> None:
    """Shape of a published panel estimate (synthetic number): +0.4 pp inflation for +10 % oil in the year of the
    shock, no SE. A user-stated +10 % assumption gives +0.4 pp in the four quarters of year 0, nothing after."""
    e = est(
        "oil_cpi",
        ("variable", "oil_price.global", "percent_change"),
        INFL,
        measure="response",
        unit="percentage_points",
        shock={"size": 10, "unit": "percent"},
        point=0.4,
        sign="positive",
        uncertainty={"status": "not_reported"},
        horizon={"kind": "impact", "value": 0, "unit": "year"},
        scope={
            "country_group": "72 advanced and developing economies (synthetic)",
            "period": {"from": "1970", "to": "2015"},
        },
    )
    d = decision(
        instrument="oil_price_assumption", magnitude={"value": "10", "unit": "percent"}, countries=["FRA", "DEU"]
    )
    r = run(d, [e], draws=200)
    v = var(r, "atlas_concept:imf.inflation_cpi_avg@DEU")
    assert [p.get("p50") for p in v["periods"][:4]] == pytest.approx([0.4] * 4)
    assert all(p["missing_reason"] for p in v["periods"][4:])
    hop = next(h for h in r["hops"] if h["kind"] == "estimate")
    assert hop["applicability"]["status"] == "group_unverified"
    assert not v["uncertainty_complete"] and all(p["degenerate"] for p in v["periods"][:4])
    assert r["direct_changes"][0]["basis_fr"].startswith("Hypothèse posée par l'utilisateur")


def test_registry_baseline_path_is_used_only_for_its_exact_periods() -> None:
    base = StaticBaselineSource(
        {
            ("atlas_concept:imf.inflation_cpi_avg", "FRA"): {
                "value": "2.0",
                "unit": "%",
                "path": {"2027Q2": {"value": "3.0", "model": "forecast.registry_ar", "unit": "%"}},
            }
        }
    )
    irf = [{"h": h, "unit": "quarter", "point": -0.5} for h in range(6)]
    e = est("b2", RATE, INFL, irf=irf, uncertainty={"status": "not_reported"})
    v = var(run(decision(), [e], draws=200, baseline=base), "atlas_concept:imf.inflation_cpi_avg@FRA")
    assert v["periods"][0]["scenario_level"]["p50"] == pytest.approx(1.75)  # persistence 2.0 − 0.25
    assert v["periods"][1]["scenario_level"]["p50"] == pytest.approx(2.75)  # registry 3.0 − 0.25
    assert "registry forecast forecast.registry_ar" in v["periods"][1]["scenario_level"]["note"]
