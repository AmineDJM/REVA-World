"""Regression tests (agent SFX1) for the scientific review of ``social.retirement_age`` (offline fixtures).

* M2 - claim-year eligibility (NLD AOW, ESP DT 7ª) is attained once, never revoked by a later
  year's higher age (AOW art. 7a(1), archived text); checked against a brute-force month scan.
* M3 - eligibility ages that affect ages outside 55-69 are refused (capability_gap), never cut.
* M6 - a target statutory age *level* for cohorts from a birth year (``retirement_age_target``).
* H4 - the pension-receipt proxy has unknown direction; L2 carries two bounding variants.
* M4 - every country (France included) is ``qualified``; the qualification is in each value.
* M8 - a one-year population base period with average-residual migration is refused unless
  explicitly allowed, and then warned.
"""

from __future__ import annotations

import json
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from reva.kernel.errors import CapabilityGap, ValidationProblem
from reva.models.social import retirement_age as ra
from reva.models.social import rules as rl

from .fixture_inputs import resolved_inputs

ROOT = Path(__file__).resolve().parents[3]


def spec(**kw: Any) -> dict[str, Any]:
    base = {"schema_version": "2.0.0", "title": "SFX1 retirement test", "model_key": ra.MODEL_KEY,
            "model_version": ra.MODEL_VERSION, "countries": ["FRA"], "reference_year": 2024,
            "horizon": {"periods": 16, "unit": "calendar_year"}, "fidelity": "L1",
            "intervention": {"kind": "retirement_age_rule_path", "scenario_path": "law_in_force_2026",
                             "reference_path": "law_2010_as_amended_2011"},
            "assumptions": {"observed_path": "law_in_force_2026"}, "missing_input_policy": "fail",
            "outputs": sorted(ra.OUTPUTS), "seed": 0}
    base.update(kw)
    return base


def run(s: dict[str, Any], fidelity: str = "L1") -> tuple[Any, list[Any]]:
    ra.MODEL.validate(s)
    c = ra.MODEL.compile(resolved_inputs(ra.MODEL.requirements(s)), s)
    assert not c.missing and not c.incompatible, (c.missing, c.incompatible)
    return c, ra.MODEL.compute(c, fidelity)


def by(rows: list[Any], output: str, variant: str) -> dict[int, Decimal]:
    return {r.period[0].year: Decimal(r.value["value"]) for r in rows if r.output_key == output
            and r.variant == variant}


def nld_path() -> rl.TablePath:
    return rl.TablePath(rl.load_rule_table("NLD"), "law_in_force", "state_pension_age")


def esp_path() -> rl.TablePath:
    return rl.TablePath(rl.load_rule_table("ESP"), "law_in_force", "normal_pension_age", "short_contribution")


# --------------------------------------------------------------------------- M2
def test_aow_once_attained_rule_is_cited_verbatim_from_the_archived_text() -> None:
    t = rl.load_rule_table("NLD")
    sem = t.claim_year_semantics
    assert sem["rule"] == "attained_once" and sem["derivation"] == "verbatim"
    text = rl.source_text("NLD_aow_art7a_excerpt.txt")
    assert "in de kalenderjaren daarna niet van toepassing" in text
    assert rl.normalise_text(sem["citation"]["locator"]) in text
    esp = rl.load_rule_table("ESP").claim_year_semantics
    assert esp["derivation"] == "model_assumption" and esp["note"]


def test_claim_year_table_must_declare_its_semantics_and_cite_it() -> None:
    import yaml

    doc = yaml.safe_load((rl.RULE_DIR / "NLD.yaml").read_text(encoding="utf-8"))
    no_sem = {k: v for k, v in doc.items() if k != "claim_year_semantics"}
    with pytest.raises(ValidationProblem, match="claim_year_semantics"):
        rl.parse_rule_table(no_sem)
    tampered = json.loads(json.dumps(doc))
    tampered["claim_year_semantics"]["citation"]["locator"] = "zijn de leeftijden ieder jaar opnieuw van toepassing"
    with pytest.raises(ValidationProblem, match="locator"):
        rl.parse_rule_table(tampered)


