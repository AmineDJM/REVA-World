"""V3 SIMWORLD: ``demography.cohort_component@1.1.0`` and ``social.retirement_age@1.1.0`` (pure, offline),
and the versioned plugin registry they rely on.

Data: WPP 2024 / ILOSTAT excerpts of the real bytes (SEN, JPN, BRA), the Eurostat fixtures of
LU/LI (for the 1.0.0 identity check) and the WBL 2026 rule tables.
"""

from __future__ import annotations

import copy
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fixtures" / "simworld"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import simworld_fixtures as SF

from reva.kernel.errors import CapabilityGap, ValidationProblem
from reva.models.population import cohort as C
from reva.models.population import cohort_component as CC
from reva.models.population import cohort_world as CW
from reva.models.population import wpp
from reva.models.social import retirement_age as ra
from reva.models.social import retirement_world as rw
from reva.models.social import rules as rl
from reva.simulation import plugins
from reva.simulation.plugins import ResolvedInputs
from reva.simulation.scenario_schema import build_schema
from reva.simulation.spec import validate_scenario_spec

RELEASE = "3f1c2b4a-5d6e-4f70-8a91-b2c3d4e5f607"


def cohort_spec(**over: Any) -> dict[str, Any]:
    doc = {
        "schema_version": "2.0.0",
        "title": "SIMWORLD cohort",
        "baseline_release_id": RELEASE,
        "knowledge_cutoff": "2026-10-15T00:00:00+00:00",
        "model_key": "demography.cohort_component",
        "model_version": "1.1.0",
        "countries": ["SEN", "JPN", "BRA"],
        "reference_year": 2023,
        "horizon": {"periods": 10, "unit": "calendar_year"},
        "intervention": {"kind": "baseline_projection"},
        "assumptions": {
            "data_source": "un_wpp2024",
            "mortality": "constant_base_period_rates",
            "fertility": "constant_base_period_rates",
            "migration": "average_residual",
            "base_period": {"first_year": 2018, "last_year": 2022},
        },
        "fidelity": "L1",
        "missing_input_policy": "fail",
        "outputs": ["population.total.all_ages", "old_age_dependency_ratio_65plus_per_15_64", "net_migration.total"],
        "seed": 0,
    }
    doc.update(over)
    return doc


def wpp_inputs(model: Any, spec: dict[str, Any], drop: tuple[tuple[str, str], ...] = ()) -> ResolvedInputs:
    reqs = model.requirements(spec)
    ds = {}
    for r in reqs:
        if (r.name, r.subject) in drop or r.subject not in SF.COUNTRIES:
            continue
        ds[(r.name, r.subject)] = (
            SF.wpp_table(SF.WPP_INPUTS[r.name], r.subject),
            {"dataset_id": f"{r.name}:{r.subject}"},
        )
    return ResolvedInputs.from_values(reqs, datasets=ds)


def run_cohort(
    spec: dict[str, Any], fidelity: str = "L1", drop: tuple[tuple[str, str], ...] = ()
) -> tuple[Any, list[Any]]:
    m = plugins.get("demography.cohort_component", "1.1.0")
    m.validate(spec)
    compiled = m.compile(wpp_inputs(m, spec, drop), spec)
    return compiled, m.compute(compiled, fidelity)


# --------------------------------------------------------------------------- platform
def test_registry_keeps_1_0_0_as_default_and_serves_1_1_0_by_version() -> None:
    plugins.load_all()
    for key, old in (("demography.cohort_component", CC.MODEL), ("social.retirement_age", ra.MODEL)):
        assert plugins.get(key) is old and plugins.REGISTRY[key] is old and old.model_version == "1.0.0"
        assert plugins.get(key, "1.0.0") is old
        assert plugins.get(key, "1.1.0").model_version == "1.1.0"
        assert set(plugins.versions_of(key)) == {"1.0.0", "1.1.0"}
        with pytest.raises(CapabilityGap):
            plugins.get(key, "9.9.9")
    schema = build_schema()
    assert schema["$defs"]["demography.cohort_component"]["properties"]["model_version"] == {"const": "1.0.0"}
    assert schema["$defs"]["demography.cohort_component@1.1.0"]["properties"]["model_version"] == {"const": "1.1.0"}


