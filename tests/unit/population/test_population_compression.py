from __future__ import annotations

import json
from fractions import Fraction

import pyarrow as pa
import pytest

from reva.compression import UnsupportedRefinement, coarsen, refine
from reva.kernel.errors import CapabilityGap, IncompatibleSemantics, ValidationProblem
from reva.population import (
    diagnose,
    exact_total_weight,
    exact_weight_sums,
    materialise,
    standard_age_bands,
)

from .conftest import F_COUNTS, M_COUNTS, make_cells, recipe_from

BANDS5 = standard_age_bands(5, 10, 9)


def _by(table, key):
    return {k[0]: v for k, v in exact_weight_sums(table, [key]).items()}


@pytest.mark.parametrize("n", [23, 501, 5000])
def test_coarsen_conserves_exactly(recipe, n):
    fine = materialise(recipe, n)
    res = coarsen(fine, BANDS5)
    t = res.table
    assert t.num_rows == 8
    assert _by(t, "sex_code") == _by(fine, "sex_code")
    assert exact_total_weight(t) == recipe.total_count
    coarse = _by(t, "cell_id")
    assert coarse["F|Y_LT5"] == sum(F_COUNTS[0:5])
    assert coarse["M|Y_LT5"] == sum(M_COUNTS[0:5])  # includes the zero cell, adds nothing
    assert coarse["F|Y_GE10"] == F_COUNTS[10]  # Y_OPEN kept whole in the open band
    assert coarse["F|UNK"] == F_COUNTS[11]  # UNK never merged into a band
    assert res.mapping["F|Y_GE10"] == ("F|Y_OPEN",)
    assert res.mapping["M|Y_LT5"] == ("M|Y_LT1", "M|Y1", "M|Y2", "M|Y4")
    rows = {r["cell_id"]: r for r in t.to_pylist()}
    assert rows["F|Y_GE10"]["includes_top_coded"] and rows["F|Y_GE10"]["age_hi"] is None
    assert rows["M|UNK"]["is_unknown_age"] and rows["M|UNK"]["age_lo"] is None
    cert = res.certificate
    assert cert.operation == "coarsen" and cert.conserved and cert.within_epsilon
    assert cert.reference == recipe.digest()
    assert cert.mapping_digest == res.mapping_digest
    assert cert.measured_error == "0"
    assert set(cert.subgroup_errors) == {"F", "M"}
    json.dumps(cert.to_dict(), allow_nan=False)


def test_coarsen_then_refine_round_trip(recipe):
    fine = materialise(recipe, 777)
    coarse = coarsen(fine, BANDS5).table
    res = refine(coarse, recipe, "age")
    out = res.table
    assert exact_total_weight(out) == exact_total_weight(fine) == recipe.total_count
    assert _by(out, "sex_code") == {"F": sum(F_COUNTS), "M": sum(M_COUNTS)}
    # refining with the same recipe restores each leaf count exactly
    assert _by(out, "cell_id") == {
        c.cell_id: Fraction(c.count) for c in recipe.leaf_cells if c.count > 0
    }
    assert "M|Y3" not in _by(out, "cell_id")
    cert = res.certificate
    assert cert.operation == "refine" and cert.conserved
    assert cert.measured_error == "0" and cert.tail_errors == {"top_coded": "0", "unknown_age": "0"}
    d = diagnose(out, recipe)
    assert d["calibration_error"]["joint_age_sex"]["max_abs_error"] == "0"
    assert d["structural_zero_violations"]["ok"]


def test_refine_after_reweighting_conserves_coarse_totals_but_reports_error(recipe):
    coarse = coarsen(materialise(recipe, 100), BANDS5).table
    # double the F|Y_LT5 coarse weight (e.g. a scenario adjustment)
    nums = coarse.column("weight_num").to_pylist()
    i = coarse.column("cell_id").to_pylist().index("F|Y_LT5")
    nums[i] *= 2
    adjusted = coarse.set_column(
        coarse.schema.get_field_index("weight_num"),
        coarse.schema.field("weight_num"),
        pa.array(nums, pa.int64()),
    )
    res = refine(adjusted, recipe, "age")
    assert exact_total_weight(res.table) == exact_total_weight(adjusted)
    assert res.certificate.conserved
    assert float(res.certificate.measured_error) == max(F_COUNTS[0:5])
    assert res.certificate.subgroup_errors["M"] == "0"
    assert not res.certificate.within_epsilon


@pytest.mark.parametrize("dim", ["income", "household", "religion", "occupation"])
def test_refine_along_unsupported_dimension_raises(recipe, dim):
    coarse = coarsen(materialise(recipe, 100), BANDS5).table
    with pytest.raises(UnsupportedRefinement) as ei:
        refine(coarse, recipe, dim)
    assert isinstance(ei.value, CapabilityGap)
    assert ei.value.details["report"]["status"] == "unsupported"
    assert ei.value.details["report"]["new_detail"] == dim
    assert "unsupported" in ei.value.message


def test_refine_requires_matching_recipe(recipe):
    coarse = coarsen(materialise(recipe, 100), BANDS5).table
    other = recipe_from(make_cells(), seed=recipe.seed + 1)
    with pytest.raises(IncompatibleSemantics):
        refine(coarse, other, "age")


def test_coarsen_rejects_bands_that_split_or_miss_categories(recipe):
    fine = materialise(recipe, 100)
    with pytest.raises(ValidationProblem):  # ages 5..9 not covered
        coarsen(fine, [("Y_LT5", 0, 4), ("Y_GE10", 10, None)])
    with pytest.raises(ValidationProblem):  # open band above Y_OPEN lower bound (10)
        coarsen(fine, [("Y0-9", 0, 9), ("Y_GE12", 12, None)])
    with pytest.raises(ValidationProblem):  # no open band for Y_OPEN
        coarsen(fine, [("Y0-9", 0, 9), ("Y10-20", 10, 20)])
    with pytest.raises(ValidationProblem):  # overlapping bands
        coarsen(fine, [("A", 0, 5), ("B", 5, None)])


def test_coarsen_with_broader_open_band_absorbs_top_group(recipe):
    res = coarsen(materialise(recipe, 300), [("Y_LT5", 0, 4), ("Y_GE5", 5, None)])
    coarse = _by(res.table, "cell_id")
    assert coarse["F|Y_GE5"] == sum(F_COUNTS[5:11])
    back = refine(res.table, recipe, "age")
    assert back.certificate.measured_error == "0"
