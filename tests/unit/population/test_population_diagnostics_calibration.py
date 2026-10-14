from __future__ import annotations

import json

import numpy as np
import pyarrow as pa
import pytest

from reva.population import diagnose, materialise, rake

from .conftest import make_cells, recipe_from


def test_diagnostics_of_fresh_materialisation(recipe):
    t = materialise(recipe, 1000)
    d = diagnose(t, recipe)
    json.dumps(d, allow_nan=False)
    for margin in ("age_margin", "sex_margin", "joint_age_sex"):
        assert d["calibration_error"][margin]["max_abs_error"] == "0"
        assert d["calibration_error"][margin]["max_rel_error"] == "0"
    assert d["structural_zero_violations"]["ok"] is True
    assert d["total_weight"] == str(recipe.total_count)
    assert 1 <= d["effective_sample_size"] <= 1000
    assert 0 < d["max_weight_share"] < 1
    assert 0 <= d["weight_concentration"]["gini_record_weights"] < 1
    names = " ".join(a["association"] for a in d["unsupported_associations"])
    assert "income" in names and "household" in names


def test_equal_weights_give_full_ess_and_zero_gini():
    equal = [50] * 12
    r = recipe_from(make_cells(f_counts=equal, m_counts=equal))
    d = diagnose(materialise(r, 240), r)  # 10 records per cell, all weights 5
    assert d["effective_sample_size"] == pytest.approx(240)
    assert d["weight_concentration"]["gini_record_weights"] == pytest.approx(0, abs=1e-12)
    assert d["max_weight_share"] == pytest.approx(1 / 240)


def test_records_moved_into_zero_cell_are_flagged(recipe):
    t = materialise(recipe, 100)
    cells = t.column("cell_id").to_pylist()
    i = cells.index("M|Y4")
    cells[i] = "M|Y3"  # structural zero cell
    tampered = t.set_column(t.schema.get_field_index("cell_id"), "cell_id", pa.array(cells))
    d = diagnose(tampered, recipe)
    assert d["structural_zero_violations"]["records_in_observed_zero_cells"] == 1
    assert d["structural_zero_violations"]["ok"] is False
    joint = d["calibration_error"]["joint_age_sex"]
    assert joint["max_abs_error"] != "0"
    assert "M|Y3" in joint["categories_with_undefined_relative_error"]


def test_ipf_converges_on_compatible_margins(recipe):
    t = materialise(recipe, 2000)
    ages = t.column("age_code").to_pylist()
    sexes = t.column("sex_code").to_pylist()
    age_m: dict[str, int] = {}
    sex_m: dict[str, int] = {}
    for c in recipe.leaf_cells:
        age_m[c.age_code] = age_m.get(c.age_code, 0) + c.count
        sex_m[c.sex_code] = sex_m.get(c.sex_code, 0) + c.count
    start = np.ones(t.num_rows)  # deliberately uncalibrated
    res = rake(start, {"age": ages, "sex": sexes}, {"age": age_m, "sex": sex_m})
    assert res.feasible and res.converged
    assert res.iterations >= 2
    w = res.weights
    for s, target in sex_m.items():
        assert w[np.array(sexes) == s].sum() == pytest.approx(target, rel=1e-9)
    for a, target in age_m.items():
        assert w[np.array(ages) == a].sum() == pytest.approx(target, rel=1e-9)
    json.dumps(res.to_dict(), allow_nan=False)


def test_ipf_infeasible_when_totals_disagree():
    cats = {"a": ["x", "y", "x"], "b": ["p", "p", "q"]}
    margins = {"a": {"x": 10, "y": 5}, "b": {"p": 9, "q": 7}}
    res = rake([1, 1, 1], cats, margins)
    assert not res.feasible and not res.converged and res.weights is None
    assert any(p["kind"] == "margin_totals_disagree" for p in res.problems)
    assert res.targets["b"] == {"p": 9.0, "q": 7.0}  # targets echoed unaltered


def test_ipf_infeasible_positive_target_without_records():
    cats = {"a": ["x", "x"]}
    res = rake([1, 1], cats, {"a": {"x": 3, "y": 2}})
    assert not res.feasible
    assert {"kind": "positive_target_without_support", "dimension": "a", "category": "y"} in (
        res.problems
    )


def test_ipf_infeasible_records_without_target():
    res = rake([1, 1], {"a": ["x", "z"]}, {"a": {"x": 2}})
    assert not res.feasible
    assert res.problems[0]["kind"] == "records_in_category_without_target"


def test_ipf_reports_non_convergence_for_jointly_incompatible_support():
    # records only at (x,p) and (y,q); margins require mass at x and q only -> impossible
    cats = {"a": ["x", "y"], "b": ["p", "q"]}
    margins = {"a": {"x": 2, "y": 0}, "b": {"p": 0, "q": 2}}
    res = rake([1.0, 1.0], cats, margins, max_iterations=50)
    assert res.feasible and not res.converged
    assert res.problems[-1]["kind"] == "not_converged"
