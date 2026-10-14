"""Unit registry: dimensional analysis, currency basis, mass/volume refusal."""

from __future__ import annotations

from decimal import Decimal

import pytest

from reva.kernel.errors import IncompatibleSemantics, MissingInput
from reva.kernel.values import MeasurementBasis, ScalarDecimal
from reva.ontology import units
from reva.ontology.units import (
    CurrencyTransform,
    DensityConvention,
    UnknownUnit,
    apply_currency_transform,
    canonical_unit_ref,
    convert,
    currency_basis_issues,
    dimensionality,
    mass_to_volume,
    validate_observation_units,
    volume_to_mass,
)


def test_no_unit_inference_from_field_names_exists() -> None:
    assert not hasattr(units, "unit_from_field_name")
    assert not any("field_name" in name for name in dir(units))


@pytest.mark.parametrize("ref", ["unit.usd", "unit.persons", "unit.kg", "percent_of_baseline_gdp", "ratio"])
def test_required_refs_are_registered(ref: str) -> None:
    assert units.is_known_unit(ref)
    assert canonical_unit_ref(ref).startswith("unit.")


def test_physical_conversion_is_exact_decimal() -> None:
    assert convert("2.5", "unit.tonne", "unit.kg") == Decimal("2500")
    # 1 oil barrel = 42 US gal = 0.158987294928 m3 exactly
    assert convert("1", "unit.barrel", "unit.m3") == Decimal("0.158987294928")
    assert convert("12.5", "unit.percent_of_baseline_gdp", "unit.ratio_to_baseline_gdp") == Decimal("0.125")


def test_dimensions_are_distinct() -> None:
    assert dimensionality("unit.usd") != dimensionality("unit.eur")
    assert dimensionality("unit.persons") == "[reva_population]"
    with pytest.raises(IncompatibleSemantics):
        convert("1", "unit.persons", "unit.kg")


def test_kg_to_barrel_refused_without_density_and_convention() -> None:
    with pytest.raises(IncompatibleSemantics, match="density"):
        convert("1000", "unit.kg", "unit.barrel")
    with pytest.raises(IncompatibleSemantics, match="density"):
        mass_to_volume("1000", "unit.kg", "unit.barrel", None)
    with pytest.raises(IncompatibleSemantics):
        volume_to_mass("1", "unit.barrel", "unit.kg", None)


def test_mass_volume_with_explicit_convention_round_trips() -> None:
    conv = DensityConvention(convention_key="test.crude_at_15c", version="1", density_kg_per_m3="850",
                             reference_temperature_c="15", evidence_ref="test-fixture: assumed density")
    bbl = mass_to_volume("850", "unit.kg", "unit.m3", conv)
    assert bbl == Decimal(1)
    back = volume_to_mass(mass_to_volume("1000", "unit.kg", "unit.barrel", conv), "unit.barrel", "unit.kg", conv)
    assert abs(back - Decimal(1000)) < Decimal("1e-20")  # Pint factors carry ~28 significant digits
    with pytest.raises(IncompatibleSemantics):
        mass_to_volume("1", "unit.barrel", "unit.kg", conv)  # wrong direction of kinds


def test_currency_never_converts_through_pint() -> None:
    with pytest.raises(IncompatibleSemantics, match="CurrencyTransform"):
        convert("1", "unit.usd", "unit.eur")
    with pytest.raises(IncompatibleSemantics):
        convert("1", "unit.usd", "unit.ratio")


def test_ratio_denominators_are_not_interchangeable() -> None:
    with pytest.raises(IncompatibleSemantics, match="denominator"):
        convert("1", "unit.percent_of_baseline_gdp", "unit.percent")
    with pytest.raises(IncompatibleSemantics):
        convert("1", "unit.percent_of_baseline_gdp", "ratio")


def test_currency_without_basis_rejected() -> None:
    rep = validate_observation_units({"value": {"kind": "decimal", "value": "10", "unit_ref": "unit.usd",
                                                "basis": {"scale_factor": "1"}}})
    assert not rep.ok
    assert {"currency_missing", "price_basis_missing"} <= {i.code for i in rep.errors}
    assert not validate_observation_units({"kind": "decimal", "value": "1", "unit_ref": "unit.usd"}).ok
    real_no_year = currency_basis_issues("unit.usd", MeasurementBasis(currency="USD", price_basis="real"))
    assert "price_reference_year_missing" in {i.code for i in real_no_year}
    wrong_ccy = currency_basis_issues("unit.usd", {"currency": "EUR", "price_basis": "nominal"})
    assert "currency_unit_mismatch" in {i.code for i in wrong_ccy}