def test_spec_validation_routes_by_version() -> None:
    s = validate_scenario_spec(cohort_spec())
    assert s.as_json()["model_version"] == "1.1.0"
    old = cohort_spec(model_version="1.0.0", countries=["LUX"])
    old["assumptions"] = {k: v for k, v in old["assumptions"].items() if k != "data_source"}
    old["reference_year"] = 2025
    old["assumptions"]["base_period"] = {"first_year": 2019, "last_year": 2023}
    assert validate_scenario_spec(old).as_json()["model_version"] == "1.0.0"
    bad = cohort_spec(model_version="1.0.0")  # SEN is not an Eurostat country, data_source unknown to 1.0.0
    with pytest.raises((ValidationProblem, CapabilityGap)):
        validate_scenario_spec(bad)
    with pytest.raises(ValidationProblem):  # data_source is required in 1.1.0
        a = cohort_spec()
        del a["assumptions"]["data_source"]
        validate_scenario_spec(a)


# --------------------------------------------------------------------------- cohort 1.1.0
def test_wpp_projection_rows_are_labelled_and_exact() -> None:
    compiled, rows = run_cohort(cohort_spec())
    assert {p.status for p in compiled.countries} == {"ready"}
    assert {r.iso3 for r in rows} == set(SF.COUNTRIES)
    r0 = rows[0]
    assert r0.applicability["model"] == "demography.cohort_component@1.1.0"
    assert r0.applicability["data_source"] == "un_wpp2024" and "model-based" in r0.applicability["data_label"]
    assert "consistency with WPP, not truth" in r0.uncertainty["description"]
    jump = [r for r in rows if r.output_key == "population.total.all_ages" and r.period[0].year == 2023]
    for r in jump:  # the jump-off row is the WPP 1 January 2023 total, exactly
        t = SF.wpp_table("wpp2024_pop_jan1", r.iso3).to_pylist()
        obs = sum(Decimal(x["value"]) for x in t if x["year"] == 2023)
        assert Decimal(r.value["value"]) == C._CTX.quantize(obs, Decimal(1))


def test_eurostat_path_of_1_1_0_equals_1_0_0_exactly() -> None:
    import test_population_cohort as TPC

    spec10 = {
        **cohort_spec(model_version="1.0.0", countries=["LUX", "LIE"], reference_year=2025),
        "assumptions": {
            "mortality": "constant_base_period_rates",
            "fertility": "constant_base_period_rates",
            "migration": "average_residual",
            "base_period": {"first_year": 2019, "last_year": 2023},
        },
    }
    spec11 = copy.deepcopy(spec10)
    spec11["model_version"] = "1.1.0"
    spec11["assumptions"]["data_source"] = "eurostat_demography"
    out = []
    for spec, model in ((spec10, CC.MODEL), (spec11, plugins.get("demography.cohort_component", "1.1.0"))):
        reqs = model.requirements(spec)
        ds = {
            (r.name, r.subject): (TPC.table(r.name, r.subject), {})
            for r in reqs
            if TPC.table(r.name, r.subject) is not None
        }
        compiled = model.compile(ResolvedInputs.from_values(reqs, datasets=ds), spec)
        out.append(model.compute(compiled, "L1"))
    a, b = out
    assert [(r.output_key, r.iso3, r.variant, r.period, r.value) for r in a] == [
        (r.output_key, r.iso3, r.variant, r.period, r.value) for r in b
    ]
    assert b[0].applicability["model"].endswith("@1.1.0") and a[0].applicability["model"].endswith("@1.0.0")


def test_wpp_refusals_are_typed() -> None:
    with pytest.raises(CapabilityGap, match="EUROPOP2023"):
        CW.parse_spec(cohort_spec(assumptions={**cohort_spec()["assumptions"], "migration": "europop2023_bsl"}))
    with pytest.raises(CapabilityGap, match="1 January 2023"):
        CW.parse_spec(cohort_spec(reference_year=2025))
    with pytest.raises(CapabilityGap):
        CW.parse_spec(cohort_spec(countries=["SEN", "ZZZ"]))
    ms, src = CW.parse_spec(cohort_spec(countries=["ALL"]))
    assert ms.countries == ("ALL",) and src == "un_wpp2024"
    probe = plugins.get("demography.cohort_component", "1.1.0").requirements(cohort_spec(countries=["ALL"]))
    assert {r.subject for r in probe} == {"ALL"} and {r.concept for r in probe} == set(wpp.CONCEPTS.values())
    compiled, rows = run_cohort(cohort_spec(missing_input_policy="skip_missing_countries"), drop=(("deaths", "JPN"),))
    jpn = next(p for p in compiled.countries if p.iso3 == "JPN")
    assert jpn.status == "missing_input" and jpn.problems[0]["concept"] == "demography.wpp.deaths.age_sex"
    assert "JPN" not in {r.iso3 for r in rows} and compiled.missing == []


