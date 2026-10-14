"""Leaf partition = finest disjoint cover (R3-01), Y_OPEN lower bound (R3-09),
observed vs structural zeros (R3-12), and the real FR demo_pjan 2023 structure."""

from __future__ import annotations

import json
from datetime import date
from fractions import Fraction
from pathlib import Path

import pytest

from reva.compression import coarsen, refine
from reva.kernel.errors import ValidationProblem
from reva.population import (
    InconsistentSourceTotals,
    SourceCell,
    build_recipe,
    diagnose,
    exact_total_weight,
    exact_weight_sums,
    finest_disjoint_cover,
    materialise,
    standard_age_bands,
    suggest_coarsening,
)

FIVE_YEAR_TO_85 = [(f"Y{lo}-{lo + 4}", lo, lo + 4) for lo in range(0, 85, 5)] + [("Y_GE85", 85, None)]
FR_FIXTURE = (
    Path(__file__).resolve().parents[2] / "fixtures/source/eurostat.demo_pjan.fr/9a08edda6adba71f.json"
)


def _cells(per_sex: dict[str, list[tuple[str, int | None]]], *, totals: bool = True) -> list[SourceCell]:
    out = [SourceCell(a, s, c, (), f"{s}|{a}") for s, rows in per_sex.items() for a, c in rows]
    if totals:
        sums = {s: sum(c for _, c in rows if c is not None) for s, rows in per_sex.items()}
        for s, v in sums.items():
            out.append(SourceCell("TOTAL", s, v, (), f"{s}|TOTAL"))
        out.append(SourceCell("TOTAL", "T", sum(sums.values()), (), "T|TOTAL"))
    return out


def _same(rows: list[tuple[str, int | None]]) -> dict[str, list[tuple[str, int | None]]]:
    return {"F": list(rows), "M": list(rows)}


def _recipe(cells: list[SourceCell], **kw: object):
    return build_recipe(
        cells, geography="XX", reference_date=date(2023, 1, 1), source_ref={}, seed=1, **kw  # type: ignore[arg-type]
    )


def _singles(lo: int, hi: int, count: int = 10) -> list[tuple[str, int | None]]:
    return [("Y_LT1" if k == 0 else f"Y{k}", count) for k in range(lo, hi + 1)]


def _by_cell(table) -> dict[str, Fraction]:
    return {k[0]: v for k, v in exact_weight_sums(table, ["cell_id"]).items()}


# --------------------------------------------------------------------------- R3-01


def test_reviewer_case_a_band_only_top_is_a_leaf_and_materialises_exactly():
    """s10 case A: singles Y_LT1..Y84 + Y_GE85 (no Y_OPEN); TOTAL 1350 per sex."""
    rows = [*_singles(0, 84), ("Y_GE85", 500)]
    r = _recipe(_cells(_same(rows)))
    assert "Y_GE85" in r.leaf_age_codes and r.leaf("F|Y_GE85").count == 500
    assert r.leaf("F|Y_GE85").is_age_band and (r.leaf("F|Y_GE85").age_lo, r.leaf("F|Y_GE85").age_hi) == (85, None)
    assert r.total_count == 2700 and r.totals_status == "consistent"
    assert r.open_age_lower_bound is None  # no Y_OPEN leaf
    t = materialise(r, 1000)
    assert exact_total_weight(t) == 2700
    assert _by_cell(t)["M|Y_GE85"] == 500
    # 5-year coarsening keeps Y_GE85 whole; refine restores the leaves exactly
    c = coarsen(t, FIVE_YEAR_TO_85)
    assert c.certificate.conserved and c.mapping["F|Y_GE85"] == ("F|Y_GE85",)
    back = refine(c.table, r, "age")
    assert back.certificate.conserved and back.certificate.measured_error == "0"
    # a scheme that would split the band leaf is refused, not guessed
    with pytest.raises(ValidationProblem):
        coarsen(t, [("Y_LT90", 0, 89), ("Y_GE90", 90, None)])
    sugg = {s["scheme"]: s for s in suggest_coarsening(r, 10)}
    assert sugg["5_year_age_bands"]["minimum_feasible_records"] == 2 * 18
    assert sugg["broad_age_groups"]["minimum_feasible_records"] == 2 * 3


