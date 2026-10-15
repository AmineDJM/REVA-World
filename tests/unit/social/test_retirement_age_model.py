"""social.retirement_age@1.0.0: pure plugin on captured fixtures (no DB, no network)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from reva.kernel.errors import CapabilityGap, ValidationProblem
from reva.models.social import assumptions as lit
from reva.models.social import retirement_age as ra
from reva.simulation import plugins

from .fixture_inputs import resolved_inputs


def spec(**kw: Any) -> dict[str, Any]:
    base = {"schema_version": "2.0.0", "title": "FRA 2023 reform as amended vs 2010 law",
            "model_key": ra.MODEL_KEY, "model_version": ra.MODEL_VERSION, "countries": ["FRA"],
            "reference_year": 2024, "horizon": {"periods": 16, "unit": "calendar_year"}, "fidelity": "L1",
            "intervention": {"kind": "retirement_age_rule_path", "scenario_path": "law_in_force_2026",
                             "reference_path": "law_2010_as_amended_2011"},
            "assumptions": {"observed_path": "law_in_force_2026"}, "missing_input_policy": "fail",
            "outputs": sorted(ra.OUTPUTS), "seed": 0}
    base.update(kw)
    return base


def run(s: dict[str, Any], fidelity: str = "L1", drop: tuple[tuple[str, str], ...] = ()) -> tuple[Any, list[Any]]:
    ra.MODEL.validate(s)
    c = ra.MODEL.compile(resolved_inputs(ra.MODEL.requirements(s), drop), s)
    if c.missing or c.incompatible:
        return c, []
    return c, ra.MODEL.compute(c, fidelity)


def by(rows: list[Any], output: str, variant: str) -> dict[int, Decimal]:
    return {r.period[0].year: Decimal(r.value["value"]) for r in rows if r.output_key == output and
            r.variant == variant}


@pytest.fixture(scope="module")
def l1() -> tuple[Any, list[Any]]:
    return run(spec())


@pytest.fixture(scope="module")
def l2() -> tuple[Any, list[Any]]:
    return run(spec(fidelity="L2"), "L2")


def test_plugin_is_registered_experimental_and_labelled() -> None:
    m = plugins.get(ra.MODEL_KEY)
    assert m is ra.MODEL and m.fidelities == frozenset({"L1", "L2"})
    man = m.manifest()
    assert man["review_status"] == "maintainer_review_pending" and man["implementation_digest"].startswith("sha256:")
    assert set(man["rule_tables"]) == {"DEU", "ESP", "FRA", "NLD"}


def test_l1_rows_cover_every_year_and_output(l1: tuple[Any, list[Any]]) -> None:
    c, rows = l1
    assert c.years == tuple(range(2025, 2041))
    assert len(rows) == len(c.years) * len(ra.OUTPUTS)
    r = rows[0]
    assert r.applicability["modality"] == "assumption_conditioned_scenario"
    assert r.applicability["label"] == ra.LABEL and r.applicability["model_label"] == ra.EXPERIMENTAL_LABEL
    assert r.applicability["grid_point"]["point_id"] == "rabate_rochut_2020_fra"
    assert "secondary_compilation" in r.applicability["grid_point"]["evidence"]
    assert all(r.period[1].year == r.period[0].year + 1 or r.output_key == "old_age_dependency_ratio" for r in rows)


def test_change_equals_difference_of_levels_and_signs(l1: tuple[Any, list[Any]]) -> None:
    _, rows = l1
    scn, ref = by(rows, "employed_persons_55_69", "L1"), by(rows, "employed_persons_55_69_reference", "L1")
    chg, rec = by(rows, "employment_change_55_69", "L1"), by(rows, "pension_recipients_change", "L1")
    exp = by(rows, "pension_expenditure_change_approx_eur", "L1")
    for y in scn:
        assert abs((scn[y] - ref[y]) - chg[y]) <= Decimal("0.002")  # rounding of three presented values
        assert chg[y] >= 0 and rec[y] <= 0 and exp[y] <= 0  # a higher minimum age never lowers employment
        assert chg[y] <= -rec[y]  # employment gain bounded by the pension-receipt drop (share s <= 1)
    assert rec[2035] < rec[2025] < 0  # the reform phases in


def test_group_rates_are_percentages_and_unaffected_groups_unchanged(l1: tuple[Any, list[Any]]) -> None:
    _, rows = l1
    for key in [k for k in ra.OUTPUTS if k.startswith("employment_rate.")]:
        vals = by(rows, key, "L1")
        assert all(Decimal(0) <= v <= Decimal(100) for v in vals.values())
    young = by(rows, "employment_rate.F.Y55-59", "L1")
    assert len(set(young.values())) == 1  # ages 55-59: no path differs from the observed regime


def test_l2_grid_range_contains_every_point_and_no_response_reference(l2: tuple[Any, list[Any]]) -> None:
    c, rows = l2
    pts = [p.point_id for p in c.countries[0].points]
    assert pts[0] == "rabate_rochut_2020_fra" and pts[-1] == lit.NO_RESPONSE_ID
    rng = [r for r in rows if r.variant == ra.SENSITIVITY_VARIANT and r.output_key == "employment_change_55_69"]
    assert len(rng) == 16
    for r in rng:
        lo, hi = Decimal(r.value["lower"]), Decimal(r.value["upper"])
        assert lo == 0  # the no-response reference point
        pv = [Decimal(x.value["value"]) for x in rows if x.output_key == r.output_key and x.period == r.period
              and x.variant.startswith("L2:") and x.variant != ra.SENSITIVITY_VARIANT]
        assert len(pv) == len(pts) and min(pv) == lo and max(pv) == hi
        assert r.uncertainty["kind"] == "sensitivity_range" and r.uncertainty.get("level") is None


def test_pension_drop_comes_from_own_country_data_not_the_study(l2: tuple[Any, list[Any]]) -> None:
    _, rows = l2
    rec = {r.variant: Decimal(r.value["value"]) for r in rows
           if r.output_key == "pension_recipients_change" and r.period[0].year == 2035 and "range" not in r.variant}
    assert len(set(rec.values())) == 1  # every grid point shares the French data-based drop


def test_no_employment_response_gives_zero_employment_change(l2: tuple[Any, list[Any]]) -> None:
    _, rows = l2
    z = by(rows, "employment_change_55_69", f"L2:{lit.NO_RESPONSE_ID}")
    assert z and all(v == 0 for v in z.values())


def test_l1_is_deterministic(l1: tuple[Any, list[Any]]) -> None:
    from reva.simulation.engine import output_digest

    _, again = run(spec())
    assert output_digest(again) == output_digest(l1[1])


def test_larger_shift_gives_larger_effects() -> None:
    out = []
    for m in (6, 18):
        _, rows = run(spec(intervention={"kind": "retirement_age_shift", "reference_path": "law_in_force_2026",
                                         "shift_months": m, "first_birth_year": 1968,
                                         "phase_in_months_per_cohort": 3},
                           horizon={"periods": 12, "unit": "calendar_year"}))
        out.append(by(rows, "pension_recipients_change", "L1")[2036])
    assert out[1] < out[0] < 0


def test_missing_dataset_is_missing_input_never_zero() -> None:
    c, rows = run(spec(), drop=(("activity_rate", "FRA"),))
    assert rows == [] and c.missing and c.missing[0]["input"] == "activity_rate"
    assert c.missing[0]["code"] == "missing_input"


@pytest.mark.parametrize("bad", [
    {"outputs": ["gdp_loss"]}, {"outputs": ["unemployment_rate"]},
])
def test_macro_outputs_are_capability_gaps(bad: dict[str, Any]) -> None:
    with pytest.raises(CapabilityGap):
        ra.MODEL.validate(spec(**bad))


def test_horizon_beyond_2040_and_unknown_paths_refused() -> None:
    with pytest.raises(ValidationProblem):
        ra.MODEL.validate(spec(horizon={"periods": 17, "unit": "calendar_year"}))
    with pytest.raises(ValidationProblem):
        ra.MODEL.validate(spec(intervention={"kind": "retirement_age_rule_path", "scenario_path": "nope",
                                             "reference_path": "law_2010_as_amended_2011"}))


def test_countries_without_rule_tables_are_capability_gaps() -> None:
    for iso3 in ("ITA", "POL"):
        with pytest.raises(CapabilityGap) as e:
            ra.MODEL.validate(spec(countries=[iso3]))
        assert e.value.details.get("country") == iso3


def test_uncovered_years_are_refused_not_extrapolated() -> None:
    s = spec(countries=["NLD"], intervention={"kind": "retirement_age_rule_path", "scenario_path": "law_in_force",
                                              "reference_path": "pre_2013_counterfactual"},
             assumptions={"observed_path": "law_in_force"}, horizon={"periods": 2, "unit": "calendar_year"})
    with pytest.raises(CapabilityGap) as e:
        ra.MODEL.validate(s)
    assert e.value.code == "retirement_rule_coverage_gap"


def test_netherlands_runs_within_coverage_without_expenditure_output() -> None:
    s = spec(countries=["NLD"], intervention={"kind": "retirement_age_rule_path", "scenario_path": "law_in_force",
                                              "reference_path": "pre_2013_counterfactual"},
             assumptions={"observed_path": "law_in_force"}, horizon={"periods": 1, "unit": "calendar_year"})
    c, rows = run(s)
    assert {r.output_key for r in rows} == set(ra.OUTPUTS) - {"pension_expenditure_change_approx_eur"}
    assert c.countries[0].notes[0]["code"] == "output_not_computable"
    assert rows[0].applicability["grid_point"]["point_id"] == "atav_jongen_rabate_2021_nld_wp"


def test_spain_short_track_assumption_is_refused_by_the_data() -> None:
    s = spec(countries=["ESP"], intervention={"kind": "retirement_age_rule_path", "scenario_path": "law_in_force",
                                              "reference_path": "pre_2013_counterfactual"},
             assumptions={"observed_path": "law_in_force",
                          "employment_response": {"central": "staubli_zweimuller_2013_aut"}})
    with pytest.raises(ValidationProblem, match="leaves"):
        run(s)


def test_germany_requires_an_explicit_central_estimate() -> None:
    s = spec(countries=["DEU"], reference_year=2023, horizon={"periods": 17, "unit": "calendar_year"},
             intervention={"kind": "retirement_age_rule_path", "scenario_path": "law_in_force",
                           "reference_path": "pre_2007_counterfactual"}, assumptions={"observed_path": "law_in_force"})
    with pytest.raises(ValidationProblem, match="central"):
        ra.MODEL.validate(s)


def test_spec_schema_fragment_is_closed() -> None:
    from reva.simulation.scenario_schema import branch_schema

    b = branch_schema(ra.MODEL)
    assert b["additionalProperties"] is False
    assert "gdp_loss" not in b["properties"]["outputs"]["items"]["enum"]
