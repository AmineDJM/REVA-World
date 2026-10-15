"""economic.io_price@1.0.0: hand-computed Leontief cost-push solution and plugin contract.

The 3-economy x 2-industry table is SYNTHETIC (tests/fixtures/io_tables/synthetic.py); the
expected values are derived by hand in that module's docstring.
"""

from __future__ import annotations

import importlib.util
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from reva.adapters.connectors import io_tables as it
from reva.kernel.errors import CapabilityGap, IntegrityFailure, ValidationProblem
from reva.kernel.ids import entity_id
from reva.models.economic import io_price as m
from reva.simulation import plugins
from reva.simulation.plugins import ResolvedInputs

_spec = importlib.util.spec_from_file_location(
    "io_synthetic", Path(__file__).resolve().parents[1] / "fixtures" / "io_tables" / "synthetic.py")
assert _spec and _spec.loader
syn = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(syn)
DIMS = it.TableDims(countries=syn.COUNTRIES, industries=syn.INDUSTRIES)


@pytest.fixture(scope="module")
def built() -> it.BuiltDataset:
    items = [it.parse_sdmx_csv(syn.body(g), dataset=syn.DATASET, year=syn.YEAR, origins=g, dims=DIMS)
             for g in syn.groups()]
    return it.build_dataset(items, dataset=syn.DATASET, year=syn.YEAR, origin="test_fixture", dims=DIMS)


def spec(**over: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "model_key": m.MODEL_KEY, "model_version": m.MODEL_VERSION, "reference_year": 2024,
        "countries": ["FRA", "DEU"], "horizon": {"periods": 1, "unit": "annual_accounting_period"},
        "intervention": {"kind": "set_exogenous_industry_price_change",
                         "shocks": [{"origin": "DEU", "industry": "B", "price_change_percent": "10"}]},
        "assumptions": {"pass_through_grid": ["0.5", "0.75", "1"]},
        "outputs": list(m.OUTPUT_GROUPS), "missing_input_policy": "fail", "fidelity": "L1"}
    doc.update(over)
    return doc


def inputs(built: it.BuiltDataset, doc: dict[str, Any], *, coverage: dict[str, Any] | None = None,
           missing: bool = False) -> ResolvedInputs:
    reqs = m.MODEL.requirements(doc)
    ds = None if missing else {(m.DATASET_INPUT, m.DATASET_SUBJECT): (
        built.table(), {"dataset_id": "ds-1", "coverage": coverage or built.manifest})}
    return ResolvedInputs.from_values(reqs, datasets=ds, release={"id": "rel-1"})


def results(built: it.BuiltDataset, fidelity: str = "L1", **over: Any) -> dict[tuple[str, str, str], Any]:
    doc = spec(**over)
    m.MODEL.validate(doc)
    compiled = m.MODEL.compile(inputs(built, doc), doc)
    rows = m.MODEL.compute(compiled, fidelity)
    return {(r.output_key, r.iso3, r.variant): r for r in rows}


def val(r: Any) -> Decimal:
    return Decimal(r.value["value"])


def q(f: Fraction) -> Decimal:
    """Exact rational -> the model's 6-decimal half-even rounding."""
    return (Decimal(f.numerator) / Decimal(f.denominator)).quantize(Decimal("0.000001"))


# ------------------------------------------------------------------ hand-computed answers
P_FR_C = Fraction(2, 97)  # θ = 1: 0.02 / 0.97
P_DE_C = Fraction(3, 10) * P_FR_C
P_FR_C_HALF = Fraction(1, 100) / Fraction(9925, 10000)  # θ = 0.5: 0.01 / 0.9925
P_DE_C_HALF = Fraction(15, 100) * P_FR_C_HALF


def test_industry_prices_match_the_hand_computed_solution(built: it.BuiltDataset) -> None:
    r = results(built)
    assert val(r[("industry_price_change_percent:C10-12", "FRA", "L1")]) == q(100 * P_FR_C) == Decimal("2.061856")
    assert val(r[("industry_price_change_percent:C10-12", "DEU", "L1")]) == q(100 * P_DE_C) == Decimal("0.618557")
    # the shocked industry carries exactly the exogenous change; untouched industries do not move
    shocked = r[("industry_price_change_percent:B", "DEU", "L1")]
    assert val(shocked) == Decimal("10.000000") and "exogenous" in shocked.applicability
    assert val(r[("industry_price_change_percent:B", "FRA", "L1")]) == Decimal("0.000000")
    assert r[("industry_price_change_percent:C10-12", "FRA", "L1")].subject_id == entity_id("FRA")


