from __future__ import annotations

import pytest

from reva.kernel.errors import CapabilityGap, ValidationProblem
from reva.population import SourceCell, UnsupportedAttribute, parse_age_code
from reva.population.recipe import FORBIDDEN_ATTRIBUTES

from .conftest import F_COUNTS, M_COUNTS, make_cells, recipe_from


def test_only_disjoint_leaves_are_counted(recipe):
    assert len(recipe.leaf_cells) == 24
    assert {c.sex_code for c in recipe.leaf_cells} == {"F", "M"}
    assert recipe.total_count == sum(F_COUNTS) + sum(M_COUNTS)
    leaf_keys = {c.source_key for c in recipe.leaf_cells}
    excluded_keys = {c.source_key for c in recipe.excluded_cells}
    assert leaf_keys.isdisjoint(excluded_keys)
    reasons = {c.reason for c in recipe.excluded_cells}
    assert reasons == {
        "grand_total",
        "age_total",
        "sex_total",
        "age_band_subtotal",
        "age_band_subtotal_both_sexes",
    }
    # every total/subtotal produced a consistency check, all consistent in the clean fixture
    assert len(recipe.consistency) == len(recipe.excluded_cells)
    assert all(c.status == "consistent" for c in recipe.consistency)
    assert recipe.is_internally_consistent


def test_inconsistent_total_is_reported_not_corrected():
    r = recipe_from(make_cells(total_offsets={"F|TOTAL": 3}))
    check = next(c for c in r.consistency if c.sex_code == "F" and c.age_code == "TOTAL")
    assert check.status == "inconsistent"
    assert check.reported == sum(F_COUNTS) + 3
    assert check.leaf_sum == sum(F_COUNTS)
    assert check.difference == -3 and check.absolute_difference == 3
    assert float(check.relative_difference) == pytest.approx(3 / (sum(F_COUNTS) + 3))
    # source leaves are not altered to match the reported total
    assert r.total_count == sum(F_COUNTS) + sum(M_COUNTS)
    assert not r.is_internally_consistent
    # the grand total (T|TOTAL) is unaffected
    grand = next(c for c in r.consistency if c.sex_code == "T" and c.age_code == "TOTAL")
    assert grand.status == "consistent"


def test_open_band_subtotal_includes_top_coded_group(recipe):
    ge5 = next(c for c in recipe.consistency if c.sex_code == "F" and c.age_code == "Y_GE5")
    assert ge5.leaf_sum == sum(F_COUNTS[5:11])
    assert recipe.open_age_lower_bound == 10


def test_band_straddling_open_group_is_not_computable():
    cells = [*make_cells(), SourceCell("Y5-14", "F", 1, (), "k")]
    r = recipe_from(cells)
    chk = next(c for c in r.consistency if c.age_code == "Y5-14")
    assert chk.status == "not_computable"
    assert "Y_OPEN" in chk.note


def test_unknown_and_top_coded_retained_explicitly(recipe):
    unk = recipe.leaf("F|UNK")
    top = recipe.leaf("M|Y_OPEN")
    assert unk.is_unknown_age and unk.age_years is None and unk.count == F_COUNTS[-1]
    assert top.is_top_coded and top.age_years is None and top.count == M_COUNTS[-2]
    assert recipe.leaf("F|Y_LT1").age_years == 0


def test_observed_zero_is_structural_zero_not_missing(recipe):
    assert recipe.structural_zeros == ("M|Y3",)
    assert recipe.leaf("M|Y3").count == 0
    assert recipe.missing_cells == ()


def test_missing_count_raises_by_default():
    f = list(F_COUNTS)
    f[4] = None  # F|Y4 is ':' in the source
    with pytest.raises(ValidationProblem) as ei:
        recipe_from(make_cells(f_counts=f))
    assert [m["cell_id"] for m in ei.value.details["missing_cells"]] == ["F|Y4"]


def test_missing_count_excluded_and_reported_when_allowed():
    f = list(F_COUNTS)
    f[4] = None
    r = recipe_from(make_cells(f_counts=f), allow_missing=True)
    assert [(m.cell_id, m.reason) for m in r.missing_cells] == [("F|Y4", "count_missing_in_source")]
    assert "F|Y4" not in {c.cell_id for c in r.leaf_cells}
    assert r.total_count == sum(v for v in f if v is not None) + sum(M_COUNTS)
    # checks that need the missing leaf cannot be computed (missing is not zero)
    f_total = next(c for c in r.consistency if c.sex_code == "F" and c.age_code == "TOTAL")
    assert f_total.status == "not_computable" and "F|Y4" in f_total.note
    m_total = next(c for c in r.consistency if c.sex_code == "M" and c.age_code == "TOTAL")
    assert m_total.status == "consistent"


def test_leaf_absent_from_source_is_missing():
    with pytest.raises(ValidationProblem):
        recipe_from(make_cells(drop=[("M", "Y8")]))
    r = recipe_from(make_cells(drop=[("M", "Y8")]), allow_missing=True)
    assert [(m.cell_id, m.reason) for m in r.missing_cells] == [("M|Y8", "absent_from_source")]


@pytest.mark.parametrize(
    "attr",
    sorted(
        FORBIDDEN_ATTRIBUTES
        & {
            "income",
            "religion",
            "sexual_orientation",
            "political_opinion",
            "health",
            "ethnicity",
            "household",
        }
    ),
)
def test_forbidden_attributes_rejected(cells, attr):
    with pytest.raises(UnsupportedAttribute) as ei:
        recipe_from(cells, attributes=("age", "sex", attr))
    assert isinstance(ei.value, CapabilityGap)
    assert ei.value.details["forbidden"] == [attr]


def test_any_other_attribute_rejected(cells):
    with pytest.raises(UnsupportedAttribute) as ei:
        recipe_from(cells, attributes=("age", "sex", "occupation"))
    assert ei.value.details["requested"] == ["occupation"]
    assert ei.value.details["forbidden"] == []


def test_flags_and_recorded_sex_definition_preserved():
    r = recipe_from(make_cells(flags={("F", "Y_LT1"): ("p",), ("M", "UNK"): ("e", "b")}))
    assert r.leaf("F|Y_LT1").flags == ("p",)
    assert r.flag_summary == {"b": 1, "e": 1, "p": 1}
    assert "not gender" in r.sex_definition
    assert r.supported_attributes == ("age", "sex")


def test_invalid_inputs_raise(cells):
    with pytest.raises(ValidationProblem):
        recipe_from([*cells, SourceCell("Y_FOO", "F", 1, (), "x")])
    with pytest.raises(ValidationProblem):
        recipe_from([*cells, SourceCell("Y1", "X", 1, (), "x")])
    with pytest.raises(ValidationProblem):
        recipe_from([*cells, SourceCell("Y1", "F", 1, (), "dup")])
    with pytest.raises(ValidationProblem):
        SourceCell("Y1", "F", -1, (), "neg")


def test_digest_is_deterministic_and_seed_sensitive(cells):
    a, b = recipe_from(cells, seed=1), recipe_from(list(reversed(cells)), seed=1)
    assert a.digest() == b.digest()
    assert a.digest() != recipe_from(cells, seed=2).digest()


def test_parse_age_code_bands():
    assert (parse_age_code("Y_LT5").lo, parse_age_code("Y_LT5").hi) == (0, 4)
    assert (parse_age_code("Y15-64").lo, parse_age_code("Y15-64").hi) == (15, 64)
    assert parse_age_code("Y_GE65").hi is None
    assert parse_age_code("Y_GT84").lo == 85
    assert parse_age_code("Y99").is_leaf and not parse_age_code("Y5-9").is_leaf
