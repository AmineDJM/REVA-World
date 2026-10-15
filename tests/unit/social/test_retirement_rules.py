"""Retirement-age rule tables: schema, per-row citations verified against archived text, arithmetic."""

from __future__ import annotations

import copy
from fractions import Fraction

import pytest
import yaml

from reva.kernel.errors import CapabilityGap, ValidationProblem
from reva.models.social import rules as rl

COUNTRIES = rl.available_countries()


def _doc(iso3: str) -> dict:
    with open(rl.RULE_DIR / f"{iso3}.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_expected_countries_have_tables_and_the_rest_are_typed_gaps() -> None:
    assert COUNTRIES == ("DEU", "ESP", "FRA", "NLD")
    for iso3 in ("ITA", "BEL", "SWE", "POL", "AUT"):
        with pytest.raises(CapabilityGap) as e:
            rl.load_rule_table(iso3)
        assert e.value.details["capability"] == "social.retirement_age"
    assert "403" in rl.gap_reason("POL") and "riktålder" in rl.gap_reason("SWE")


@pytest.mark.parametrize("iso3", COUNTRIES)
def test_every_row_is_cited_and_its_locator_is_in_the_archived_source(iso3: str) -> None:
    t = rl.load_rule_table(iso3)  # parse_rule_table checks every locator verbatim
    assert t.review_status == "maintainer_review_pending"
    assert t.transcription == "transcribed from law text by an agent; maintainer review pending"
    for r in t.rows:
        assert r.citation["source_id"] in t.sources and r.citation["legal_reference"] and r.citation["locator"]
        text = rl.source_text(t.sources[r.citation["source_id"]]["archived_file"])
        assert rl.normalise_text(r.citation["locator"]) in text
        if r.derivation != "verbatim":
            assert r.note
    for s in t.sources.values():
        assert (rl.SOURCE_DIR / s["archived_file"]).is_file() and s["retrieved_at"] and s["url"].startswith("https://")


@pytest.mark.parametrize("iso3", COUNTRIES)
def test_a_tampered_locator_is_refused(iso3: str) -> None:
    doc = _doc(iso3)
    bad = copy.deepcopy(doc)
    bad["rows"][0]["citation"]["locator"] += " (not in the law)"
    with pytest.raises(ValidationProblem, match="locator not found"):
        rl.parse_rule_table(bad)
    uncited = copy.deepcopy(doc)
    del uncited["rows"][0]["citation"]
    with pytest.raises(ValidationProblem, match="must cite"):
        rl.parse_rule_table(uncited)


def test_overlapping_rows_and_agent_review_claims_are_refused() -> None:
    doc = _doc("FRA")
    dup = copy.deepcopy(doc)
    dup["rows"].append({**copy.deepcopy(doc["rows"][2]), "row_id": "FRA.dup"})
    with pytest.raises(ValidationProblem, match="overlap"):
        rl.parse_rule_table(dup)
    reviewed = copy.deepcopy(doc)
    reviewed["review_status"] = "reviewed"
    with pytest.raises(ValidationProblem):
        rl.parse_rule_table(reviewed)


@pytest.mark.parametrize(
    ("path", "birth", "months"),
    [
        ("law_in_force_2026", (1951, 6), 720),
        ("law_in_force_2026", (1951, 7), 724),
        ("law_in_force_2026", (1952, 3), 729),
        ("law_in_force_2026", (1955, 1), 744),
        ("law_in_force_2026", (1961, 8), 744),
        ("law_in_force_2026", (1961, 9), 747),
        ("law_in_force_2026", (1964, 5), 753),
        ("law_in_force_2026", (1965, 3), 753),
        ("law_in_force_2026", (1965, 4), 756),
        ("law_in_force_2026", (1968, 12), 765),
        ("law_in_force_2026", (1980, 1), 768),
        ("law_2023_as_enacted", (1964, 5), 756),
        ("law_2023_as_enacted", (1968, 1), 768),
        ("law_2010_as_amended_2011", (1970, 1), 744),
        ("pre_2010_counterfactual", (1970, 1), 720),
    ],
)
def test_france_minimum_ages_match_the_law(path: str, birth: tuple[int, int], months: int) -> None:
    t = rl.load_rule_table("FRA")
    assert t.age_months_at(path, "minimum_pension_age", birth[0], birth[1], 2030) == months


def test_germany_and_netherlands_rows() -> None:
    de = rl.load_rule_table("DEU")
    assert de.age_months_at("law_in_force", "normal_pension_age", 1958, 5, 2024) == 66 * 12
    assert de.age_months_at("law_in_force", "normal_pension_age", 1963, 5, 2024) == 66 * 12 + 10
    assert de.age_months_at("law_in_force", "normal_pension_age", 1970, 5, 2024) == 67 * 12
    nl = rl.load_rule_table("NLD")
    assert nl.age_months_at("law_in_force", "state_pension_age", 1950, 1, 2019) == 66 * 12 + 4
    with pytest.raises(rl.RuleCoverageGap):  # set by regulation from 2026, not archived: never extrapolated
        nl.age_months_at("law_in_force", "state_pension_age", 1960, 1, 2026)
    es = rl.load_rule_table("ESP")
    assert es.age_months_at("law_in_force", "normal_pension_age", 1960, 1, 2026, "short_contribution") == 66 * 12 + 10
    assert es.age_months_at("law_in_force", "normal_pension_age", 1960, 1, 2026, "long_contribution") == 65 * 12


def test_ineligible_share_is_exact_and_bounded() -> None:
    t = rl.load_rule_table("FRA")
    p = rl.TablePath(t, "law_2010_as_amended_2011", "minimum_pension_age")
    ref = rl.TablePath(t, "pre_2010_counterfactual", "minimum_pension_age")
    for y in range(2010, 2021):
        for a in range(55, 70):
            s = rl.ineligible_share_single_age(p, y, a)
            assert (
                isinstance(s, Fraction)
                and 0 <= s <= 1
                and s.denominator in (1, 2, 3, 4, 6, 8, 9, 12, 16, 18, 24, 36, 48, 72, 144)
            )
            assert rl.ineligible_share_single_age(ref, y, a) == (1 if a < 60 else 0)
    assert rl.ineligible_share(p, 2019, range(60, 65)) == Fraction(2, 5)  # ages 60 and 61 fully below 62
    assert rl.ineligible_share(p, 2011, range(60, 65)) > 0


def test_shift_path_is_monotone_in_the_shift() -> None:
    t = rl.load_rule_table("FRA")
    base = rl.TablePath(t, "law_in_force_2026", "minimum_pension_age")
    vals = [rl.ineligible_share(rl.ShiftedPath(base, m, 1970, 0), 2038, range(55, 70)) for m in (6, 12, 24)]
    assert vals[0] < vals[1] < vals[2]
    assert rl.ineligible_share(rl.ShiftedPath(base, 12, 1990, 0), 2038, range(55, 70)) == rl.ineligible_share(
        base, 2038, range(55, 70)
    )  # cohorts born 1990 are under 55 in 2038
    with pytest.raises(ValidationProblem):
        rl.ShiftedPath(base, 0, 1970)


def test_coverage_problems_are_typed() -> None:
    nl = rl.load_rule_table("NLD")
    p = rl.TablePath(nl, "law_in_force", "state_pension_age")
    assert rl.coverage_problems(p, (2025,), range(55, 70)) == []
    probs = rl.coverage_problems(p, (2026,), range(55, 70))
    assert probs and {x["code"] for x in probs} == {"retirement_rule_coverage_gap"}