def test_wpp_backtest_and_migration_diagnostic() -> None:
    d = wpp.country_demography(
        "SEN",
        population=SF.wpp_table("wpp2024_pop_jan1", "SEN"),
        deaths=SF.wpp_table("wpp2024_deaths", "SEN"),
        births=SF.wpp_table("wpp2024_births", "SEN"),
    )
    bt = C.backtest(d, jump_off=2013, target_year=2023, base_period=(2008, 2012))
    assert bt["open_age"] == 100 and set(bt["variants"]) == {
        "mig=average_residual;fert=constant_base_period_rates",
        "mig=zero;fert=constant_base_period_rates",
    }
    rates = C.base_rates(d, 2018, 2022)
    chk = wpp.migration_check(rates, range(2018, 2023), SF.wpp_table("wpp2024_indicators", "SEN"))
    assert chk is not None and set(chk["years"]) == {str(y) for y in range(2018, 2023)}
    # WPP data carry no status flags, so no series-break year can be excluded
    assert rates.residual_breaks == {}


# --------------------------------------------------------------------------- retirement 1.1.0
def ret_spec(**over: Any) -> dict[str, Any]:
    doc = {
        "schema_version": "2.0.0",
        "title": "SIMWORLD retirement",
        "baseline_release_id": RELEASE,
        "knowledge_cutoff": "2026-10-15T00:00:00+00:00",
        "model_key": "social.retirement_age",
        "model_version": "1.1.0",
        "countries": ["SEN", "JPN", "BRA"],
        "reference_year": 2022,
        "horizon": {"periods": 8, "unit": "calendar_year"},
        "intervention": {
            "kind": "retirement_age_target",
            "reference_path": "wbl2026_held_constant",
            "target_age_months": 744,
            "first_birth_year": 1965,
            "phase_in_months_per_cohort": 0,
        },
        "assumptions": {"employment_response": {"central": "rabate_rochut_2020_fra"}},
        "fidelity": "L1",
        "missing_input_policy": "fail",
        "outputs": sorted(rw.OUTPUTS),
        "seed": 0,
    }
    doc.update(over)
    return doc


def run_ret(spec: dict[str, Any], fidelity: str = "L1", drop: tuple[str, ...] = ()) -> tuple[Any, list[Any]]:
    m = plugins.get("social.retirement_age", "1.1.0")
    m.validate(spec)
    reqs = m.requirements(spec)
    ds = {}
    for r in reqs:
        if r.subject in drop:
            continue
        t = (
            SF.ilo_table(r.name, r.subject)
            if r.name in ("employment_rate", "activity_rate")
            else SF.wpp_table(SF.WPP_INPUTS[r.name], r.subject)
        )
        if t is not None:
            ds[(r.name, r.subject)] = (t, {})
    compiled = m.compile(ResolvedInputs.from_values(reqs, datasets=ds), spec)
    return compiled, m.compute(compiled, fidelity)


def test_wbl_tables_are_separate_cited_and_1_0_0_is_unchanged() -> None:
    assert rl.available_countries() == ("DEU", "ESP", "FRA", "NLD")  # the 1.0.0 law-text tables only
    wbl = rl.wbl_countries()
    assert len(wbl) == 163 and not set(wbl) & {"DEU", "ESP", "FRA", "NLD"}
    t = rl.load_wbl_rule_table("SEN")
    assert t.transcription == "transcribed by agent SIMWORLD from WBL 2026, maintainer review pending"
    assert t.review_status == "maintainer_review_pending" and t.sources["wbl2026"]["kind"] == "secondary_legal_dataset"
    for r in t.rows:
        assert rl.normalise_text(r.citation["locator"]) in rl.source_text(
            "WBL2026_pension_full_benefit_ages_excerpt.txt"
        )
        assert r.derivation == "verbatim" or (r.derivation == "counterfactual_extension" and r.note)
    assert {p.status for p in t.paths.values()} == {"in_force", "counterfactual"}
    with pytest.raises(CapabilityGap) as e:  # 66.8 years: the month is not in WBL
        rl.load_wbl_rule_table("USA")
    assert "not a whole number of months" in e.value.details["reason"]
    with pytest.raises(CapabilityGap):
        rl.load_rule_table("SEN")  # 1.0.0 still has no table for SEN
    assert set(ra.MODEL.manifest()["rule_tables"]) == {"DEU", "ESP", "FRA", "NLD"}