def test_household_basket_and_contributions_match_hand_computation(built: it.BuiltDataset) -> None:
    r = results(built)
    basket = (50 * P_FR_C + 30 * P_DE_C + 20 * Fraction(1, 10)) / 100
    assert val(r[(m.BASKET_KEY, "FRA", "L1")]) == q(100 * basket) == Decimal("3.216495")
    dom = r[("household_basket_contribution_domestic_products_percent", "FRA", "L1")]
    imp = r[("household_basket_contribution_imported_products_percent", "FRA", "L1")]
    assert val(dom) == q(100 * 50 * P_FR_C / 100) and val(imp) == q(100 * (30 * P_DE_C + 2) / 100)
    assert val(r[("household_basket_contribution_domestic_shocks_percent", "FRA", "L1")]) == 0
    assert val(r[("household_basket_contribution_foreign_shocks_percent", "FRA", "L1")]) == val(
        r[(m.BASKET_KEY, "FRA", "L1")])
    # DEU households buy nothing in the synthetic table: a typed not-applicable value, never 0 or a crash
    d = r[(m.BASKET_KEY, "DEU", "L1")]
    assert d.value == {"kind": "missing", "reason": "not_applicable"} and "no household" in d.applicability["note"]
    assert (m.BASKET_KEY, "DEU", m.SENSITIVITY_VARIANT) not in results(built, "L2")


def test_l2_pass_through_grid_and_one_sided_sensitivity_range(built: it.BuiltDataset) -> None:
    r = results(built, "L2")
    key = "industry_price_change_percent:C10-12"
    assert val(r[(key, "FRA", "L2:pass_through=0.5")]) == q(100 * P_FR_C_HALF) == Decimal("1.007557")
    assert val(r[(key, "DEU", "L2:pass_through=0.5")]) == q(100 * P_DE_C_HALF)
    assert val(r[(key, "FRA", "L2:pass_through=1")]) == val(r[(key, "FRA", "L1")])  # θ = 1 is L1 exactly
    rng = r[(key, "FRA", m.SENSITIVITY_VARIANT)]
    assert rng.value["kind"] == "sensitivity_range"
    assert Decimal(rng.value["lower"]) == q(100 * P_FR_C_HALF) and Decimal(rng.value["upper"]) == q(100 * P_FR_C)
    assert "one-sided" in rng.value["one_sidedness"]
    assert rng.uncertainty["kind"] == "sensitivity_range" and rng.uncertainty["level"] is None
    assert "not a confidence interval" in rng.uncertainty["description"]
    # every L1 row carries the label; the shocked industry is not scaled by θ
    assert r[(key, "FRA", "L1")].applicability["label"] == m.LABEL
    assert "not an inflation forecast" in m.LABEL and "full pass-through" in m.LABEL
    assert val(r[("industry_price_change_percent:B", "DEU", "L2:pass_through=0.5")]) == Decimal("10.000000")


def test_zero_output_industry_is_not_applicable_never_zero(built: it.BuiltDataset) -> None:
    doc = spec(countries=["FRA"], intervention={"kind": "set_exogenous_industry_price_change", "shocks": [
        {"origin": "FIGARO_ROW", "industry": "B", "price_change_percent": "50"}]})
    compiled = m.MODEL.compile(inputs(built, doc), doc)
    assert compiled.arrays.diagnostics["zero_output_industries"] == 1
    arr = compiled.arrays
    sol = m.solve(arr, compiled.spec.shocks, Decimal(1))
    k = arr.index(arr.keys.index("FIGARO_ROW"), "C10-12")
    assert np.isnan(sol.dp[k])
    # nobody buys from the rest of the world: no propagation
    assert np.nansum(np.abs(np.delete(sol.dp, [arr.index(2, "B"), k]))) == 0


def test_linearity_and_decomposition_by_shock_origin(built: it.BuiltDataset) -> None:
    both = [{"origin": "DEU", "industry": "B", "price_change_percent": "10"},
            {"origin": "FRA", "industry": "B", "price_change_percent": "-20"}]
    doc = spec(intervention={"kind": "set_exogenous_industry_price_change", "shocks": both}, countries=["FRA"])
    compiled = m.MODEL.compile(inputs(built, doc), doc)
    sol = m.solve(compiled.arrays, compiled.spec.shocks, Decimal("0.75"))
    total = sol.dp
    parts = sol.dp_by_origin["DEU"] + sol.dp_by_origin["FRA"]
    assert np.allclose(np.nan_to_num(total), np.nan_to_num(parts), rtol=0, atol=1e-15)
    b = m.basket(compiled.arrays, sol, compiled.arrays.keys.index("FRA"))
    assert b.domestic_shocks + b.foreign_shocks == pytest.approx(b.total, abs=1e-15)
    assert b.domestic_products + b.imported_products == b.total
    rows = m.MODEL.compute(compiled, "L2")
    rng = next(r for r in rows if r.output_key == m.BASKET_KEY and r.variant == m.SENSITIVITY_VARIANT)
    assert "no monotonicity" in rng.value["one_sidedness"]  # mixed-sign shocks


