from __future__ import annotations

import io
from fractions import Fraction

import numpy as np
import pyarrow.parquet as pq
import pytest

from reva.kernel.errors import CapabilityGap, ValidationProblem
from reva.population import (
    MAX_RECORDS,
    ResolutionError,
    allocate,
    exact_total_weight,
    exact_weight_sums,
    materialise,
    summary_logical_hash,
    table_logical_hash,
    to_parquet_bytes,
)

from .conftest import AGES, make_cells, recipe_from

NONZERO = 23  # 24 leaves minus the observed zero M|Y3


@pytest.mark.parametrize("n", [NONZERO, NONZERO + 1, 50, 997, 10_000, MAX_RECORDS])
def test_leaf_totals_conserved_exactly(recipe, n):
    t = materialise(recipe, n)
    assert t.num_rows == n
    assert t.column("record_id").to_pylist() == list(range(n))
    sums = {k[0]: v for k, v in exact_weight_sums(t, ["cell_id"]).items()}
    assert sums == {c.cell_id: Fraction(c.count) for c in recipe.leaf_cells if c.count > 0}
    assert exact_total_weight(t) == recipe.total_count
    # never leaves + totals/subtotals
    reported = sum(c.count or 0 for c in recipe.excluded_cells)
    assert exact_total_weight(t) != recipe.total_count + reported
    grand = next(c for c in recipe.excluded_cells if c.reason == "grand_total")
    assert exact_total_weight(t) == grand.count
    # float weights are the rounding of the exact fraction
    num = t.column("weight_num").to_numpy()
    den = t.column("weight_den").to_numpy()
    np.testing.assert_allclose(t.column("weight").to_numpy(), num / den, rtol=1e-15)
    assert all(Fraction(a, b).denominator == b for a, b in zip(num[:50], den[:50], strict=True))


def test_every_nonzero_cell_represented_and_zero_cell_empty(recipe):
    alloc = allocate(recipe, NONZERO)
    assert all(c.records == 1 for c in alloc.cells)
    t = materialise(recipe, 500)
    cells = set(t.column("cell_id").to_pylist())
    assert "M|Y3" not in cells
    assert cells == {c.cell_id for c in recipe.nonzero_cells}


def test_allocation_is_proportional_largest_remainder(recipe):
    n = 1000
    alloc = allocate(recipe, n)
    total = recipe.total_count
    for c in alloc.cells:
        quota = 1 + Fraction((n - NONZERO) * c.count, total)
        assert abs(c.records - quota) < 1


def test_age_and_tail_columns(recipe):
    t = materialise(recipe, 200).to_pydict()
    rows = {
        cid: (age, years, top, unk)
        for cid, age, years, top, unk in zip(
            t["cell_id"],
            t["age_code"],
            t["age_years"],
            t["is_top_coded"],
            t["is_unknown_age"],
            strict=True,
        )
    }
    assert rows["F|Y_LT1"] == ("Y_LT1", 0, False, False)
    assert rows["F|Y7"] == ("Y7", 7, False, False)
    assert rows["M|Y_OPEN"] == ("Y_OPEN", None, True, False)
    assert rows["F|UNK"] == ("UNK", None, False, True)
    assert set(t["sex_code"]) == {"F", "M"}
    assert {a for a in t["age_code"]} <= set(AGES)


def test_same_inputs_identical_outputs(cells):
    a = materialise(recipe_from(cells, seed=3), 777)
    b = materialise(recipe_from(cells, seed=3), 777)
    assert a.equals(b, check_metadata=True)
    assert table_logical_hash(a) == table_logical_hash(b)
    assert summary_logical_hash(a) == summary_logical_hash(b)
    assert to_parquet_bytes(a) == to_parquet_bytes(b)


def test_table_hash_is_row_order_independent_and_content_sensitive(recipe):
    t = materialise(recipe, 300)
    perm = np.random.default_rng(0).permutation(t.num_rows)
    assert table_logical_hash(t.take(perm)) == table_logical_hash(t)
    assert table_logical_hash(t.slice(1)) != table_logical_hash(t)


def test_parquet_roundtrip_preserves_logical_content(recipe):
    t = materialise(recipe, 400)
    data = to_parquet_bytes(t)
    back = pq.read_table(io.BytesIO(data))
    assert pq.ParquetFile(io.BytesIO(data)).metadata.row_group(0).column(0).compression == "ZSTD"
    assert table_logical_hash(back) == table_logical_hash(t)
    assert back.schema.metadata[b"reva.recipe_digest"].decode() == recipe.digest()


def test_seed_only_changes_tied_allocations():
    equal = [100] * 12
    cells = make_cells(f_counts=equal, m_counts=equal)
    allocs = {s: allocate(recipe_from(cells, seed=s), 30).records_by_cell() for s in range(10)}
    # 24 equal cells, 6 extra records: all remainders tie, so the seed decides placement
    assert len({tuple(sorted(a.items())) for a in allocs.values()}) > 1
    for a in allocs.values():
        assert sorted(a.values()) == [1] * 18 + [2] * 6
    tables = [materialise(recipe_from(cells, seed=s), 30) for s in (0, 1)]
    for t in tables:
        assert exact_total_weight(t) == 2400
        sums = {k[0]: v for k, v in exact_weight_sums(t, ["cell_id"]).items()}
        assert set(sums.values()) == {Fraction(100)}
    # without ties the seed does not matter
    base = make_cells(f_counts=[1000 + 37 * i for i in range(12)], m_counts=[2003 + 41 * i for i in range(12)])
    assert allocate(recipe_from(base, seed=0), 1000).cells == allocate(recipe_from(base, seed=99), 1000).cells


def test_too_few_records_raises_resolution_error_with_suggestion(recipe):
    with pytest.raises(ResolutionError) as ei:
        materialise(recipe, 10)
    err = ei.value
    assert isinstance(err, CapabilityGap)
    assert err.details["minimum_feasible_records"] == NONZERO
    five = next(s for s in err.details["suggested_coarsening"] if s["scheme"] == "5_year_age_bands")
    assert five["age_bands"] == [["Y_LT5", 0, 4], ["Y5-9", 5, 9], ["Y_GE10", 10, None]]
    # F,M x (Y_LT5, Y5-9, Y_GE10, UNK)
    assert five["minimum_feasible_records"] == 8
    assert five["feasible_for_requested"] is True


def test_record_limits(recipe):
    with pytest.raises(ResolutionError) as ei:
        allocate(recipe, MAX_RECORDS + 1)
    assert ei.value.details["maximum_records"] == MAX_RECORDS
    with pytest.raises(ValidationProblem):
        allocate(recipe, 0)
