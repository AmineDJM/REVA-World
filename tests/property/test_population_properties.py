"""Property tests: exact conservation for arbitrary nonnegative leaf counts, and leaf
sums reproducing the reported TOTAL for any mix of single years, bands and Y_OPEN."""

from __future__ import annotations

from datetime import date
from fractions import Fraction

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from reva.compression import coarsen, refine
from reva.kernel.errors import ValidationProblem
from reva.population import (
    InconsistentSourceTotals,
    SourceCell,
    build_recipe,
    exact_total_weight,
    exact_weight_sums,
    materialise,
    standard_age_bands,
)


@st.composite
def recipes_and_n(draw):
    top = draw(st.integers(min_value=1, max_value=30))
    ages = ["Y_LT1", *[f"Y{k}" for k in range(1, top + 1)], "Y_OPEN", "UNK"]
    count = st.integers(min_value=0, max_value=10**8)
    cells = []
    for sex in ("F", "M"):
        for a in ages:
            cells.append(SourceCell(a, sex, draw(count), (), f"{sex}.{a}"))
    if all(c.count == 0 for c in cells):
        cells[0] = SourceCell(cells[0].age_code, "F", 1, (), cells[0].source_key)
    # a reported grand total: consistent, or perturbed (then it blocks unless explained)
    leaf_total = sum(c.count or 0 for c in cells)
    offset = draw(st.sampled_from([0, 0, 1, -1]))
    cells.append(SourceCell("TOTAL", "T", max(leaf_total + offset, 0), (), "T.TOTAL"))
    nonzero = sum(1 for c in cells[:-1] if c.count)
    n = draw(st.integers(min_value=nonzero, max_value=nonzero + 3000))
    seed = draw(st.integers(min_value=0, max_value=2**31))
    kw = {"geography": "XX", "reference_date": date(2024, 1, 1), "source_ref": {}, "seed": seed}
    if offset and leaf_total + offset >= 0:
        with pytest.raises(InconsistentSourceTotals):
            build_recipe(cells, **kw)
        recipe = build_recipe(cells, inconsistency_explanation="test: perturbed total", **kw)
        assert recipe.totals_status == "inconsistent_accepted"
    else:
        recipe = build_recipe(cells, **kw)
    return recipe, n, top


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(recipes_and_n())
def test_exact_conservation_for_random_counts(data):
    recipe, n, top = data
    t = materialise(recipe, n)
    assert t.num_rows == n
    sums = {k[0]: v for k, v in exact_weight_sums(t, ["cell_id"]).items()}
    assert sums == {c.cell_id: Fraction(c.count) for c in recipe.leaf_cells if c.count > 0}
    assert exact_total_weight(t) == recipe.total_count
    # coarsen -> refine round trip is exact as well
    coarse = coarsen(t, standard_age_bands(5, top + 1, top))
    assert coarse.certificate.conserved
    back = refine(coarse.table, recipe, "age")
    assert back.certificate.conserved and back.certificate.measured_error == "0"


def _band_code(lo: int, hi: int | None) -> str:
    if hi is None:
        return f"Y_GE{lo}"
    if lo == hi:
        return "Y_LT1" if lo == 0 else f"Y{lo}"
    return f"Y_LT{hi + 1}" if lo == 0 else f"Y{lo}-{hi}"