def test_born_august_1950_stays_eligible_in_january_2016() -> None:
    """65y3m (2015 age) reached in November 2015; in January 2016 the person is 65y5m < 65y6m (the 2016 age)
    but the AOW text says the later age no longer applies: still eligible (the old code revoked it)."""
    p = nld_path()
    assert p.age_months(1950, 8, 2016) == 12 * 65 + 6
    assert rl.is_eligible(p, 1950, 8, 2015, 11)
    assert rl.is_eligible(p, 1950, 8, 2016, 1)
    assert not rl.is_eligible(p, 1950, 8, 2015, 10)  # 65y2m in October 2015: not yet


def _brute_force_eligible(table: rl.RuleTable, path: str, kind: str, track: str, by_: int, bm: int, year: int,
                          month: int, first_year: int) -> bool:
    """Independent check: any month tau <= observation with age(tau) >= age of tau's calendar year
    (scanned from age 60 or the table's first covered year, whichever is later)."""
    obs = 12 * year + month - 1
    start = max(12 * by_ + bm - 1 + 12 * 60, 12 * first_year)  # nobody reaches these ages before 60
    for tau in range(start, obs + 1):
        y = tau // 12
        age = tau - (12 * by_ + bm - 1)
        if age >= table.age_months_at(path, kind, by_, bm, y, track):
            return True
    return False


@pytest.mark.parametrize("which", ["NLD", "ESP"])
def test_claim_year_eligibility_is_monotone_and_matches_brute_force(which: str) -> None:
    p = nld_path() if which == "NLD" else esp_path()
    last_year = 2025 if which == "NLD" else 2030
    first_year = 2013 if which == "ESP" else 2010
    births = [(y, m) for y in range(1946, 1962) for m in range(1, 13)]
    for by_, bm in births:
        was = False
        for y in range(max(first_year, by_ + 62), min(last_year, by_ + 69) + 1):
            for m in range(1, 13):
                now = rl.is_eligible(p, by_, bm, y, m)
                assert not (was and not now), f"{which}: born {by_}-{bm:02d} loses eligibility in {y}-{m:02d}"
                if y >= first_year + 3:  # brute force needs coverage of the years it scans
                    assert now == _brute_force_eligible(p.table, p.path, p.age_kind, p.track, by_, bm, y, m,
                                                        first_year)
                was = was or now


def test_nld_ineligible_share_no_longer_counts_revoked_persons() -> None:
    p = nld_path()
    below_old = 0  # the previous rule: below the age of the observation year, re-tested every year
    for m in range(12):
        for k in range(12):
            by_, bm0 = divmod(12 * (2016 - 65) + m - k, 12)
            if 12 * 65 + k < p.age_months(by_, bm0 + 1, 2016):
                below_old += 1
    share = rl.ineligible_share_single_age(p, 2016, 65)
    assert share < Fraction(below_old, 144)  # persons who reached the 2015 age are eligible in 2016


# --------------------------------------------------------------------------- M3
def deu_spec(intervention: dict[str, Any]) -> dict[str, Any]:
    return spec(countries=["DEU"], reference_year=2023, horizon={"periods": 17, "unit": "calendar_year"},
                intervention=intervention,
                assumptions={"observed_path": "law_in_force",
                             "employment_response": {"central": "staubli_zweimuller_2013_aut"}})


def test_shift_beyond_age_69_is_refused_not_cut() -> None:
    s = deu_spec({"kind": "retirement_age_shift", "reference_path": "law_in_force", "shift_months": 60,
                  "first_birth_year": 1964})
    with pytest.raises(CapabilityGap) as e:
        ra.MODEL.validate(s)
    assert e.value.details["capability"] == "social.retirement_age.age_range"
    assert {c["age"] for c in e.value.details["cells"]} >= {70}
    ok = deu_spec({"kind": "retirement_age_shift", "reference_path": "law_in_force", "shift_months": 12,
                   "first_birth_year": 1964})  # 67 -> 68: inside 55-69
    ra.MODEL.validate(ok)