def test_retirement_world_rows_and_signs() -> None:
    compiled, rows = run_ret(ret_spec())
    assert {p.iso3: p.status for p in compiled.countries} == {"SEN": "ready", "JPN": "ready", "BRA": "ready"}
    by = {(r.iso3, r.output_key, r.period[0].year): Decimal(r.value["value"]) for r in rows}
    # SEN 60 -> 62 for cohorts from 1965: more employment, fewer pension recipients in 2027-2030
    assert by[("SEN", "employment_change_55_64", 2029)] > 0 > by[("SEN", "pension_recipients_change_55_64", 2029)]
    # JPN 65 -> 62: the reverse
    assert by[("JPN", "employment_change_55_64", 2029)] < 0 < by[("JPN", "pension_recipients_change_55_64", 2029)]
    for iso in SF.COUNTRIES:
        for t in range(2023, 2031):
            assert by[(iso, "employment_change_55_64", t)] == ra.CTX.quantize(
                by[(iso, "employed_persons_55_64", t)] - by[(iso, "employed_persons_55_64_reference", t)],
                Decimal("0.001"),
            ) or abs(
                by[(iso, "employment_change_55_64", t)]
                - (by[(iso, "employed_persons_55_64", t)] - by[(iso, "employed_persons_55_64_reference", t)])
            ) <= Decimal("0.002")
    sen = next(r for r in rows if r.iso3 == "SEN")
    assert sen.applicability["model"] == "social.retirement_age@1.1.0" and sen.applicability["status"] == "qualified"
    assert any("transferred from FRA" in c for c in sen.applicability["caveats"])
    assert "QUALIFIED (SEN)" in sen.value["basis"]["notes"]


def test_retirement_world_l2_range_and_refusals() -> None:
    _, rows = run_ret(ret_spec(countries=["SEN"], fidelity="L2"), fidelity="L2")
    rng = [r for r in rows if r.variant == "L2:sensitivity_range" and r.output_key == "employment_change_55_64"]
    pts = [
        r
        for r in rows
        if r.variant.startswith("L2:")
        and r.variant != "L2:sensitivity_range"
        and r.output_key == "employment_change_55_64"
    ]
    for r in rng:
        vals = [Decimal(p.value["value"]) for p in pts if p.period == r.period]
        assert Decimal(r.value["lower"]) == min(vals) and Decimal(r.value["upper"]) == max(vals)
    assert any("pension_drop=Y55-59" in p.variant for p in pts) and any(
        p.variant.endswith("reference_no_employment_response") for p in pts
    )
    with pytest.raises(CapabilityGap) as e:  # a 65+ eligibility change: ILOSTAT has no 65-69 band
        plugins.get("social.retirement_age", "1.1.0").validate(
            ret_spec(countries=["JPN"], intervention={**ret_spec()["intervention"], "target_age_months": 804})
        )
    assert e.value.details["capability"] == "social.retirement_age.age_range"
    with pytest.raises(CapabilityGap):
        plugins.get("social.retirement_age", "1.1.0").validate(ret_spec(outputs=["pension_recipients"]))
    with pytest.raises(CapabilityGap):
        plugins.get("social.retirement_age", "1.1.0").validate(ret_spec(countries=["USA"]))
    with pytest.raises(CapabilityGap, match="WPP 2024"):
        plugins.get("social.retirement_age", "1.1.0").validate(ret_spec(reference_year=2023))
    compiled, rows = run_ret(
        ret_spec(countries=["SEN", "USA", "BRA"], missing_input_policy="skip_missing_countries"), drop=("BRA",)
    )
    st = {p.iso3: p.status for p in compiled.countries}
    assert st == {"SEN": "ready", "USA": "unsupported", "BRA": "missing_input"} and {r.iso3 for r in rows} == {"SEN"}


def test_rates_year_falls_back_and_stale_rates_are_refused() -> None:
    emp = rw.ilo_rates(SF.ilo_table("employment_rate", "SEN").to_pylist())
    act = rw.ilo_rates(SF.ilo_table("activity_rate", "SEN").to_pylist())
    y = rw.rates_year(emp, act, 2022)
    assert y is not None and y <= 2022
    assert rw.rates_year(emp, act, min(emp) - 1) is None
    assert rw.rates_year({2000: emp[y]}, {2000: act[y]}, 2022) is None  # older than MAX_RATES_AGE


def test_oversized_runs_are_refused_with_a_typed_gap() -> None:
    from uuid import uuid4

    from reva.simulation.plugins import ResultRow

    row = ResultRow(
        "x", "SEN", uuid4(), "L1", {"kind": "decimal", "value": "1", "notes": "n" * 2000}, {"status": "unknown"}, {}
    )
    CW.result_size_guard([row] * 10, "m")  # small: fine
    with pytest.raises(CapabilityGap) as e:
        CW.result_size_guard([row] * 20_000, "m")
    assert e.value.details["capability"] == "simulation.result_size"