def test_band_leaves_that_cannot_reproduce_total_block_the_recipe():
    """Y_GE85 = 500 and Y_OPEN = 100 (really 100+) with Y85..Y99 absent: the leaves
    (singles + Y_OPEN placed at 85) drop 400 people per sex -> blocked, never materialised."""
    rows = [*_singles(0, 84), ("Y_GE85", 500), ("Y_OPEN", 100)]
    cells = _cells({"F": rows, "M": rows}, totals=False)
    cells += [SourceCell("TOTAL", s, 850 + 500, (), f"{s}|TOTAL") for s in ("F", "M")]
    with pytest.raises(InconsistentSourceTotals) as ei:
        _recipe(cells)
    assert {c["sex_code"] for c in ei.value.details["failing_checks"]} == {"F", "M"}
    assert all(c["leaf_sum"] == 950 and c["reported"] == 1350 for c in ei.value.details["failing_checks"])


def test_band_only_source_materialises_exactly():
    """demo_pjangroup-like: 5-year bands + Y_GE85 with overlapping broad subtotals."""
    bands = [("Y_LT5", 50)] + [(f"Y{lo}-{lo + 4}", 40 + lo) for lo in range(5, 85, 5)] + [("Y_GE85", 30)]
    f = bands
    m = [(a, c + 3) for a, c in bands]
    per_sex = {"F": f, "M": m}
    cells = _cells(per_sex)
    for s, rows in per_sex.items():
        d = dict(rows)
        cells.append(SourceCell("Y15-64", s, sum(d[f"Y{lo}-{lo + 4}"] for lo in range(15, 65, 5)), (), f"{s}|Y15-64"))
        cells.append(SourceCell("Y_GE65", s, sum(d[f"Y{lo}-{lo + 4}"] for lo in range(65, 85, 5)) + d["Y_GE85"], (),
                                f"{s}|Y_GE65"))
    r = _recipe(cells)
    assert r.leaf_age_codes == tuple(a for a, _ in bands)
    assert r.total_count == sum(c for _, c in f) + sum(c for _, c in m)
    assert r.totals_status == "consistent" and r.is_internally_consistent
    subtotals = {(c.sex_code, c.age_code): c for c in r.consistency}
    assert subtotals[("F", "Y15-64")].status == "consistent"
    assert subtotals[("M", "Y_GE65")].status == "consistent"
    assert all(c.age_years is None and c.is_age_band for c in r.leaf_cells)
    t = materialise(r, 500)
    assert _by_cell(t) == {c.cell_id: Fraction(c.count) for c in r.leaf_cells}
    tens = [("Y_LT10", 0, 9)] + [(f"Y{lo}-{lo + 9}", lo, lo + 9) for lo in range(10, 80, 10)] + [("Y_GE80", 80, None)]
    with pytest.raises(ValidationProblem):  # Y_GE85 fits Y_GE80, but Y80-84 + Y_GE85 do; Y_LT5 fits Y_LT10 ...
        coarsen(t, [("Y_LT3", 0, 2), ("Y_GE3", 3, None)])  # ... while Y_LT5 would be split here
    c = coarsen(t, tens)
    assert c.certificate.conserved
    back = refine(c.table, r, "age")
    assert back.certificate.measured_error == "0"
    d = diagnose(t, r)
    assert d["support_violations"]["ok"] and d["source_consistency"]["totals_status"] == "consistent"


def test_band_only_source_with_open_class_infers_bound_from_bands():
    rows = [("Y_LT5", 50)] + [(f"Y{lo}-{lo + 4}", 40) for lo in range(5, 85, 5)] + [("Y_OPEN", 30)]
    r = _recipe(_cells(_same(rows)))
    assert r.open_age_lower_bound == 85 and r.open_age_lower_bound_basis.startswith("inferred")
    assert r.leaf("F|Y_OPEN").age_lo == 85
    t = materialise(r, 200)
    c = coarsen(t, FIVE_YEAR_TO_85)
    assert c.certificate.conserved


