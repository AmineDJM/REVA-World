"""R3-03 / R3-19: the L2 sensitivity range is a separate row and never displaces L1's uncertainty.

Uses the review counter-example (M=100, X=40, G=1000, p=1.25, grid without ε=(0,0)),
for which a grid-only min/max excludes the L1 exposure of 1.5% of GDP.
"""

from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from reva.kernel.uncertainty import UncertaintyKind, UncertaintySpec, UncertaintyStatus
from reva.models.economic import crude_exposure as ce
from reva.simulation import engine
from reva.simulation.spec import CONTRACTS, validate_scenario_spec

GRID_WITHOUT_ZERO = [
    {"import_elasticity": -0.5, "export_elasticity": 0.5, "basis": "scenario_assumption"},
    {"import_elasticity": -0.2, "export_elasticity": 0.3, "basis": "scenario_assumption"},
]


def _compiled(grid: list[dict]) -> SimpleNamespace:
    raw = json.loads((CONTRACTS / "scenario.example.json").read_text())
    raw.update(countries=["FRA"], baseline_release_id=str(uuid4()))
    if grid:
        raw.update(
            fidelity="L2",
            assumptions={
                "quantity_response": "elasticity_sensitivity",
                "elasticity_grid": grid,
                "no_general_equilibrium": True,
            },
        )
    spec = validate_scenario_spec(raw)
    country = SimpleNamespace(
        iso3="FRA",
        subject_id=uuid4(),
        status="ready",
        inputs=None,
        values=(Decimal(100), Decimal(40), Decimal(1000)),
        input_refs=lambda: {},
    )
    change = SimpleNamespace(import_multiplier=Decimal("1.25"), export_multiplier=Decimal("1.25"))
    return SimpleNamespace(spec=spec, change=change, countries=[country])


def _rows(grid: list[dict]) -> dict[tuple[str, str], engine.ResultRow]:
    return {(r.output_key, r.variant): r for r in engine.compute(_compiled(grid), "L2")}  # type: ignore[arg-type]


def test_l1_row_keeps_unquantified_uncertainty_in_l2_run():
    rows = _rows(GRID_WITHOUT_ZERO)
    for k in ce.OUTPUT_KEYS:
        u = UncertaintySpec.model_validate(rows[(k, "L1")].uncertainty)
        assert u.status == UncertaintyStatus.unquantified and u.kind == UncertaintyKind.unknown
        assert rows[(k, "L1")].value["kind"] == "decimal"


def test_range_row_is_separate_levelless_assumption_sensitivity():
    rows = _rows(GRID_WITHOUT_ZERO)
    for k in ce.OUTPUT_KEYS:
        r = rows[(k, engine.SENSITIVITY_VARIANT)]
        assert r.value["kind"] == "sensitivity_range"
        assert r.value["statement"] == "assumption sensitivity, not a confidence interval"
        assert r.value["unit_ref"] == ce.OUTPUT_UNITS[k]
        u = UncertaintySpec.model_validate(r.uncertainty)
        assert u.kind == UncertaintyKind.sensitivity_range and u.level is None
        assert u.status == UncertaintyStatus.quantified and not u.quantifies_measurement
        assert "not a confidence interval" in u.description
        assert (u.lower, u.upper) == (r.value["lower"], r.value["upper"])
        # conditioning = every grid point plus the always-included fixed-quantity point
        assert len(u.conditioning) == len(GRID_WITHOUT_ZERO) + 1
        assert u.conditioning[-1].startswith("fixed_quantity_point")


def test_range_contains_l1_even_when_grid_omits_zero():
    rows = _rows(GRID_WITHOUT_ZERO)
    k = "exposure_percent_baseline_gdp"
    l1 = Decimal(rows[(k, "L1")].value["value"])
    assert l1 == Decimal("1.5")
    # grid-only range from the review: [-0.410, 0.608], which excludes 1.5
    grid_only = [
        ce.exposure_l2(100, 40, 1000, 1.25, 1.25, g["import_elasticity"], g["export_elasticity"])
        for g in GRID_WITHOUT_ZERO
    ]
    lo_g, hi_g = ce.sensitivity_range(grid_only, k)
    assert not lo_g <= l1 <= hi_g
    r = rows[(k, engine.SENSITIVITY_VARIANT)].value
    lo, hi = Decimal(r["lower"]), Decimal(r["upper"])
    assert lo <= l1 <= hi and r["includes_fixed_quantity_point"] is True
    assert hi == l1 and lo == lo_g
    for key in ce.OUTPUT_KEYS:
        rr = rows[(key, engine.SENSITIVITY_VARIANT)].value
        assert Decimal(rr["lower"]) <= Decimal(rows[(key, "L1")].value["value"]) <= Decimal(rr["upper"])


def test_grid_variants_unchanged_and_zero_point_not_duplicated():
    grid = [*GRID_WITHOUT_ZERO, {"import_elasticity": 0.0, "export_elasticity": 0.0, "basis": "scenario_assumption"}]
    rows = _rows(grid)
    variants = {v for (_, v) in rows}
    assert variants == {"L1", engine.SENSITIVITY_VARIANT, "L2:eM=-0.5;eX=0.5", "L2:eM=-0.2;eX=0.3", "L2:eM=0;eX=0"}
    for k in ce.OUTPUT_KEYS:
        assert rows[(k, "L2:eM=0;eX=0")].value == rows[(k, "L1")].value
        assert rows[(k, engine.SENSITIVITY_VARIANT)].value["evaluated_points"] == 3


def test_l1_run_has_no_range_row():
    compiled = _compiled([])
    rows = engine.compute(compiled, "L1")  # type: ignore[arg-type]
    assert {r.variant for r in rows} == {"L1"}


def test_kernel_sensitivity_range_needs_both_bounds():
    with pytest.raises(ValidationError):
        UncertaintySpec(status="quantified", kind="sensitivity_range", description="d", method_version="m@1", lower="1")
    s = UncertaintySpec.sensitivity("1", "2", "m@1", "grid", conditioning=("eM=-0.5",))
    assert s.is_assumption_sensitivity and not s.quantifies_measurement and s.conditioning == ("eM=-0.5",)
    pi = UncertaintySpec(
        status="quantified",
        kind="prediction_interval",
        description="d",
        method_version="m@1",
        level=0.8,
        lower="1",
        upper="2",
    )
    assert pi.quantifies_measurement and not pi.is_assumption_sensitivity
    assert not UncertaintySpec.unknown().quantifies_measurement
