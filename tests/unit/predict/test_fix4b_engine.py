"""FIX4B (audit AUDIT3 M3, M4, M5): level vs growth, declared variant preferences instead of an alphabetical
tie-break, pooled-panel applicability, onset-year horizon. SYNTHETIC estimates only (source ``test.synthetic``),
except the preference registry, which is read as shipped."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from reva.predict import estimates as es

from .test_predict_engine import INFL, RATE, decision, est, run, var

SANCTION = ("event_type", "sanction_episode", "occurrence")
GROWTH_CONCEPT = ("atlas_concept", "wb.ny.gdp.pcap.kd.zg", "percent_change")
LEVEL_VAR = ("variable", "output.real_gdp_per_capita", "percent_change")


def sanction_decision() -> Any:
    return decision(
        id="test_ban",
        instrument="import_ban",
        magnitude={"value": "1", "unit": "share_of_imports_banned"},
        countries=["RUS"],
        target={"label_fr": "Russie", "geo": "RUS"},
        horizon_quarters=12,
    )


def lp(key: str, effect: tuple[str, str, str], **over: Any) -> dict[str, Any]:
    base = {
        "measure": "response",
        "unit": "percent",
        "shock": {"size": 1, "unit": "occurrence"},
        "sign": "negative",
        "point": -0.4,
        "horizon": {"kind": "at_horizon", "value": 2, "unit": "year"},
        "irf": [
            {"h": 0, "unit": "year", "point": -0.2, "se": 0.1},
            {"h": 1, "unit": "year", "point": -0.3, "se": 0.1},
            {"h": 2, "unit": "year", "point": -0.4, "se": 0.1},
        ],
        "scope": {"countries": ["RUS", "IRN", "CUB"], "n_countries": 150, "period": {"from": "1961", "to": "2025"}},
        "effect": {
            "kind": effect[0],
            "id": effect[1],
            "transform": effect[2],
            "definition": "REVA LHS: cumulative sum of the outcome (growth rate, %) from t to t+h",
        },
    }
    base.update(over)
    e = est(key, SANCTION, effect)
    e.update(base)
    return e


def test_cumulative_level_response_on_a_growth_concept_is_refused_never_converted() -> None:
    r = run(sanction_decision(), [lp("bad", GROWTH_CONCEPT)], draws=200)
    assert not [v for v in r["variables"] if "gdp" in v["id"]]
    ex = [x for x in r["excluded_links"] if x["estimate_key"] == "pub:test.synthetic:bad"]
    assert ex and ex[0]["code"] == "not_propagatable"
    assert "effect_level_transform_on_growth_concept" in ex[0]["detail"]


def test_level_response_on_the_level_variable_propagates_with_onset_year_first() -> None:
    r = run(sanction_decision(), [lp("ok", LEVEL_VAR)], draws=200)
    v = var(r, "variable:output.real_gdp_per_capita@RUS")
    assert v["unit"] == "percent"
    by = {p["period"]: p for p in v["periods"]}
    assert by["2027Q1"]["p50"] == pytest.approx(-0.2, abs=0.02)  # h = 0 = onset year 2027
    assert by["2028Q1"]["p50"] == pytest.approx(-0.3, abs=0.02)


def test_growth_rate_on_a_level_variable_with_a_growth_variable_is_refused() -> None:
    e = lp("g", ("variable", "output.real_gdp_per_capita", "growth_rate"), unit="percentage_points")
    assert es.consistency_problem(e) == "effect_growth_rate_on_level_variable"
    ok = lp("g2", ("variable", "output.real_gdp_per_capita_growth", "growth_rate"), unit="percentage_points")
    ok["effect"]["definition"] = "growth rate of GDP per capita in the onset year"
    assert es.consistency_problem(ok) is None


def test_pooled_panel_listing_the_country_is_not_exact() -> None:
    st, notes = es.applicability({"countries": ["RUS", "IRN"], "n_countries": 158}, "RUS", 2027)
    assert st == "panel_member" and "not an estimate specific to RUS" in notes[0]
    st, notes = es.applicability({"country_group": "158 countries"}, "RUS", 2027)
    assert st == "group_unverified" and "RUS membership not checked" in notes[0]
    assert es.applicability({"countries": ["RUS"]}, "RUS", 2027)[0] == "exact"


def test_remaining_tie_is_reported_never_broken_alphabetically() -> None:
    a = est("a_variant", RATE, INFL, point=-0.5)
    b = est("b_variant", RATE, INFL, point=-0.1)
    r = run(decision(), [a, b], draws=200)
    assert not [v for v in r["variables"] if v["id"].startswith("atlas_concept:imf.inflation")]
    gap = next(g for g in r["gaps"] if g["code"] == "unresolved_tie")
    assert sorted(gap["estimate_keys"]) == ["pub:test.synthetic:a_variant", "pub:test.synthetic:b_variant"]


def test_declared_preference_picks_the_bias_corrected_median_and_names_it(tmp_path: Path) -> None:
    keys = {
        "output_peak_mean": -0.25,
        "output_peak_mean_bias_corrected": -0.14,
        "output_peak_median": -0.15,
        "output_peak_median_bias_corrected": -0.08,
    }
    ests = []
    for k, p in keys.items():
        e = est(
            k,
            RATE,
            ("variable", "output.real_gdp_level", "log_level"),
            point=p,
            measure="response",
            unit="percent",
            shock={"size": 0.25, "unit": "pp"},
            uncertainty={"status": "not_reported"},
            method="meta_analysis",
            horizon={"kind": "peak", "value": 2, "unit": "year"},
        )
        e["estimate_key"] = f"pub:effects.literature:bobasu_ciccarelli_notarpietro_2025:{k}"
        ests.append(e)
    r = run(decision(), ests, draws=200)
    hop = next(h for h in r["hops"] if h["kind"] == "estimate")
    assert hop["estimate_key"].endswith("output_peak_median_bias_corrected")
    assert hop["preference"]["variant_en"] == "publication-bias-corrected median"
    v = var(r, "variable:output.real_gdp_level@FRA")
    assert v["undated"]["p50"] == pytest.approx(-0.16)  # -0.08 per 25 bp, scaled to 50 bp
    assert any("variante retenue" in n for n in v["notes"])
    assert len(hop["alternatives"]) == 3


def test_preference_file_rules_are_declared() -> None:
    p = es.preference("pub:effects.literature:bobasu_ciccarelli_notarpietro_2025:inflation_peak_median")
    assert p and p["rule"] == "median_over_mean" and p["rule_fr"]
    assert es.preference("pub:test.synthetic:unknown") is None


def test_world_level_effects_are_computed_once_at_wld_never_copied_onto_countries() -> None:
    """Lead / EFFECTS-MORE finding: from a world node, a world-scope estimate gives a world value (WLD only);
    only a country-level scope (listed countries or a country group) reaches the countries of interest."""
    oil = ("variable", "oil_price.global", "percent_change")
    world_q = ("variable", "oil.world_supply_synthetic", "percent_change")  # not declared global: scope decides
    cpi = ("atlas_concept", "imf.inflation_cpi_avg", "pp_change")
    to_world = est(
        "w", oil, world_q, measure="elasticity", unit="ratio", sign="positive", point=0.1, scope={"countries": ["WLD"]}
    )
    to_cpi = est(
        "c",
        oil,
        cpi,
        measure="slope",
        unit="pp_per_percent",
        sign="positive",
        point=0.04,
        scope={"country_group": "72 economies"},
    )
    d = decision(
        instrument="oil_price_assumption", magnitude={"value": "10", "unit": "percent"}, countries=["FRA", "DEU"]
    )
    r = run(d, [to_world, to_cpi], draws=200)
    ids = {v["id"] for v in r["variables"]}
    assert "variable:oil.world_supply_synthetic@WLD" in ids
    assert not {i for i in ids if i.startswith("variable:oil.world_supply_synthetic@") and not i.endswith("@WLD")}
    assert {"atlas_concept:imf.inflation_cpi_avg@FRA", "atlas_concept:imf.inflation_cpi_avg@DEU"} <= ids
    assert not es.world_scope({"countries": ["WLD"], "country_group": "advanced economies"})