def test_eligibility_below_55_is_refused() -> None:
    s = spec(intervention={"kind": "retirement_age_shift", "reference_path": "law_in_force_2026",
                           "shift_months": -60, "first_birth_year": 1975},
             horizon={"periods": 16, "unit": "calendar_year"})
    ra.MODEL.validate(s)  # 64 - 5 = 59: inside the range
    s2 = spec(intervention={"kind": "retirement_age_target", "reference_path": "law_in_force_2026",
                            "target_age_months": 12 * 54, "first_birth_year": 1975})
    with pytest.raises(CapabilityGap) as e:
        ra.MODEL.validate(s2)
    assert e.value.details["capability"] == "social.retirement_age.age_range"
    assert all(c["age"] < 55 for c in e.value.details["cells"])


# --------------------------------------------------------------------------- M6
def test_target_age_level_equals_the_equivalent_shift() -> None:
    """FRA law in force: 64 years for cohorts from 1969. A target of 65 years (780 months) from 1969 must
    give exactly the results of a +12-month shift from 1969."""
    target = {"kind": "retirement_age_target", "reference_path": "law_in_force_2026", "target_age_months": 780,
              "first_birth_year": 1969}
    shift = {"kind": "retirement_age_shift", "reference_path": "law_in_force_2026", "shift_months": 12,
             "first_birth_year": 1969}
    _, rt = run(spec(intervention=target))
    _, rs = run(spec(intervention=shift))
    for out in ("pension_recipients_change", "employment_change_55_69"):
        a, b = by(rt, out, "L1"), by(rs, out, "L1")
        assert a == b and a[2040] != 0
    desc = next(r for r in rt).applicability
    assert desc["status"] == "qualified"


def test_target_level_keeps_earlier_cohorts_and_phases_in() -> None:
    ref = rl.TablePath(rl.load_rule_table("FRA"), "law_in_force_2026", "minimum_pension_age")
    t = rl.TargetPath(ref, 780, 1969, phase_in_months_per_cohort=3)
    assert t.age_months(1968, 6, 2030) == ref.age_months(1968, 6, 2030)
    assert t.age_months(1969, 6, 2033) == 768 + 3 and t.age_months(1972, 1, 2036) == 780
    assert t.age_months(1990, 1, 2055) == 780
    lower = rl.TargetPath(ref, 744, 1969)  # 62 years: lowering is a level too
    assert lower.age_months(1975, 1, 2037) == 744
    with pytest.raises(ValidationProblem):
        rl.TargetPath(ref, 2000, 1969)
    d = t.describe()
    assert d["kind"] == "target_level_on_rule_table_path" and d["target_age"] == {"years": 65, "months": 0}


def test_target_intervention_is_in_the_plugin_schema_and_scenario_contract() -> None:
    kinds = [b["properties"]["kind"]["const"] for b in ra.spec_schema()["intervention"]["oneOf"]]
    assert "retirement_age_target" in kinds
    branch = next(b for b in ra.spec_schema()["intervention"]["oneOf"]
                  if b["properties"]["kind"]["const"] == "retirement_age_target")
    assert branch["required"] == ["kind", "reference_path", "target_age_months", "first_birth_year"]
    contract = (ROOT / "contracts" / "scenario.schema.json").read_text(encoding="utf-8")
    assert "retirement_age_target" in contract and "target_age_months" in contract
    with pytest.raises(ValidationProblem):
        ra.MODEL.validate(spec(intervention={"kind": "retirement_age_target", "reference_path": "law_in_force_2026",
                                             "first_birth_year": 1969}))


# --------------------------------------------------------------------------- H4 and M4
@pytest.fixture(scope="module")
def l2() -> tuple[Any, list[Any]]:
    return run(spec(fidelity="L2"), "L2")


