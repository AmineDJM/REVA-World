"""Unit tests of ``demography.cohort_component@1.0.0`` (pure numerics and plugin), offline.

Real data: the recorded Eurostat fixtures for Luxembourg and Liechtenstein (since 2008).
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from functools import cache
from typing import Any

import pytest

from reva.adapters.connectors import eurostat_demography as ed
from reva.evidence.runner import FixtureHttp
from reva.kernel.errors import CapabilityGap, ValidationProblem
from reva.models.population import cohort as C
from reva.models.population.cohort_component import (
    DATASET_CONCEPTS,
    MODEL,
    OUTPUT_KEYS,
    RANGE_VARIANT,
    SINGLE_AGE_TOKEN,
)
from reva.simulation import plugins
from reva.simulation.plugins import ResolvedInputs
from reva.simulation.spec import validate_scenario_spec

DS = {"population": "demo_pjan", "deaths": "demo_magec", "births": "demo_fasec",
      "europop_migration": "proj_23nanmig", "europop_fertility": "proj_23naasfr"}


@cache
def fixture_tables() -> dict[str, dict[str, Any]]:
    c = ed.EurostatDemographyConnector(countries=("LI", "LU"), since_year=2008)
    http = FixtureHttp(ed.NAME)

    async def go() -> dict[str, list[Any]]:
        out: dict[str, list[Any]] = {}
        for i in c.all_items():
            group = f"{i.params['dataset']}:{','.join(i.params['geos'])}"
            out.setdefault(group, []).append(await c.fetch(i, http))
        return out

    raws = asyncio.run(go())
    return {group: ed.merged_tables(group.split(":")[0], parts) for group, parts in raws.items()}


def table(name: str, iso3: str) -> Any:
    ds = DS[name]
    key = f"{ds}:LI,LU" if ds.startswith("proj") else f"{ds}:{ed.ISO3_TO_GEO[iso3]}"
    return fixture_tables()[key].get(iso3)


def country(iso3: str) -> C.CountryDemography:
    return C.CountryDemography.from_tables(
        iso3, population=table("population", iso3), deaths=table("deaths", iso3), births=table("births", iso3),
        europop_migration=table("europop_migration", iso3), europop_fertility=table("europop_fertility", iso3))


# --------------------------------------------------------------------------- numerics
def test_m_to_q_conversion_and_life_table_identities() -> None:
    m = [Decimal("0.004"), Decimal("0.0003"), Decimal("0.01"), Decimal("0.3")]
    lt = C.life_table(m, "F")
    a0 = Decimal("0.053") + Decimal("2.800") * Decimal("0.004")
    assert lt.a[0] == a0
    assert lt.q[0] == C.div(m[0], C._CTX.add(1, C.mul(1 - a0, m[0])))
    assert lt.q[1] == C.div(m[1], 1 + Decimal("0.5") * m[1])
    assert lt.q[-1] == 1  # open group
    assert lt.Lx[-1] == C.div(lt.lx[-1], m[-1])
    assert lt.s_birth == lt.Lx[0]
    assert lt.s_open == C.div(lt.Lx[3], C._CTX.add(lt.Lx[2], lt.Lx[3]))
    assert all(0 < s < 1 for s in (*lt.s, lt.s_open, lt.s_birth))
    assert C.coale_demeny_a0(Decimal("0.2"), "M") == Decimal("0.330")


def test_open_group_never_split_and_omega_from_data() -> None:
    lu = country("LUX")
    starts = {C.open_start(lu.population.get((y, s))) for y in range(2008, 2014) for s in C.SEXES}
    starts |= {C.open_start(lu.deaths.get((y, s))) for y in range(2008, 2013) for s in C.SEXES}
    rates = C.base_rates(lu, 2008, 2012)
    assert None not in starts and rates.omega <= min(starts)  # type: ignore[type-var]
    # summing single ages above omega into the open group is a sum of disjoint leaves
    cells = lu.population[(2010, "F")]
    vec = C.to_vector(cells, rates.omega)
    assert vec is not None
    assert C.to_vector(cells, (C.open_start(cells) or 0) + 1) is None  # would need splitting: refused


def test_residual_migration_reconstructs_observed_population_exactly() -> None:
    lu = country("LUX")
    rates = C.base_rates(lu, 2008, 2012)
    rec = C.reconstruct(lu, rates, 2008, 2012)
    for y, by_sex in rec.items():
        for s, vec in by_sex.items():
            assert vec == C._vec(lu, "population", y, s, rates.omega), (y, s)
    assert any(x < 0 for t in rates.residual_migration.values() for v in t.values() for x in v)  # negatives kept


def test_projection_conservation_every_year_and_outputs() -> None:
    lu = country("LUX")
    a = C.Assumptions(migration="average_residual", base_period=(2019, 2023))
    p = C.project(lu, jump_off=2024, end_year=2034, assumptions=a)
    for t in range(2024, 2034):
        f = p.flows[t]
        for s in C.SEXES:
            lhs = C.xsum(p.population[t + 1][s])
            rhs = C.xadd(C.xsub(C.xsum(p.population[t][s]), f.deaths[s]), f.births[s], f.net_migration[s])
            assert lhs == rhs
    vals = C.output_values(p)
    assert {k for k, _ in vals} == set(OUTPUT_KEYS)
    y = 2030
    assert vals[("population.total.all_ages", y)] == C.xadd(vals[("population.female.all_ages", y)],
                                                           vals[("population.male.all_ages", y)])
    r = vals[("old_age_dependency_ratio_65plus_per_15_64", y)]
    assert r == C.div(100 * vals[("population.total.age_65_plus", y)], vals[("population.total.age_15_64", y)])
    r67 = vals[("old_age_dependency_ratio_67plus_per_20_66", y)]
    assert r67 == C.div(100 * vals[("population.total.age_67_plus", y)], vals[("population.total.age_20_66", y)])
    table_ = p.single_age_table()
    assert table_.num_rows == 11 * 2 * (p.omega + 1)


def test_no_information_after_jump_off() -> None:
    lu = country("LUX")
    with pytest.raises(ValidationProblem):
        C.project(lu, jump_off=2013, end_year=2023, assumptions=C.Assumptions(base_period=(2009, 2013)))
    with pytest.raises(ValidationProblem):
        C.project(lu, jump_off=2013, end_year=2023,
                  assumptions=C.Assumptions(base_period=(2008, 2012), migration_window=(2010, 2013)))


def test_backtest_reports_metrics_without_tuning() -> None:
    bt = C.backtest(country("LUX"), jump_off=2013, target_year=2023, base_period=(2008, 2012))
    assert set(bt["variants"]) == {"mig=average_residual;fert=constant_base_period_rates",
                                   "mig=zero;fert=constant_base_period_rates"}
    for v in bt["variants"].values():
        assert set(v["groups"]) == set(C.COMPARISON_GROUPS)
        assert Decimal(v["total_ape"]) >= 0
    assert Decimal(bt["naive_no_change_total_ape"]) > 0


def test_small_country_lowers_open_age_and_reports_it() -> None:
    li = country("LIE")
    rates = C.base_rates(li, 2019, 2023)
    assert C.MIN_OPEN_AGE <= rates.omega < 100
    assert any("open age lowered" in n for n in rates.notes)


def test_unknown_assumption_refused() -> None:
    with pytest.raises(ValidationProblem):
        C.Assumptions(migration="guess", base_period=(2019, 2023))


# --------------------------------------------------------------------------- plugin
def spec(**over: Any) -> dict[str, Any]:
    doc = {
        "schema_version": "2.0.0", "title": "Population projection (test)",
        "baseline_release_id": "00000000-0000-4000-8000-000000000001",
        "knowledge_cutoff": "2026-10-01T00:00:00Z", "model_key": "demography.cohort_component",
        "model_version": "1.0.0", "countries": ["LUX", "LIE"], "reference_year": 2025,
        "horizon": {"periods": 10, "unit": "calendar_year"}, "intervention": {"kind": "baseline_projection"},
        "assumptions": {"mortality": "constant_base_period_rates", "fertility": "constant_base_period_rates",
                        "migration": "average_residual", "base_period": {"first_year": 2019, "last_year": 2023}},
        "fidelity": "L1", "missing_input_policy": "fail",
        "outputs": ["population.total.all_ages", "old_age_dependency_ratio_65plus_per_15_64", "births.total"],
        "seed": 0,
    }
    doc.update(over)
    return doc


def resolved(doc: dict[str, Any], drop: tuple[tuple[str, str], ...] = ()) -> ResolvedInputs:
    reqs = MODEL.requirements(doc)
    data = {}
    for r in reqs:
        t = None if (r.name, r.subject) in drop else table(r.name, r.subject)
        data[(r.name, r.subject)] = (t, {"dataset_id": f"fixture:{r.name}:{r.subject}"}) if t is not None else None
    return ResolvedInputs.from_values(reqs, datasets=data)


def test_plugin_is_registered_and_spec_validates() -> None:
    assert plugins.get("demography.cohort_component") is MODEL
    s = validate_scenario_spec(spec(), allow_fixture_baseline=True)
    assert s.as_json()["model_key"] == "demography.cohort_component"
    reqs = MODEL.requirements(spec())
    assert {r.concept for r in reqs} == {DATASET_CONCEPTS[n] for n in ("population", "deaths", "births")}
    assert all(r.kind == "dataset" for r in reqs)
    m = MODEL.manifest()
    assert m["review_status"] == "maintainer_review_pending" and "not a forecast" in m["label"]


@pytest.mark.parametrize("over, err", [
    ({"assumptions": {**spec()["assumptions"], "base_period": {"first_year": 2021, "last_year": 2025}}},
     ValidationProblem),
    ({"assumptions": {**spec()["assumptions"], "sensitivity": {"migration": ["zero"],
                                                               "fertility": ["constant_base_period_rates"]}}},
     ValidationProblem),
    ({"fidelity": "L2"}, ValidationProblem),
    ({"horizon": {"periods": 40, "unit": "calendar_year"},
      "assumptions": {**spec()["assumptions"], "migration": "europop2023_bsl"}}, CapabilityGap),
])
def test_plugin_validation_refusals(over: dict[str, Any], err: type[Exception]) -> None:
    with pytest.raises(err):
        MODEL.validate(spec(**over))


def test_unknown_outputs_and_countries_refused_by_schema_or_capability() -> None:
    with pytest.raises((ValidationProblem, CapabilityGap)):
        validate_scenario_spec(spec(outputs=["gdp_loss"]), allow_fixture_baseline=True)
    with pytest.raises((ValidationProblem, CapabilityGap)):
        validate_scenario_spec(spec(countries=["USA"]), allow_fixture_baseline=True)


def test_l1_rows_have_periods_labels_and_rounded_values() -> None:
    doc = spec()
    comp = MODEL.compile(resolved(doc), doc)
    assert comp.missing == [] and [c.status for c in comp.countries] == ["ready", "ready"]
    rows = MODEL.compute(comp, "L1")
    assert {r.variant for r in rows} == {"L1"}
    pop = [r for r in rows if r.output_key == "population.total.all_ages" and r.iso3 == "LUX"]
    assert [r.period[0].year for r in pop] == list(range(2025, 2036))
    assert all((r.period[1] - r.period[0]).days == 1 for r in pop)  # 1 January instants
    births = [r for r in rows if r.output_key == "births.total" and r.iso3 == "LUX"]
    assert [r.period[0].year for r in births] == list(range(2025, 2035))
    assert all(r.period[1].year == r.period[0].year + 1 for r in births)
    assert all(r.applicability["label"] == C.LABEL for r in rows)
    assert all(r.uncertainty["status"] == "unquantified" for r in rows)
    assert all("." not in r.value["value"] for r in pop)
    app = MODEL.run_applicability(comp, "L1")
    assert app["countries"]["LUX"]["open_age"] >= C.MIN_OPEN_AGE


def test_l2_grid_range_contains_l1_and_europop_gap_for_liechtenstein() -> None:
    doc = spec(fidelity="L2", assumptions={
        **spec()["assumptions"],
        "sensitivity": {"migration": ["zero", "average_residual", "europop2023_bsl"],
                        "fertility": ["constant_base_period_rates", "europop2023_bsl"]}})
    comp = MODEL.compile(resolved(doc), doc)
    assert [c.status for c in comp.countries] == ["ready", "ready"]
    lie = next(c for c in comp.countries if c.iso3 == "LIE")
    assert {u["variant"] for u in lie.unavailable_variants} == {
        "mig=europop2023_bsl;fert=constant_base_period_rates", "mig=europop2023_bsl;fert=europop2023_bsl",
        "mig=zero;fert=europop2023_bsl", "mig=average_residual;fert=europop2023_bsl"}
    rows = MODEL.compute(comp, "L2")
    lux_variants = {r.variant for r in rows if r.iso3 == "LUX"}
    assert len(lux_variants) == 1 + 5 + 1  # L1, 5 other grid points, range
    by = {(r.iso3, r.output_key, r.variant, r.period): r for r in rows}
    for (iso3, key, variant, period), r in by.items():
        if variant != RANGE_VARIANT:
            continue
        assert r.uncertainty["kind"] == "sensitivity_range" and "level" not in str(r.uncertainty.get("level", ""))
        l1 = Decimal(by[(iso3, key, "L1", period)].value["value"])
        assert Decimal(r.value["lower"]) <= l1 <= Decimal(r.value["upper"])
    lie_range = next(r for r in rows if r.iso3 == "LIE" and r.variant == RANGE_VARIANT)
    assert lie_range.value["evaluated_points"] == 2  # L1 + zero migration only


def test_missing_dataset_is_missing_input_or_skipped_never_zero() -> None:
    doc = spec()
    comp = MODEL.compile(resolved(doc, drop=(("deaths", "LIE"),)), doc)
    assert [p["input"] for p in comp.missing] == ["deaths"] and comp.missing[0]["country"] == "LIE"
    doc2 = spec(missing_input_policy="skip_missing_countries")
    comp2 = MODEL.compile(resolved(doc2, drop=(("deaths", "LIE"),)), doc2)
    assert comp2.missing == [] and comp2.skipped == ["LIE"]
    rows = MODEL.compute(comp2, "L1")
    assert {r.iso3 for r in rows} == {"LUX"}


def test_single_age_output_is_opt_in() -> None:
    doc = spec(countries=["LUX"], horizon={"periods": 2, "unit": "calendar_year"},
               outputs=["population.total.all_ages", SINGLE_AGE_TOKEN])
    comp = MODEL.compile(resolved(doc), doc)
    rows = MODEL.compute(comp, "L1")
    omega = comp.countries[0].rates.omega
    single = [r for r in rows if ".age_" in r.output_key]
    assert len(single) == 3 * 2 * (omega + 1)
    assert any(r.output_key.endswith(f"age_{omega:03d}_plus") for r in single)
    total = sum(Decimal(r.value["value"]) for r in single if r.period[0].year == 2026)
    agg = next(r for r in rows if r.output_key == "population.total.all_ages" and r.period[0].year == 2026)
    assert abs(total - Decimal(agg.value["value"])) <= Decimal("0.5")