def test_residual_check_is_enforced(built: it.BuiltDataset, monkeypatch: pytest.MonkeyPatch) -> None:
    doc = spec()
    compiled = m.MODEL.compile(inputs(built, doc), doc)
    monkeypatch.setattr(m, "RESIDUAL_TOLERANCE", -1.0)
    with pytest.raises(IntegrityFailure, match="residual"):
        m.solve(compiled.arrays, compiled.spec.shocks, Decimal(1))


def test_productivity_bound_and_rounding() -> None:
    a = np.array([[0.2, 0.3], [0.1, 0.4]])
    rho = max(abs(np.linalg.eigvals(a)))
    bound = m.spectral_radius_upper_bound(a)
    assert rho <= bound < rho + 1e-6
    assert m.spectral_radius_upper_bound(np.array([[0.6, 0.6], [0.6, 0.6]])) >= 1.2 - 1e-9
    assert m.to_result_decimal(-1e-12) == Decimal("0.000000") and str(m.to_result_decimal(-1e-12)) == "0.000000"
    assert m.to_result_decimal(0.0000005) == Decimal("0.000000")  # half-even
    assert m.to_result_decimal(0.0000015) == Decimal("0.000002")


def test_validation_envelopes_and_coverage() -> None:
    m.MODEL.validate(spec())
    gaps = [
        spec(intervention={"kind": "set_exogenous_industry_price_change",
                           "shocks": [{"origin": "DEU", "industry": "C24", "price_change_percent": "10"}]}),
        spec(intervention={"kind": "set_exogenous_industry_price_change",
                           "shocks": [{"origin": "DEU", "industry": "B", "price_change_percent": "250"}]}),
        spec(intervention={"kind": "set_exogenous_industry_price_change",
                           "shocks": [{"origin": "DZA", "industry": "B", "price_change_percent": "10"}]}),
        spec(countries=["DZA"]),
        spec(horizon={"periods": 5, "unit": "annual_accounting_period"}),
        spec(outputs=["inflation_forecast"]),
        spec(missing_input_policy="skip_missing_countries"),
    ]
    for doc in gaps:
        with pytest.raises(CapabilityGap):
            m.MODEL.validate(doc)
    for doc in (spec(assumptions={"pass_through_grid": [0]}), spec(assumptions={"pass_through_grid": [1.5]}),
                spec(intervention={"kind": "other", "shocks": []}),
                spec(intervention={"kind": "set_exogenous_industry_price_change", "shocks": [
                    {"origin": "DEU", "industry": "B", "price_change_percent": "10"},
                    {"origin": "DEU", "industry": "B", "price_change_percent": "5"}]})):
        with pytest.raises(ValidationProblem):
            m.MODEL.validate(doc)
    with pytest.raises(CapabilityGap):  # -100 % (a free good) is outside the envelope
        m.parse_spec(spec(intervention={"kind": "set_exogenous_industry_price_change", "shocks": [
            {"origin": "DEU", "industry": "B", "price_change_percent": "-100"}]}))


def test_missing_dataset_and_year_mismatch_are_typed(built: it.BuiltDataset) -> None:
    doc = spec()
    compiled = m.MODEL.compile(inputs(built, doc, missing=True), doc)
    assert compiled.missing and compiled.missing[0]["code"] == "missing_input"
    assert compiled.missing[0]["kind"] == "dataset" and compiled.missing[0]["concept"] == m.DATASET_CONCEPT
    doc23 = spec(reference_year=2023)
    compiled = m.MODEL.compile(inputs(built, doc23), doc23)
    assert compiled.incompatible and compiled.incompatible[0]["table_year"] == 2024
    failed = {**built.manifest, "identity_report": {"passed": False}}
    with pytest.raises(IntegrityFailure, match="identity"):
        m.MODEL.compile(inputs(built, doc, coverage=failed), doc)


def test_plugin_registration_manifest_and_schema() -> None:
    assert plugins.get(m.MODEL_KEY) is m.MODEL
    assert isinstance(m.MODEL, plugins.SimulationModel)
    man = m.MODEL.manifest()
    assert man["review_status"] == "maintainer_review_pending" and man["label"] == m.LABEL
    assert man["implementation_digest"].startswith("sha256:")
    [req] = m.MODEL.requirements(spec())
    assert (req.kind, req.concept, req.subject, req.years) == ("dataset", m.DATASET_CONCEPT, "WORLD", (2024,))
    sch = m.MODEL.spec_schema()
    assert set(sch) <= {"intervention", "assumptions", "outputs", "countries", "reference_year", "horizon",
                        "missing_input_policy"}
    assert "FIGARO_ROW" not in sch["countries"]["items"]["enum"]
    assert "B" in m.SHOCKABLE_INDUSTRIES and "broader than crude" in m.SHOCKABLE_INDUSTRIES["B"].lower()
    assert m.MODEL.fidelities == frozenset({"L1", "L2"})
    assert m.REGISTRY_ENTRY["approval_status"] == "candidate"
