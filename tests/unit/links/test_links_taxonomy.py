"""Link taxonomy (registry/links/link_types.yaml) and hub list (registry/links/hubs.yaml)."""

from __future__ import annotations

import copy

import pytest
import yaml

from reva.links.taxonomy import (
    CAUSAL_PIDS,
    LINK_TYPES_FILE,
    kg_allowlist,
    load_hubs,
    load_taxonomy,
    validate_taxonomy,
)

REQUIRED_LINK_TYPES = {
    "border_wikidata",
    "land_border",
    "maritime_neighbour",
    "shared_official_language",
    "shared_language_used",
    "shared_language_family",
    "colony_of",
    "former_colony_of",
    "successor_of",
    "co_member",
    "alliance_co_member",
    "co_signatory",
    "conflict_co_participant",
    "conflict_same_side",
    "conflict_opposing_sides",
    "diplomatic_relation",
    "bilateral_trade",
    "migrant_stock",
    "shared_head_of_state",
    "shared_currency",
    "shared_legal_family",
    "unga_vote_agreement",
}


@pytest.fixture(scope="module")
def doc() -> dict:
    return yaml.safe_load(LINK_TYPES_FILE.read_text(encoding="utf-8"))


def test_taxonomy_validates_against_schema_and_allow_list(doc: dict) -> None:
    assert validate_taxonomy(doc, kg_allowlist()) == []


def test_required_link_types_present(doc: dict) -> None:
    ids = {d["id"] for d in doc["derived_links"]}
    assert REQUIRED_LINK_TYPES <= ids


def test_every_link_type_is_complete() -> None:
    tax = load_taxonomy()
    for lt in tax.link_types.values():
        assert lt.labels["fr"] and lt.labels["en"]
        assert lt.honesty["fr"].startswith("relié via") and lt.honesty["en"].startswith("connected via")
        assert lt.temporal_kind in ("interval", "point", "snapshot", "undated")
        assert lt.properties or lt.concepts
        if lt.directed:
            assert lt.inverse_labels != lt.labels
        if lt.weight_kind == "source_quantity":
            assert lt.method.startswith("atlas_")


def test_causal_wording_reserved_to_causal_properties() -> None:
    tax = load_taxonomy()
    causal = {p for p, e in tax.edge_types.items() if e.causal}
    assert causal == set(CAUSAL_PIDS)
    assert {"P828", "P1542"} <= causal
    for p in causal:
        assert tax.edge_types[p].causal_phrase is not None


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (lambda d: d["derived_links"][0]["honesty"].update(en="caused by a border"), "honesty"),
        (lambda d: d["derived_links"][0].update(causal=True), "schema"),
        (lambda d: d["derived_links"][0]["labels"].pop("fr"), "schema"),
        (lambda d: d["derived_links"].append(copy.deepcopy(d["derived_links"][0])), "duplicate"),
        (
            lambda d: d["statement_edges"].append(
                {**d["statement_edges"][0], "property": "P37", "causal": True, "causal_phrase": {"fr": "x", "en": "y"}}
            ),
            "causal wording",
        ),
        (
            lambda d: next(x for x in d["derived_links"] if x["derivation"]["method"].startswith("kg_"))[
                "weight"
            ].update(kind="source_quantity"),
            "quantities",
        ),
        (lambda d: next(x for x in d["derived_links"] if x["directed"]).pop("inverse_labels"), "schema"),
    ],
)
def test_invalid_taxonomies_rejected(doc: dict, mutate, expected: str) -> None:
    bad = copy.deepcopy(doc)
    mutate(bad)
    problems = validate_taxonomy(bad)
    assert problems, "mutation should be rejected"
    assert any(expected in p for p in problems), problems


def test_unknown_property_rejected_against_allow_list(doc: dict) -> None:
    bad = copy.deepcopy(doc)
    bad["statement_edges"].append({**bad["statement_edges"][0], "property": "P999999"})
    assert any("allow-list" in p for p in validate_taxonomy(bad, kg_allowlist()))


def test_hubs_declarative() -> None:
    hubs = load_hubs()
    assert {"Q5", "Q2", "Q6256"} <= hubs.blocked  # human, Earth, country
    assert hubs.penalised["Q1065"] > 0  # United Nations
    assert "P31" in hubs.excluded_properties
    assert not hubs.blocked & set(hubs.penalised)
    assert hubs.degree_threshold > 0