@st.composite
def mixed_sources(draw):
    """True single-year counts 0..A (+ an open top mass), published as a random mix of
    single years and disjoint bands, a top group (Y_OPEN, Y_GEk or none), optional UNK,
    redundant coarser subtotals, and consistent TOTALs per sex and for both sexes."""
    a_max = draw(st.integers(min_value=2, max_value=25))
    count = st.integers(min_value=0, max_value=10**6)
    top_kind = draw(st.sampled_from(["open", "ge_band", "none"]))
    with_unk = draw(st.booleans())
    # partition 0..a_max into segments; each published as singles or one band
    cuts = sorted(draw(st.sets(st.integers(min_value=1, max_value=a_max), max_size=a_max)))
    bounds = [0, *cuts, a_max + 1]
    segments = [(bounds[i], bounds[i + 1] - 1) for i in range(len(bounds) - 1)]
    as_band = [draw(st.booleans()) for _ in segments]
    if top_kind == "open" and as_band[-1]:
        as_band[-1] = False  # keep the Y_OPEN bound identifiable from the highest observed age
    cells: list[SourceCell] = []
    totals: dict[str, int] = {}
    expected_codes: list[str] = []
    for sex in ("F", "M"):
        ages = [draw(count) for _ in range(a_max + 1)]
        top_mass = draw(count) if top_kind != "none" else 0
        unk = draw(count) if with_unk else 0
        for (lo, hi), band in zip(segments, as_band, strict=True):
            if band and hi > lo:
                cells.append(SourceCell(_band_code(lo, hi), sex, sum(ages[lo : hi + 1]), (), f"{sex}.{lo}-{hi}"))
                if sex == "F":
                    expected_codes.append(_band_code(lo, hi))
            else:
                for k in range(lo, hi + 1):
                    cells.append(SourceCell(_band_code(k, k), sex, ages[k], (), f"{sex}.{k}"))
                    if sex == "F":
                        expected_codes.append(_band_code(k, k))
        if top_kind == "open":
            cells.append(SourceCell("Y_OPEN", sex, top_mass, (), f"{sex}.open"))
            # a redundant open subtotal covering the last segment plus Y_OPEN
            lo = segments[-1][0]
            cells.append(SourceCell(f"Y_GE{lo}", sex, sum(ages[lo:]) + top_mass, (), f"{sex}.ge{lo}"))
        elif top_kind == "ge_band":
            cells.append(SourceCell(f"Y_GE{a_max + 1}", sex, top_mass, (), f"{sex}.ge"))
        if with_unk:
            cells.append(SourceCell("UNK", sex, unk, (), f"{sex}.unk"))
        # a redundant broad subtotal over the whole finite range (unless that is the only leaf)
        if len(segments) > 1 or not as_band[0]:
            cells.append(SourceCell(_band_code(0, a_max), sex, sum(ages), (), f"{sex}.all"))
        totals[sex] = sum(ages) + top_mass + unk
        cells.append(SourceCell("TOTAL", sex, totals[sex], (), f"{sex}.TOTAL"))
    cells.append(SourceCell("TOTAL", "T", totals["F"] + totals["M"], (), "T.TOTAL"))
    if top_kind == "open":
        expected_codes.append("Y_OPEN")
    elif top_kind == "ge_band":
        expected_codes.append(f"Y_GE{a_max + 1}")
    if with_unk:
        expected_codes.append("UNK")
    return cells, totals, expected_codes, a_max, top_kind


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(mixed_sources())
def test_leaf_sums_equal_reported_total_for_mixed_sources(data):
    cells, totals, expected_codes, a_max, top_kind = data
    if sum(totals.values()) == 0:
        return
    recipe = build_recipe(cells, geography="XX", reference_date=date(2024, 1, 1), source_ref={}, seed=3)
    # every covered age once: the finest disjoint cover is exactly the published leaves
    assert set(recipe.leaf_age_codes) == set(expected_codes)
    for sex in ("F", "M"):
        assert sum(c.count for c in recipe.leaf_cells if c.sex_code == sex) == totals[sex]
    assert recipe.total_count == totals["F"] + totals["M"]
    assert recipe.totals_status == "consistent" and recipe.is_internally_consistent
    if top_kind == "open":
        assert recipe.open_age_lower_bound == a_max + 1
    nonzero = len(recipe.nonzero_cells)
    t = materialise(recipe, nonzero + 50)
    assert exact_total_weight(t) == recipe.total_count
    # coarsening everything into one open band and refining back is exact
    coarse = coarsen(t, [("Y_GE0", 0, None)])
    assert coarse.certificate.conserved
    back = refine(coarse.table, recipe, "age")
    assert back.certificate.measured_error == "0"
    # dropping any one leaf age code never loses population silently: either a coarser
    # published band takes over (and totals still match exactly) or the recipe is refused
    leaf_code = recipe.leaf_age_codes[0]
    perturbed = [c for c in cells if not (c.sex_code in ("F", "M") and c.age_code == leaf_code)]
    try:
        r2 = build_recipe(perturbed, geography="XX", reference_date=date(2024, 1, 1), source_ref={}, seed=3)
    except ValidationProblem:
        return
    assert leaf_code not in r2.leaf_age_codes
    assert r2.total_count == totals["F"] + totals["M"] and r2.totals_status == "consistent"
