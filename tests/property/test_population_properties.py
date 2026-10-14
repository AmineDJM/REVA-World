"""Property tests: exact conservation for arbitrary nonnegative leaf counts."""

from __future__ import annotations

from datetime import date
from fractions import Fraction

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from reva.compression import coarsen, refine
from reva.population import (
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
    # include a (possibly inconsistent) reported grand total; it must never be counted
    cells.append(SourceCell("TOTAL", "T", draw(count), (), "T.TOTAL"))
    nonzero = sum(1 for c in cells[:-1] if c.count)
    n = draw(st.integers(min_value=nonzero, max_value=nonzero + 3000))
    seed = draw(st.integers(min_value=0, max_value=2**31))
    recipe = build_recipe(
        cells, geography="XX", reference_date=date(2024, 1, 1), source_ref={}, seed=seed
    )
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
