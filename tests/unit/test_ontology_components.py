"""Component manifests: instance validation, change classification, immutability."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from reva.kernel.errors import ImmutableRecordViolation, ValidationProblem
from reva.kernel.records import ComponentManifest
from reva.ontology.components import (
    ChangeLevel,
    ComponentCatalog,
    check_version_bump,
    classify_change,
    component_key,
    load_component_manifests,
    validate_component_instance,
    verify_published,
)

MANIFESTS = load_component_manifests()
DEMO = MANIFESTS["demographics@1.0.0"]
ECON = MANIFESTS["economy@1.0.0"]
TRADE = MANIFESTS["trade_crude@1.0.0"]
GEOM = MANIFESTS["display_geometry@1.0.0"]


def _evolve(m: ComponentManifest, version: str, mutate: Any = None, **update: Any) -> ComponentManifest:
    schema = copy.deepcopy(m.json_schema)
    if mutate is not None:
        mutate(schema)
    schema["$id"] = f"urn:reva:component:{m.name}:{version}"
    return m.model_copy(update={"version": version, "json_schema": schema, **update})


def test_component_keys() -> None:
    assert {component_key(m) for m in MANIFESTS.values()} == {
        "demographics.v1", "economy.v1", "trade_crude.v1", "display_geometry.v1"}


# --------------------------------------------------------------------------- instances

GOOD_DEMO = {"age": {"kind": "single_year", "source_code": "Y42", "years": 42},
             "recorded_sex": {"code": "F", "vocabulary_version": "eurostat.sex.recorded.v1"},
             "weight": {"kind": "decimal", "value": "1234.5", "unit_ref": "unit.persons", "basis": {}},
             "reference_date": "2023-01-01"}


def test_demographics_records_recorded_sex_not_gender() -> None:
    assert validate_component_instance(DEMO, GOOD_DEMO).ok
    bad = {**GOOD_DEMO, "gender": "woman"}
    rep = validate_component_instance(DEMO, bad)
    assert not rep.ok
    assert any("gender" in e for e in rep.rule_errors)
    assert any("gender" in e for e in rep.schema_errors)
    with pytest.raises(ValidationProblem):
        rep.require()
    assert not validate_component_instance(DEMO, {**GOOD_DEMO, "recorded_sex": {
        "code": "X", "vocabulary_version": "eurostat.sex.recorded.v1"}}).ok


def test_demographics_open_and_unknown_ages_are_not_imputed() -> None:
    open_age = {**GOOD_DEMO, "age": {"kind": "open_ended", "source_code": "Y_OPEN", "lower_bound_years": 100}}
    assert validate_component_instance(DEMO, open_age).ok
    imputed = {**GOOD_DEMO, "age": {"kind": "unknown", "source_code": "UNK", "years": 40}}
    rep = validate_component_instance(DEMO, imputed)
    assert not rep.ok and any("no imputation" in e for e in rep.rule_errors)


def test_economy_gdp_requires_full_currency_basis() -> None:
    ok = {"reference_year": 2023, "gdp_current_usd": {
        "kind": "decimal", "value": "3051831611384.3", "unit_ref": "unit.usd",
        "basis": {"currency": "USD", "price_basis": "nominal", "exchange_rate_transform": "provider_converted"}}}
    assert validate_component_instance(ECON, ok).ok
    no_basis = {"reference_year": 2023, "gdp_current_usd": {"kind": "decimal", "value": "1", "unit_ref": "unit.usd",
                                                            "basis": {}}}
    rep = validate_component_instance(ECON, no_basis)
    assert not rep.ok and rep.rule_errors
    missing = {"reference_year": 2023, "gdp_current_usd": {"kind": "missing", "reason": "unavailable"}}
    assert validate_component_instance(ECON, missing).ok
    zero_for_missing = {"reference_year": 2023, "gdp_current_usd": None}
    assert not validate_component_instance(ECON, zero_for_missing).ok


def _border(valuation: str | None) -> dict:
    basis: dict = {"currency": "USD", "price_basis": "nominal"}
    if valuation is not None:
        basis["valuation"] = valuation
    return {"kind": "decimal", "value": "1000", "unit_ref": "unit.usd", "basis": basis}


def test_trade_crude_requires_valuation_and_kg_only() -> None:
    inst = {"reference_year": 2023, "classification": {"system": "HS", "revision": "H6", "product_code": "2709"},
            "imports_border_value": _border("CIF"), "exports_border_value": {"kind": "missing", "reason": "unavailable"},
            "imports_net_weight": {"kind": "decimal", "value": "5", "unit_ref": "unit.kg"}}
    assert validate_component_instance(TRADE, inst).ok
    rep = validate_component_instance(TRADE, {**inst, "imports_border_value": _border(None)})
    assert not rep.ok and any("valuation" in e for e in rep.rule_errors)
    barrels = {**inst, "imports_net_weight": {"kind": "decimal", "value": "5", "unit_ref": "unit.barrel"}}
    assert not validate_component_instance(TRADE, barrels).ok


def test_display_geometry_is_never_authoritative() -> None:
    inst = {"dataset_sha256": "a" * 64, "feature_key": "FRA", "match_status": "reviewed_match", "crs": "OGC:CRS84",
            "geometry_wkt": "POLYGON ((0 0, 1 0, 1 1, 0 0))", "scale": "1:110m", "authoritative": False}
    assert validate_component_instance(GEOM, inst).ok
    assert not validate_component_instance(GEOM, {**inst, "authoritative": True}).ok


# --------------------------------------------------------------------------- change classification


def test_additive_optional_field_is_minor() -> None:
    new = _evolve(ECON, "1.1.0", lambda s: s["properties"].__setitem__("gdp_note", {"type": "string"}))
    rep = classify_change(ECON, new)
    assert rep.level is ChangeLevel.minor
    check_version_bump(ECON, new)


def test_new_required_field_is_major() -> None:
    def mutate(s: dict) -> None:
        s["properties"]["population"] = {"type": "string"}
        s["required"].append("population")

    rep = classify_change(ECON, _evolve(ECON, "2.0.0", mutate))
    assert rep.level is ChangeLevel.major
    with pytest.raises(ValidationProblem, match="minor bump"):
        check_version_bump(ECON, _evolve(ECON, "1.1.0", mutate))


def test_unit_change_is_major() -> None:
    def mutate(s: dict) -> None:
        s["properties"]["gdp_current_usd"]["oneOf"][0]["properties"]["unit_ref"] = {"const": "unit.eur"}

    rep = classify_change(ECON, _evolve(ECON, "2.0.0", mutate))
    assert rep.level is ChangeLevel.major
    assert any("unit_ref" in r.path for r in rep.reasons)


def test_category_definition_change_is_major_even_if_additive() -> None:
    def mutate(s: dict) -> None:
        s["properties"]["recorded_sex"]["properties"]["code"]["enum"].append("X")

    assert classify_change(DEMO, _evolve(DEMO, "1.1.0", mutate)).level is ChangeLevel.major


def test_meaning_change_is_major_and_title_is_patch() -> None:
    def meaning(s: dict) -> None:
        s["properties"]["reference_year"]["description"] = "Fiscal year ending in this year"

    assert classify_change(ECON, _evolve(ECON, "2.0.0", meaning)).level is ChangeLevel.major
    assert classify_change(ECON, _evolve(ECON, "1.0.1", lambda s: s.__setitem__("title", "x"))).level is \
        ChangeLevel.patch
    concept_bump = _evolve(ECON, "2.0.0", concepts=("economics.gdp.nominal_current_usd@2.0.0",))
    assert classify_change(ECON, concept_bump).level is ChangeLevel.major
    assert classify_change(ECON, _evolve(ECON, "1.0.0")).level is ChangeLevel.none


def test_removed_field_and_privacy_change_are_major() -> None:
    assert classify_change(TRADE, _evolve(TRADE, "2.0.0", lambda s: s["properties"].pop("exports_net_weight"))
                           ).level is ChangeLevel.major
    assert classify_change(ECON, _evolve(ECON, "2.0.0", privacy_class="synthetic_public_aggregate_derived")
                           ).level is ChangeLevel.major


# --------------------------------------------------------------------------- immutability


def test_published_versions_are_immutable() -> None:
    cat = ComponentCatalog()
    cat.publish(ECON)
    cat.publish(ECON)  # idempotent
    changed_in_place = _evolve(ECON, "1.0.0", lambda s: s["properties"].__setitem__("x", {"type": "string"}))
    with pytest.raises(ImmutableRecordViolation):
        cat.publish(changed_in_place)
    with pytest.raises(ValidationProblem):
        cat.publish(_evolve(ECON, "1.0.1", lambda s: s["properties"].__setitem__("x", {"type": "string"})))
    cat.publish(_evolve(ECON, "1.1.0", lambda s: s["properties"].__setitem__("x", {"type": "string"})))
    assert cat.versions("economy") == ["1.0.0", "1.1.0"]


def test_verify_published_detects_in_place_edit() -> None:
    verify_published(MANIFESTS)
    edited = dict(MANIFESTS)
    edited["economy@1.0.0"] = _evolve(ECON, "1.0.0", lambda s: s.__setitem__("title", "edited"))
    with pytest.raises(ImmutableRecordViolation):
        verify_published(edited)