def test_singles_beyond_band_start_are_replaced_by_the_band_when_band_covers_more():
    """Singles 0..99 and Y_GE85 without Y_OPEN: people aged 100+ exist only in Y_GE85."""
    rows = [*_singles(0, 99), ("Y_GE85", 15 * 10 + 7)]
    totals = [SourceCell("TOTAL", s, 85 * 10 + 157, (), f"{s}|TOTAL") for s in ("F", "M")]
    r = _recipe(_cells(_same(rows), totals=False) + totals)
    assert r.leaf_age_codes[-1] == "Y_GE85" and "Y85" not in r.leaf_age_codes
    assert r.total_count == 2 * (85 * 10 + 157)
    reasons = {c.reason for c in r.excluded_cells if c.age_code == "Y90" and c.sex_code == "F"}
    assert reasons == {"finer_than_leaf_partition"}


def test_mixed_singles_bands_and_open_band():
    rows = [*_singles(0, 84), ("Y85-89", 20), ("Y90-94", 15), ("Y95-99", 5), ("Y_GE100", 1), ("Y_GE85", 41)]
    r = _recipe(_cells({"F": rows, "M": rows}, totals=False) + [
        SourceCell("TOTAL", s, 850 + 41, (), f"{s}|TOTAL") for s in ("F", "M")
    ])
    assert r.leaf_age_codes[-4:] == ("Y85-89", "Y90-94", "Y95-99", "Y_GE100")
    assert r.totals_status == "consistent"
    ge85 = next(c for c in r.consistency if c.age_code == "Y_GE85" and c.sex_code == "F")
    assert ge85.status == "consistent" and ge85.leaf_sum == 41


def test_singles_preferred_over_overlapping_bands_with_open_class(recipe):
    # conftest fixture: singles Y_LT1..Y9, Y_OPEN, UNK + subtotals Y_LT5, Y5-9, Y_GE5
    assert recipe.leaf_age_codes == ("Y_LT1", *[f"Y{k}" for k in range(1, 10)], "Y_OPEN", "UNK")


def test_partially_overlapping_codes_without_disjoint_cover_raise():
    rows = [("Y_LT5", 10), ("Y3-9", 10)]
    with pytest.raises(ValidationProblem) as ei:
        _recipe(_cells(_same(rows)))
    assert set(ei.value.details["age_codes"]) == {"Y_LT5", "Y3-9"}


def test_finest_disjoint_cover_unit():
    assert finest_disjoint_cover({}) == ()
    assert finest_disjoint_cover({"a": (0, 4), "b": (0, 1), "c": (2, 4), "d": (5, None)}) == ("b", "c", "d")
    assert finest_disjoint_cover({"a": (0, 4), "b": (3, 9)}) is None
    # gaps are allowed
    assert finest_disjoint_cover({"a": (0, 1), "b": (5, 6)}) == ("a", "b")


# --------------------------------------------------------------------------- R3-09


def _case_b_rows() -> list[tuple[str, int | None]]:
    return [*_singles(0, 84), *[(f"Y{k}", None) for k in range(85, 100)], ("Y_OPEN", 500), ("UNK", 0)]


def test_open_bound_unknown_when_cells_above_highest_observed_age_are_missing():
    """s10 case B: Y85..Y99 reported ':'; Y_OPEN is really 85+. The bound is not guessed."""
    r = _recipe(_cells(_same(_case_b_rows()), totals=False), allow_missing=True)
    assert r.open_age_lower_bound is None
    assert r.open_age_lower_bound_basis.startswith("unknown") and "Y85" in r.open_age_lower_bound_basis
    assert r.leaf("F|Y_OPEN").age_lo is None
    assert len(r.missing_cells) == 30
    t = materialise(r, 500)
    # coarsening that depends on the bound is refused
    with pytest.raises(ValidationProblem, match="lower bound unknown"):
        coarsen(t, FIVE_YEAR_TO_85)
    sugg = suggest_coarsening(r, 10)
    assert all(s["minimum_feasible_records"] is None and not s["feasible_for_requested"] for s in sugg)
    assert all("unknown" in s["not_applicable_reason"] for s in sugg)
    assert diagnose(t, r)["open_age_lower_bound"] is None


def test_declared_open_bound_from_metadata_is_used():
    r = _recipe(_cells(_same(_case_b_rows()), totals=False), open_age_lower_bound=85)
    assert r.open_age_lower_bound == 85 and r.open_age_lower_bound_basis.startswith("declared")
    assert r.missing_cells == ()  # Y85..Y99 lie inside Y_OPEN per the declaration
    assert {c.reason for c in r.excluded_cells if c.age_code == "Y90"} == {"finer_than_leaf_partition"}
    c = coarsen(materialise(r, 300), FIVE_YEAR_TO_85)
    assert c.certificate.conserved