def test_l2_has_pension_drop_bounding_variants_inside_the_range(l2: tuple[Any, list[Any]]) -> None:
    c, rows = l2
    central = c.countries[0].central.point_id
    lo_v, hi_v = f"L2:{central};pension_drop=Y60-64", f"L2:{central};pension_drop=Y65-69"
    a, b = by(rows, "pension_recipients_change", lo_v), by(rows, "pension_recipients_change", hi_v)
    own = by(rows, "pension_recipients_change", f"L2:{central}")
    # France: every affected age (62-64) is in the 60-64 group, so the central equals the 60-64 variant;
    # the 65-69 group's inactivity (mostly retired) gives a larger drop
    assert a and b and a == own and abs(b[2035]) > abs(a[2035]) > 0
    rng = {r.period[0].year: r for r in rows if r.variant == ra.SENSITIVITY_VARIANT
           and r.output_key == "pension_recipients_change"}
    for y, r in rng.items():
        lo, hi = Decimal(r.value["lower"]), Decimal(r.value["upper"])
        assert lo <= min(a[y], b[y], own[y]) and max(a[y], b[y], own[y]) <= hi
        assert lo_v in r.applicability["range_over"] and hi_v in r.applicability["range_over"]
    row = next(r for r in rows if r.variant == lo_v)
    assert row.applicability["pension_drop"] == {"basis": "Y60-64", "direction": "unknown",
                                                 "role": "bounding variant"}


def test_no_upper_bound_claim_for_the_proxy(l2: tuple[Any, list[Any]]) -> None:
    _, rows = l2
    blobs = [json.dumps(ra.explain("L2")), json.dumps(ra.manifest_dict()), ra.__doc__ or ""]
    blobs += [json.dumps(r.applicability) + json.dumps(r.value) for r in rows[:50]]
    for b in blobs:
        assert "upper-bound proxy" not in b and "upper-bound-leaning" not in b
    assert "unknown direction" in ra.PENSION_DROP_LABEL


def test_france_is_qualified_and_every_value_carries_its_qualification(l2: tuple[Any, list[Any]]) -> None:
    _, rows = l2
    assert {r.applicability["status"] for r in rows} == {"qualified"}
    for r in rows:
        notes = r.value["basis"]["notes"]
        assert "QUALIFIED (FRA)" in notes and "carrière longue" in notes
        assert "carrière longue" in r.applicability["qualification"]


def test_germany_central_value_is_published_with_its_qualification() -> None:
    _, rows = run(deu_spec({"kind": "retirement_age_rule_path", "scenario_path": "law_in_force",
                            "reference_path": "pre_2007_counterfactual"}))
    central = [r for r in rows if r.variant == "L1" and r.output_key == "pension_recipients_change"]
    assert central
    for r in central:
        assert r.applicability["status"] == "qualified"
        assert r.value["basis"]["notes"].split(". ", 1)[1].startswith("QUALIFIED (DEU): Upper side")
        assert "35 insurance years" in r.applicability["qualification"]


# --------------------------------------------------------------------------- M8 and H1 through PEN
def test_one_year_population_base_needs_explicit_opt_in_and_is_warned() -> None:
    s = spec(horizon={"periods": 2, "unit": "calendar_year"},
             assumptions={"observed_path": "law_in_force_2026", "population": {"base_period_years": 1}})
    with pytest.raises(ValidationProblem, match="allow_single_year_migration"):
        ra.MODEL.validate(s)
    ok = spec(horizon={"periods": 2, "unit": "calendar_year"},
              assumptions={"observed_path": "law_in_force_2026",
                           "population": {"base_period_years": 1, "allow_single_year_migration": True}})
    c, rows = run(ok)
    codes = [w["code"] for w in c.countries[0].data.population_summary["warnings"]]
    assert "migration_window_single_year" in codes and "base_period_single_year" in codes
    assert any("migration_window_single_year" in cv for cv in rows[0].applicability["caveats"])


def test_germany_population_excludes_the_2023_census_break() -> None:
    c, rows = run(deu_spec({"kind": "retirement_age_rule_path", "scenario_path": "law_in_force",
                            "reference_path": "pre_2007_counterfactual"}))
    summary = c.countries[0].data.population_summary
    assert summary["migration_window"]["years_used"] == [2019, 2020, 2021, 2023]
    w = summary["warnings"][0]
    assert w["code"] == "migration_residual_contains_break" and w["years"] == [2022]
    assert any("migration_residual_contains_break" in cv for cv in rows[0].applicability["caveats"])
