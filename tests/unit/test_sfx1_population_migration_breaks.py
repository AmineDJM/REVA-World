"""Regression tests (agent SFX1) for the scientific review of ``demography.cohort_component``.

* H1 - census corrections (Eurostat series breaks, flag ``b``) must not become permanent migration:
  break years are excluded from the residual-migration average by default, with a typed warning
  listing them and their residuals; a window left with too few years is refused (missing_input).
* M8 - averaging residual migration over fewer than two years is refused unless explicitly
  allowed; one-year base periods get a typed warning.
* M1 - the back-test reports a constant-growth (and linear-trend) benchmark next to no-change.
* L3 - the back-test's open age comes from the base period / jump-off, not from the target year.

Offline: the recorded Eurostat fixtures for Luxembourg (``b`` flag on 1 January 2012 and 2017)
and Liechtenstein.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from functools import cache
from typing import Any

import pytest

from reva.adapters.connectors import eurostat_demography as ed
from reva.evidence.runner import FixtureHttp
from reva.kernel.errors import MissingInput, ValidationProblem
from reva.models.population import cohort as C
from reva.models.population.cohort_component import MODEL
from reva.simulation.plugins import ResolvedInputs

DS = {
    "population": "demo_pjan",
    "deaths": "demo_magec",
    "births": "demo_fasec",
    "europop_migration": "proj_23nanmig",
    "europop_fertility": "proj_23naasfr",
}


@cache
def _tables() -> dict[str, dict[str, Any]]:
    c = ed.EurostatDemographyConnector(countries=("LI", "LU"), since_year=2008, window_years=ed.WINDOW_YEARS)
    http = FixtureHttp(ed.NAME)

    async def go() -> dict[str, list[Any]]:
        out: dict[str, list[Any]] = {}
        for i in c.all_items():
            out.setdefault(f"{i.params['dataset']}:{','.join(i.params['geos'])}", []).append(await c.fetch(i, http))
        return out

    return {g: ed.merged_tables(g.split(":")[0], parts) for g, parts in asyncio.run(go()).items()}


def table(name: str, iso3: str) -> Any:
    ds = DS[name]
    key = f"{ds}:LI,LU" if ds.startswith("proj") else f"{ds}:{ed.ISO3_TO_GEO[iso3]}"
    return _tables()[key].get(iso3)


def country(iso3: str) -> C.CountryDemography:
    return C.CountryDemography.from_tables(
        iso3,
        population=table("population", iso3),
        deaths=table("deaths", iso3),
        births=table("births", iso3),
        europop_migration=table("europop_migration", iso3),
        europop_fertility=table("europop_fertility", iso3),
    )


def mean_residual(rates: C.BaseRates, years: list[int]) -> dict[str, list[Decimal]]:
    n = Decimal(len(years))
    return {
        s: [C.qp(C.div(C.xsum(rates.residual_migration[y][s][x] for y in years), n)) for x in range(rates.omega + 1)]
        for s in C.SEXES
    }


# --------------------------------------------------------------------------- H1
def test_break_flag_on_the_following_january_marks_the_residual_year() -> None:
    lu = country("LUX")
    rates = C.base_rates(lu, 2008, 2012)
    assert "b" in lu.population[(2012, "F")]["Y_OPEN"].status or any(
        "b" in c.status for (y, _s), cells in lu.population.items() if y == 2012 for c in cells.values()
    )
    assert rates.residual_breaks == {2011: ["b"]}  # 1 Jan 2012 is flagged -> the 2011 residual spans the break


def test_series_break_years_are_excluded_by_default_with_a_typed_warning() -> None:
    lu = country("LUX")
    p = C.project(lu, jump_off=2013, end_year=2015, assumptions=C.Assumptions(base_period=(2008, 2012)))
    assert p.migration_window is not None
    assert p.migration_window.years_used == (2008, 2009, 2010, 2012)
    assert set(p.migration_window.excluded) == {2011}
    w = [x for x in p.warnings if x["code"] == "migration_residual_contains_break"]
    assert len(w) == 1 and w[0]["years"] == [2011] and w[0]["action"] == "excluded from the average"
    assert w[0]["residuals"]["2011"]["residual_persons"] == "11191"
    assert w[0]["residuals"]["2011"]["flags"] == ["b"]
    # the projected migration is the mean over the unflagged years only
    expected = mean_residual(p.rates, [2008, 2009, 2010, 2012])
    for s in C.SEXES:
        assert p.flows[2013].migration_by_age[s] == tuple(expected[s])
    assert p.summary()["warnings"] == list(p.warnings)
    assert p.summary()["migration_window"]["excluded"]["2011"]["reason"] == "series_break"


def test_including_break_years_requires_an_explicit_choice_and_is_still_warned() -> None:
    lu = country("LUX")
    a = C.Assumptions(base_period=(2008, 2012), migration_breaks="include")
    assert a.variant == "mig=average_residual+breaks_included;fert=constant_base_period_rates"
    p = C.project(lu, jump_off=2013, end_year=2014, assumptions=a)
    assert p.migration_window is not None and p.migration_window.years_used == (2008, 2009, 2010, 2011, 2012)
    w = next(x for x in p.warnings if x["code"] == "migration_residual_contains_break")
    assert w["action"].startswith("included")
    with pytest.raises(ValidationProblem):
        C.Assumptions(base_period=(2008, 2012), migration_breaks="ignore")


def test_window_left_too_short_after_excluding_breaks_is_missing_input() -> None:
    lu = country("LUX")
    a = C.Assumptions(base_period=(2008, 2012), migration_window=(2011, 2012))
    with pytest.raises(MissingInput) as e:
        C.project(lu, jump_off=2013, end_year=2014, assumptions=a)
    assert e.value.code == "missing_input"
    assert e.value.details["missing"][0]["excluded_years"] == [2011]
    # explicitly allowed: one year, with a typed warning
    a1 = C.Assumptions(base_period=(2008, 2012), migration_window=(2011, 2012), allow_single_year_migration=True)
    p = C.project(lu, jump_off=2013, end_year=2014, assumptions=a1)
    assert p.migration_window is not None and p.migration_window.years_used == (2012,)
    assert {x["code"] for x in p.warnings} >= {"migration_residual_contains_break", "migration_window_single_year"}


def test_unflagged_outlier_is_warned_but_kept() -> None:
    """A large unflagged revision (synthetic: 1 Jan 2021 lowered by 10 %) is only a diagnostic."""
    lu = country("LUX")
    pop = {k: dict(v) for k, v in lu.population.items()}
    for s in C.SEXES:
        for code, c in pop[(2021, s)].items():
            if c.value is not None and c.kind in ("single", "open"):
                pop[(2021, s)][code] = C.Cell(c.value * Decimal("0.9"), c.kind, c.start, c.end, c.cell_type, c.status)
    shocked = C.CountryDemography(lu.iso3, pop, lu.deaths, lu.births)
    p = C.project(shocked, jump_off=2024, end_year=2025, assumptions=C.Assumptions(base_period=(2017, 2023)))
    w = [x for x in p.warnings if x["code"] == "migration_residual_outlier"]
    assert w and 2020 in w[0]["years"] and "kept" in w[0]["action"]
    assert p.migration_window is not None and 2020 in p.migration_window.years_used


# --------------------------------------------------------------------------- M8
def test_single_year_migration_window_is_refused_unless_explicit() -> None:
    lu = country("LUX")
    with pytest.raises(ValidationProblem, match="fewer than 2"):
        C.project(lu, jump_off=2024, end_year=2025, assumptions=C.Assumptions(base_period=(2023, 2023)))
    p = C.project(
        lu,
        jump_off=2024,
        end_year=2025,
        assumptions=C.Assumptions(base_period=(2023, 2023), allow_single_year_migration=True),
    )
    codes = {x["code"] for x in p.warnings}
    assert {"base_period_single_year", "migration_window_single_year"} <= codes
    one = next(x for x in p.warnings if x["code"] == "base_period_single_year")
    assert one["components"]["mortality"] == [2023]
    # zero migration with a one-year base period: allowed, but still warned
    pz = C.project(
        lu, jump_off=2024, end_year=2025, assumptions=C.Assumptions(migration="zero", base_period=(2023, 2023))
    )
    assert [x["code"] for x in pz.warnings] == ["base_period_single_year"]


# --------------------------------------------------------------------------- plugin
def _spec(**over: Any) -> dict[str, Any]:
    doc = {
        "schema_version": "2.0.0",
        "title": "Population projection (SFX1 test)",
        "baseline_release_id": "00000000-0000-4000-8000-000000000001",
        "knowledge_cutoff": "2026-10-01T00:00:00Z",
        "model_key": "demography.cohort_component",
        "model_version": "1.0.0",
        "countries": ["LUX"],
        "reference_year": 2013,
        "horizon": {"periods": 2, "unit": "calendar_year"},
        "intervention": {"kind": "baseline_projection"},
        "assumptions": {
            "mortality": "constant_base_period_rates",
            "fertility": "constant_base_period_rates",
            "migration": "average_residual",
            "base_period": {"first_year": 2008, "last_year": 2012},
        },
        "fidelity": "L1",
        "missing_input_policy": "fail",
        "outputs": ["population.total.all_ages"],
        "seed": 0,
    }
    doc.update(over)
    return doc


def _resolved(doc: dict[str, Any]) -> ResolvedInputs:
    reqs = MODEL.requirements(doc)
    data = {
        (r.name, r.subject): (table(r.name, r.subject), {"dataset_id": f"fixture:{r.name}:{r.subject}"}) for r in reqs
    }
    return ResolvedInputs.from_values(reqs, datasets=data)


def test_plugin_reports_break_warning_in_applicability_and_rows() -> None:
    doc = _spec()
    comp = MODEL.compile(_resolved(doc), doc)
    assert comp.countries[0].status == "ready"
    app = MODEL.run_applicability(comp, "L1")["countries"]["LUX"]
    assert app["migration_window"]["years_used"] == [2008, 2009, 2010, 2012]
    assert [w["code"] for w in app["warnings"]] == ["migration_residual_contains_break"]
    rows = MODEL.compute(comp, "L1")
    assert rows and all(r.applicability["warnings"][0]["years"] == [2011] for r in rows)
    assert all(r.applicability["migration_years_used"] == [2008, 2009, 2010, 2012] for r in rows)


def test_plugin_window_too_short_is_missing_input_and_single_year_needs_opt_in() -> None:
    a = _spec()["assumptions"]
    doc = _spec(assumptions={**a, "migration_window": {"first_year": 2011, "last_year": 2012}})
    comp = MODEL.compile(_resolved(doc), doc)
    assert comp.countries[0].status == "missing_input" and comp.missing[0]["excluded_years"] == [2011]
    with pytest.raises(ValidationProblem):
        MODEL.validate(_spec(assumptions={**a, "base_period": {"first_year": 2012, "last_year": 2012}}))
    ok = _spec(
        assumptions={**a, "base_period": {"first_year": 2012, "last_year": 2012}, "allow_single_year_migration": True}
    )
    MODEL.validate(ok)
    comp = MODEL.compile(_resolved(ok), ok)
    codes = [w["code"] for w in comp.countries[0].warnings]
    assert "migration_window_single_year" in codes and "base_period_single_year" in codes
    schema = MODEL.spec_schema()["assumptions"]["properties"]
    assert schema["migration_breaks"] == {"enum": ["exclude", "include"]}
    assert schema["allow_single_year_migration"] == {"type": "boolean"}


# --------------------------------------------------------------------------- M1 and L3 (back-test)
def test_backtest_reports_constant_growth_and_linear_benchmarks() -> None:
    lu = country("LUX")
    bt = C.backtest(lu, jump_off=2013, target_year=2023, base_period=(2008, 2012))
    omega = bt["open_age"]
    tot = {y: sum(C.xsum(C._vec(lu, "population", y, s, omega)) for s in C.SEXES) for y in (2008, 2013)}
    obs = Decimal(bt["benchmarks"]["observed_total"])
    growth = tot[2013] * (tot[2013] / tot[2008]) ** 2  # five years of growth compounded over ten
    linear = tot[2013] + (tot[2013] - tot[2008]) * 2
    assert abs(Decimal(bt["benchmarks"]["constant_growth"]["predicted_total"]) - growth) <= 1
    assert abs(Decimal(bt["benchmarks"]["linear_trend"]["predicted_total"]) - linear) <= 1
    assert abs(Decimal(bt["constant_growth_total_ape"]) - abs(growth - obs) / obs * 100) < Decimal("0.001")
    assert abs(Decimal(bt["linear_trend_total_ape"]) - abs(linear - obs) / obs * 100) < Decimal("0.001")
    assert Decimal(bt["naive_no_change_total_ape"]) > 0
    v = bt["variants"]["mig=average_residual;fert=constant_base_period_rates"]
    assert v["migration_window"]["years_used"] == [2008, 2009, 2010, 2012]
    with_breaks = C.backtest(lu, jump_off=2013, target_year=2023, base_period=(2008, 2012), include_break_variant=True)
    assert "mig=average_residual+breaks_included;fert=constant_base_period_rates" in with_breaks["variants"]


def test_backtest_open_age_comes_from_the_jump_off_not_the_target() -> None:
    """Target 2023 with its open group lowered by five years (synthetic): the projection keeps the open
    age of the base period / jump-off and only the scoring aggregates the projection above the target's
    open age (no split, no target information in the projection)."""
    lu = country("LUX")
    omega = C.base_rates(lu, 2008, 2012).omega
    low = omega - 5
    pop = {k: dict(v) for k, v in lu.population.items()}
    for s in C.SEXES:
        cells = pop[(2023, s)]
        top = [code for code, c in cells.items() if c.kind == "single" and c.start is not None and c.start >= low]
        total = C.xsum([cells[code].value for code in top] + [cells["Y_OPEN"].value])  # type: ignore[list-item]
        for code in top:
            del cells[code]
        o = cells["Y_OPEN"]
        cells["Y_OPEN"] = C.Cell(total, o.kind, o.start, o.end, o.cell_type, o.status)
    lowered = C.CountryDemography(lu.iso3, pop, lu.deaths, lu.births)
    bt = C.backtest(lowered, jump_off=2013, target_year=2023, base_period=(2008, 2012))
    ref = C.backtest(lu, jump_off=2013, target_year=2023, base_period=(2008, 2012))
    assert bt["open_age"] == ref["open_age"] == omega
    assert bt["scoring_open_age"] == low and ref["scoring_open_age"] == omega
    key = "mig=average_residual;fert=constant_base_period_rates"
    # same projection, same totals: the target's open age changes only how old ages are scored
    assert bt["variants"][key]["total_ape"] == ref["variants"][key]["total_ape"]
    assert (
        bt["variants"][key]["groups"]["age_65_plus"]["predicted"]
        == ref["variants"][key]["groups"]["age_65_plus"]["predicted"]
    )