def test_declared_open_bound_overlapping_observed_cells_raises():
    rows = [*_singles(0, 99), ("Y_OPEN", 5)]
    with pytest.raises(ValidationProblem):
        _recipe(_cells(_same(rows)), open_age_lower_bound=90)
    with pytest.raises(ValidationProblem):
        _recipe(_cells(_same(rows)), open_age_lower_bound=True)


def test_open_bound_inferred_from_highest_observed_age_despite_gap_below():
    rows = [*_singles(0, 99), ("Y_OPEN", 5)]
    rows[50] = ("Y50", None)
    r = _recipe(_cells(_same(rows), totals=False), allow_missing=True)
    assert r.open_age_lower_bound == 100
    assert [m.cell_id for m in r.missing_cells] == ["F|Y50", "M|Y50"]


def test_open_bound_ignores_codes_present_without_counts():
    rows = [*_singles(0, 89), *[(f"Y{k}", None) for k in range(90, 100)], ("Y_OPEN", 5)]
    with pytest.raises(ValidationProblem):  # missing leaves are never zero
        _recipe(_cells(_same(rows), totals=False))
    r = _recipe(_cells(_same(rows), totals=False), allow_missing=True)
    assert r.open_age_lower_bound is None  # not 100: Y90..Y99 have no counts


# --------------------------------------------------------------------------- R3-12


def test_observed_zero_cells_are_not_structural_zeros():
    rows = [("Y_LT1", 10), ("Y1", 10), ("Y_OPEN", 3), ("UNK", 0)]
    r = _recipe(_cells(_same(rows)))
    assert r.observed_zero_cells == ("F|UNK", "M|UNK")
    assert r.structural_zeros == ()
    t = materialise(r, 10)
    sv = diagnose(t, r)["support_violations"]
    assert sv == {
        "records_in_observed_zero_cells": 0,
        "records_in_structural_zero_cells": 0,
        "records_in_missing_cells": 0,
        "records_outside_recipe_cells": 0,
        "ok": True,
        "note": sv["note"],
    }


# --------------------------------------------------------------------------- FR demo_pjan 2023


def _fr_cells() -> list[SourceCell]:
    d = json.loads(FR_FIXTURE.read_text())
    assert d["id"] == ["freq", "unit", "age", "sex", "geo", "time"]
    ages = sorted(d["dimension"]["age"]["category"]["index"].items(), key=lambda kv: kv[1])
    sexes = sorted(d["dimension"]["sex"]["category"]["index"].items(), key=lambda kv: kv[1])
    cells = []
    for ai, (age, _) in enumerate(ages):
        for si, (sex, _) in enumerate(sexes):
            idx = str(ai * len(sexes) + si)
            v = d["value"].get(idx)
            st = d.get("status", {}).get(idx, "")
            cells.append(SourceCell(age, sex, None if v is None else int(v), tuple(st), f"FR.2023.{sex}.{age}"))
    return cells


@pytest.mark.skipif(not FR_FIXTURE.exists(), reason="captured FR demo_pjan fixture not present")
def test_fr_demo_pjan_2023_structure_unchanged():
    r = _recipe(_fr_cells())
    assert r.leaf_age_codes == ("Y_LT1", *[f"Y{k}" for k in range(1, 100)], "Y_OPEN", "UNK")
    assert len(r.leaf_cells) == 204 and r.missing_cells == ()
    assert r.total_count == 68_277_210
    assert sum(c.count for c in r.leaf_cells if c.sex_code == "F") == 35_188_809
    assert sum(c.count for c in r.leaf_cells if c.sex_code == "M") == 33_088_401
    assert r.totals_status == "consistent" and r.is_internally_consistent
    assert r.open_age_lower_bound == 100 and r.open_age_lower_bound_basis.startswith("inferred")
    assert r.observed_zero_cells == ("F|UNK", "M|UNK") and r.structural_zeros == ()
    t = materialise(r, 100_000)
    assert exact_total_weight(t) == 68_277_210
    c = coarsen(t, FIVE_YEAR_TO_85)
    assert c.certificate.conserved
    assert standard_age_bands(5, r.open_age_lower_bound, 99)[-1] == ("Y_GE100", 100, None)