def test_complete_currency_basis_passes_but_unrecorded_fx_is_flagged() -> None:
    obs = {"value": {"kind": "decimal", "value": "10", "unit_ref": "unit.usd",
                     "basis": {"currency": "USD", "price_basis": "nominal", "scale_factor": "1", "valuation": "CIF"}}}
    rep = validate_observation_units(obs, expected_unit_ref="unit.usd")
    assert rep.ok
    assert [i.code for i in rep.issues] == ["exchange_rate_transform_unrecorded"]


@pytest.mark.parametrize(
    ("value", "code"),
    [
        ({"kind": "decimal", "value": "1"}, "unit_ref_missing"),
        ({"kind": "decimal", "value": "1", "unit_ref": ""}, "unit_ref_missing"),
        ({"kind": "decimal", "value": "1", "unit_ref": "USD"}, "unit_ref_unknown"),
        ({"kind": "float", "value": 1.0, "unit_ref": "unit.barrels_guess"}, "unit_ref_unknown"),
    ],
)
def test_observation_unit_missing_or_unknown_rejected(value: dict, code: str) -> None:
    rep = validate_observation_units({"value": value, "logical_key": "x:population_persons"})
    assert not rep.ok
    assert rep.errors[0].code == code
    with pytest.raises(Exception, match="unit"):
        rep.require()


def test_observation_unit_must_match_concept_and_missing_values_pass() -> None:
    rep = validate_observation_units({"value": {"kind": "decimal", "value": "1", "unit_ref": "unit.kg"}},
                                     expected_unit_ref="unit.persons")
    assert [i.code for i in rep.errors] == ["unit_ref_mismatch"]
    assert validate_observation_units({"value": {"kind": "missing", "reason": "unavailable"}}).ok
    assert not validate_observation_units({"logical_key": "no value"}).ok


def test_canonical_unit_ref_refuses_missing() -> None:
    with pytest.raises(UnknownUnit):
        canonical_unit_ref(None)
    with pytest.raises(UnknownUnit):
        canonical_unit_ref("unit.furlong_per_fortnight")


def _transform(**kw: object) -> CurrencyTransform:
    data: dict = {
        "transform_key": "test.usd_nominal_to_eur_nominal",
        "version": "1.0.0",
        "source": {"currency": "USD", "price_basis": "nominal"},
        "target": {"currency": "EUR", "price_basis": "nominal"},
        "exchange_rate_transform": "annual_average_market_rate",
        "factors": {2023: "0.9"},
        "evidence_refs": ["test-fixture: illustrative factor, not a real rate"],
        "approval": "approved",
    }
    data.update(kw)
    return CurrencyTransform.model_validate(data)


def test_currency_transform_carries_basis_and_requires_period_factor() -> None:
    v = ScalarDecimal(value="100", unit_ref="unit.usd",
                      basis=MeasurementBasis(currency="USD", price_basis="nominal", valuation="FOB"))
    out = apply_currency_transform(v, 2023, _transform())
    assert out.unit_ref == "unit.eur" and out.value == "90"
    assert out.basis.currency == "EUR" and out.basis.valuation == "FOB"
    assert out.basis.exchange_rate_transform.startswith("test.usd_nominal_to_eur_nominal@1.0.0")
    with pytest.raises(MissingInput):
        apply_currency_transform(v, 2022, _transform())
    with pytest.raises(IncompatibleSemantics, match="candidate"):
        apply_currency_transform(v, 2023, _transform(approval="candidate"))


def test_currency_transform_rejects_incomplete_or_mismatched_basis() -> None:
    no_basis = ScalarDecimal(value="1", unit_ref="unit.usd", basis=MeasurementBasis())
    with pytest.raises(IncompatibleSemantics, match="incomplete"):
        apply_currency_transform(no_basis, 2023, _transform())
    ppp = ScalarDecimal(value="1", unit_ref="unit.usd",
                        basis=MeasurementBasis(currency="USD", price_basis="ppp", price_reference_year=2017))
    with pytest.raises(IncompatibleSemantics, match="does not match"):
        apply_currency_transform(ppp, 2023, _transform())
    with pytest.raises(ValueError, match="reference year"):
        _transform(target={"currency": "USD", "price_basis": "real"})
