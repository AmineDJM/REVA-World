"""Invariant 26: no household/network coarsening without task-relevant diagnostics.

V1 has no household synthesis (a typed reserved capability) and no network
coarsening; the only coarsening is age-band × recorded-sex aggregation of the
synthetic population, and every such step carries a compression certificate
with its task scope, loss metric, measured error, per-subgroup and tail errors,
exact conservation checks and expiry conditions. A lossy step reports its error
rather than hiding it; the population job persists the certificate.
"""

from __future__ import annotations

import inspect
from datetime import date
from fractions import Fraction

import pyarrow as pa
import pytest

from reva.compression import coarsen, refine
from reva.kernel.errors import CapabilityGap
from reva.population import SourceCell, build_recipe, materialise, standard_age_bands

AGES = ["Y_LT1", *[f"Y{k}" for k in range(1, 10)], "Y_OPEN", "UNK"]
F = [101, 98, 97, 103, 99, 100, 95, 90, 92, 91, 340, 7]
M = [105, 101, 99, 4, 100, 102, 97, 94, 90, 88, 250, 5]


def _recipe():
    cells = [
        SourceCell(a, s, c, (), f"synthetic.{s}.{a}")
        for s, counts in (("F", F), ("M", M))
        for a, c in zip(AGES, counts, strict=True)
    ]
    cells += [SourceCell("TOTAL", s, sum(v), (), f"synthetic.{s}.TOTAL") for s, v in (("F", F), ("M", M))]
    return build_recipe(
        cells,
        geography="XX-synthetic",
        reference_date=date(2024, 1, 1),
        source_ref={"dataset": "synthetic", "fixture": "invariant-26"},
        seed=11,
    )


def _assert_task_relevant(cert, operation: str) -> None:
    d = cert.to_dict()
    assert d["operation"] == operation
    assert d["task_scope"] and d["loss_metric"] and d["measured_error"] is not None
    assert set(d["subgroup_errors"]) == {"F", "M"}
    tails = set(d["tail_errors"])
    assert "unknown_age" in tails and any("top_coded" in t for t in tails), tails
    assert d["conservation"] and all("max_abs_diff" in c for c in d["conservation"])
    assert d["expiry_conditions"], "a certificate must state when it stops being valid"
    assert d["reference"] != "unknown"


def test_every_coarsening_carries_a_task_scoped_certificate() -> None:
    recipe = _recipe()
    fine = materialise(recipe, 400)
    res = coarsen(fine, standard_age_bands(5, 10, 9), task_scope="invariant-26 age-band reporting")
    assert res.certificate.task_scope == "invariant-26 age-band reporting"
    _assert_task_relevant(res.certificate, "coarsen")
    assert res.certificate.conserved


def test_lossy_refinement_reports_its_error_instead_of_hiding_it() -> None:
    recipe = _recipe()
    coarse = coarsen(materialise(recipe, 300), standard_age_bands(5, 10, 9)).table
    nums = coarse.column("weight_num").to_pylist()
    nums[coarse.column("cell_id").to_pylist().index("F|Y_LT5")] *= 3
    adjusted = coarse.set_column(
        coarse.schema.get_field_index("weight_num"),
        "weight_num",
        pa.array(nums, type=coarse.schema.field("weight_num").type),
    )
    res = refine(adjusted, recipe, "age")
    _assert_task_relevant(res.certificate, "refine")
    assert Fraction(res.certificate.measured_error) > 0
    assert Fraction(res.certificate.subgroup_errors["F"]) > 0


def test_household_coarsening_is_a_typed_reserved_capability() -> None:
    from reva.extensions import population as hh

    spec = hh.HouseholdSynthesisSpec("XX residents", "2024", ("synthetic",), (), ("one head",), "bounds")
    with pytest.raises(CapabilityGap) as ei:
        hh.synthesize_households(spec, seed=1)
    assert ei.value.details["status"] == "reserved"


def test_population_job_persists_the_certificate_with_each_step() -> None:
    from reva.population import service

    src = inspect.getsource(service)
    assert "model.compression_certificate" in src and "certificate.to_dict()" in src
