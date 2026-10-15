"""P10 household extension: structural zeros, missing never zero, exact weights, held-out SILC scoring."""

from __future__ import annotations

from fractions import Fraction

import pytest

from reva.population import households as hh
from reva.population.recipe import FORBIDDEN_ATTRIBUTES

from .fixture_inputs import labour_tables

YEAR = 2024


def _rows(iso3: str) -> list[dict]:
    return labour_tables()["lfst_hhnhtych"][iso3].to_pylist()


def _synth(iso3: str, n: int = 5000, seed: int = 3) -> hh.HouseholdSynthesis:
    lt = labour_tables()
    return hh.synthesise_households(_rows(iso3), geography=iso3, year=YEAR, seed=seed, n_records=n,
                                    source_ref={"table": "lfst_hhnhtych"},
                                    size_rows=lt["ilc_lvph03"][iso3].to_pylist(),
                                    avg_rows=lt["ilc_lvph01"][iso3].to_pylist(), person_total=68_000_000)


def test_france_recipe_cells_structural_zeros_and_totals() -> None:
    s = _synth("FRA")
    r = s.recipe
    assert r.totals_check["status"] == "consistent_within_rounding"
    assert "A1_NCH|1" in r.structural_zeros and "CPL_CH|0" in r.structural_zeros
    assert {c.cell_id for c in r.cells} >= {"A1_NCH|0", "CPL_CH|2", "OTH_CH|GE3"}
    a1 = next(c for c in r.cells if c.cell_id == "A1_NCH|0")
    assert (a1.size_lo, a1.size_hi, a1.households) == (1, 1, 13_360_600)
    big = next(c for c in r.cells if c.cell_id == "CPL_CH|GE3")
    assert (big.size_lo, big.size_hi) == (5, None)  # open: never imputed
    assert {m["cell_id"] for m in r.missing_cells} >= {"NRP|0"}


def test_exact_weight_conservation_and_no_support_violations() -> None:
    s = _synth("FRA")
    joint = hh.exact_sums(s.table, "cell_id")
    assert joint == {c.cell_id: Fraction(c.households) for c in s.recipe.cells if c.households > 0}
    d = s.diagnostics
    assert d["support_violations"]["ok"]
    assert d["calibration_error"]["joint_composition_children"]["max_abs_error"] == "0"
    assert d["person_population_check"]["status"] == "consistent"


def test_records_carry_no_person_attributes() -> None:
    names = set(hh.HOUSEHOLD_SCHEMA.names)
    assert not names & {"age", "sex", "age_code", "sex_code", "income"}
    assert not names & set(FORBIDDEN_ATTRIBUTES)
    assert any("linkage" in a for a in hh.UNSUPPORTED_ASSOCIATIONS)


def test_structural_zero_with_a_reported_value_is_refused() -> None:
    rows = [dict(r) for r in _rows("FRA")]
    for r in rows:
        if r["year"] == YEAR and r["dim1"] == "A1_NCH" and r["dim2"] == "2":
            r["value"] = "5.0"
    with pytest.raises(hh.InconsistentHouseholdSource):
        hh.build_household_recipe(rows, geography="FRA", year=YEAR, source_ref={}, seed=1)


def test_missing_leaf_stays_missing_and_totals_gate_reports_remainder() -> None:
    r = _synth("DEU").recipe  # NRP suppressed (flag u) in 2024
    assert r.totals_check["status"] == "not_computable_missing_leaves"
    assert r.totals_check["unexplained_remainder_if_missing"] > 0
    assert all(c.composition != "NRP" for c in r.cells)


def test_inconsistent_total_is_blocked_unless_explained() -> None:
    rows = [dict(r) for r in _rows("FRA")]
    for r in rows:
        if r["year"] == YEAR and r["dim1"] == "TOTAL" and r["dim2"] == "TOTAL":
            r["value"] = "40000"
    with pytest.raises(hh.InconsistentHouseholdSource):
        hh.build_household_recipe(rows, geography="FRA", year=YEAR, source_ref={}, seed=1)
    rec = hh.build_household_recipe(rows, geography="FRA", year=YEAR, source_ref={}, seed=1,
                                    inconsistency_explanation="test: total edited")
    assert rec.accepted_inconsistency == "test: total edited"


def test_held_out_silc_scoring_is_reported_including_misses() -> None:
    d = _synth("FRA").diagnostics["held_out"]
    assert d["scored"] == 6
    statuses = {c["size_class"]: c["status"] for c in d["size_classes"]}
    assert statuses["1"] == "outside"  # LFS one-adult share > SILC one-person share: reported, not corrected
    assert statuses["2"] == "inside"
    assert d["average_size"]["status"] in ("inside", "outside")


def test_determinism_and_resolution() -> None:
    a, b = _synth("FRA", seed=11), _synth("FRA", seed=11)
    assert a.table.equals(b.table) and a.recipe.digest() == b.recipe.digest()
    with pytest.raises(hh.HouseholdResolutionError):
        _synth("FRA", n=5)
