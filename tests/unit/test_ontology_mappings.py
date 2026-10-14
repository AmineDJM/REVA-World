"""Versioned mappings: identity only via exact; crosswalks preserve unmapped mass."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from reva.kernel.errors import IncompatibleSemantics
from reva.kernel.values import Missing, MissingReason
from reva.ontology.mappings import (
    ConceptMapping,
    RelationType,
    WeightedCrosswalk,
    assess_identity,
    check_mapping_against_concepts,
    find_mapping,
    load_registry_mappings,
    mapping_from_registry,
)
from reva.ontology.registry import load_registry

REG = load_registry()
MAPPINGS = load_registry_mappings(REG)


def test_all_registry_mappings_are_typed_and_versioned() -> None:
    assert len(MAPPINGS) == len(REG.mappings)
    for m in MAPPINGS:
        assert m.version and m.target.version
        assert m.evidence and m.approval.basis


def test_eurostat_jan1_total_vs_wdi_midyear_is_incompatible() -> None:
    m = find_mapping(MAPPINGS, "eurostat.demo_pjan.to_wdi_midyear")
    assert m.relation is RelationType.incompatible
    assert not m.usable_as_identity
    with pytest.raises(IncompatibleSemantics, match="only exact"):
        m.require_identity(subject="FRA", year=2023)
    # The registry declaration agrees with an independent semantic assessment.
    assessed = assess_identity(REG.concept("demography.population.jan1_age_sex"),
                               REG.concept("demography.population.wdi_midyear_total"))
    assert assessed.relation is RelationType.incompatible
    assert {"temporal_point_mismatch", "population_universe_mismatch"} <= assessed.report.codes


def test_exact_mapping_is_identity_only_within_applicability() -> None:
    m = find_mapping(MAPPINGS, "wb.SP.POP.TOTL")
    assert m.usable_as_identity
    assert m.require_identity(subject="FRA", year=2010).concept_id == "demography.population.wdi_midyear_total"
    with pytest.raises(IncompatibleSemantics, match="does not apply"):
        m.require_identity(subject="FRA", year=1999)
    with pytest.raises(IncompatibleSemantics, match="does not apply"):
        m.require_identity(subject="DEU", year=2010)
    cm = find_mapping(MAPPINGS, "comtrade.2709.M.world")
    cm.require_identity(subject="FRA", year=2023, classification="H6")
    with pytest.raises(IncompatibleSemantics):
        cm.require_identity(subject="FRA", year=2023, classification="H3")


def test_non_exact_relations_are_never_identity() -> None:
    raw = {"mapping_key": "test.overlap", "version": "1.0.0", "source_ref": {"provider": "x"},
           "target": {"concept_id": "demography.population.wdi_midyear_total", "version": "1.0.0"},
           "mapping_kind": "overlap",
           "approval": {"status": "approved", "basis": "test-review"}}
    m = mapping_from_registry(raw)
    assert not m.usable_as_identity
    with pytest.raises(IncompatibleSemantics):
        m.require_identity()


def test_mapping_coherence_rules() -> None:
    base = mapping_from_registry(REG.mappings[0]).model_dump()
    with pytest.raises(ValidationError, match="identity"):
        ConceptMapping.model_validate({**base, "relation": "approximate_transform"})
    with pytest.raises(ValidationError, match="uncertainty"):
        ConceptMapping.model_validate({**base, "relation": "approximate_transform",
                                       "transform": {"kind": "linear", "params": {"factor": "1"}},
                                       "uncertainty": {"kind": "not_applicable"}})
    with pytest.raises(ValidationError, match="crosswalk"):
        ConceptMapping.model_validate({**base, "relation": "weighted_crosswalk", "transform": {"kind": "none"},
                                       "uncertainty": {"kind": "unquantified"}})


def test_declared_exact_mapping_cannot_contradict_concepts() -> None:
    jan = REG.concept("demography.population.jan1_age_sex")
    wdi = REG.concept("demography.population.wdi_midyear_total")
    wrong = find_mapping(MAPPINGS, "wb.SP.POP.TOTL").model_copy(update={"mapping_key": "test.wrong_exact"})
    with pytest.raises(IncompatibleSemantics, match="declared exact"):
        check_mapping_against_concepts(wrong, jan, wdi)


# --------------------------------------------------------------------------- crosswalks


def _crosswalk(links: list[dict], approval: str = "approved") -> WeightedCrosswalk:
    return WeightedCrosswalk.model_validate({
        "crosswalk_key": "test.hs_to_sitc", "version": "1.0.0",
        "source_classification": "HS:H5", "target_classification": "SITC:4",
        "links": links,
        "evidence": [{"kind": "methodology_document", "ref": "test-fixture: synthetic crosswalk"}],
        "approval": {"status": approval, "basis": "test-review"},
    })


LINKS = [
    {"source_code": "A", "target_code": "X", "weight": "0.6"},
    {"source_code": "A", "target_code": "Y", "weight": "0.3"},  # 10% of A unmapped
    {"source_code": "B", "target_code": "Y", "weight": "1", "valid_to_year": 2021},
    {"source_code": "B", "target_code": "Z", "weight": "1", "valid_from_year": 2022},
]


def test_crosswalk_preserves_unmapped_mass_as_residual() -> None:
    cw = _crosswalk(LINKS)
    r = cw.apply({"A": "100", "B": "50", "C": "7"}, 2023, unit_ref="unit.usd")
    assert r.targets == {"X": "60", "Y": "30", "Z": "50"}
    assert r.residual_by_source == {"A": "10", "C": "7"}
    assert r.unmatched_source_codes == ("C",)
    assert Decimal(r.total_input) == Decimal(r.total_mapped) + Decimal(r.total_residual) == Decimal(157)
    assert not r.complete
    # The residual is not redistributed: targets total less than the input.
    assert sum(Decimal(v) for v in r.targets.values()) == Decimal(140)


def test_crosswalk_is_time_dependent() -> None:
    cw = _crosswalk(LINKS)
    assert cw.apply({"B": "50"}, 2020, unit_ref="unit.usd").targets == {"Y": "50"}
    assert cw.apply({"B": "50"}, 2022, unit_ref="unit.usd").targets == {"Z": "50"}


def test_crosswalk_missing_inputs_flag_targets_incomplete() -> None:
    r = _crosswalk(LINKS).apply({"A": Missing(reason=MissingReason.suppressed), "B": "5"}, 2023,
                                unit_ref="unit.usd")
    assert r.missing_source_codes == ("A",)
    assert r.incomplete_target_codes == ("X", "Y")
    assert r.targets == {"Z": "5"}  # never a zero-filled X


def test_crosswalk_weights_cannot_create_mass() -> None:
    with pytest.raises(ValidationError, match="> 1"):
        _crosswalk([{"source_code": "A", "target_code": "X", "weight": "0.7"},
                    {"source_code": "A", "target_code": "Y", "weight": "0.4", "valid_from_year": 2020}])
    # non-overlapping periods may each sum to 1
    _crosswalk([{"source_code": "A", "target_code": "X", "weight": "0.7", "valid_to_year": 2019},
                {"source_code": "A", "target_code": "Y", "weight": "0.4", "valid_from_year": 2020}])
    with pytest.raises(ValidationError):
        _crosswalk([{"source_code": "A", "target_code": "X", "weight": "0"}])


def test_crosswalk_requires_classification_revision_and_approval() -> None:
    with pytest.raises(ValidationError):
        WeightedCrosswalk.model_validate({
            "crosswalk_key": "k", "version": "1", "source_classification": "HS",
            "target_classification": "SITC:4", "links": LINKS,
            "evidence": [{"kind": "review_note", "ref": "r"}], "approval": {"status": "approved", "basis": "b"}})
    with pytest.raises(IncompatibleSemantics, match="candidate"):
        _crosswalk(LINKS, approval="candidate").apply({"A": "1"}, 2023, unit_ref="unit.usd")
