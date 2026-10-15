"""Contract: registry/components/*.schema.json are valid, pinned and reference real concepts."""

from __future__ import annotations

import json

import pytest
from jsonschema import Draft202012Validator

from reva.adapters.connectors.comtrade import FLOW_CONCEPT, WEIGHT_CONCEPT
from reva.ontology.components import (
    COMPONENT_METADATA_KEY,
    COMPONENTS_DIR,
    PUBLISHED_COMPONENT_DIGESTS,
    REQUESTED_REGISTRY_CONCEPTS,
    SCHEMA_DIALECT,
    latest_manifest,
    load_component_manifests,
    manifest_digest,
    verify_published,
)
from reva.ontology.registry import load_registry

FILES = sorted(COMPONENTS_DIR.glob("*.schema.json"))


def test_required_components_exist() -> None:
    names = {p.name for p in FILES}
    assert {"demographics.v1.schema.json", "economy.v1.schema.json", "trade_crude.v1.schema.json",
            "display_geometry.v1.schema.json"} <= names


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_schema_is_valid_draft_2020_12(path) -> None:  # type: ignore[no-untyped-def]
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["$schema"] == SCHEMA_DIALECT
    Draft202012Validator.check_schema(doc)
    assert COMPONENT_METADATA_KEY in doc


def test_published_versions_match_pinned_digests() -> None:
    manifests = load_component_manifests()
    verify_published(manifests)
    unpinned = sorted(set(manifests) - set(PUBLISHED_COMPONENT_DIGESTS))
    assert not unpinned, f"pin new component versions: {[(k, manifest_digest(manifests[k])) for k in unpinned]}"


def test_manifest_concepts_resolve_in_registry() -> None:
    reg = load_registry()
    known = {(c.concept_id, c.version) for c in reg.concepts}
    known_ids = {c.concept_id for c in reg.concepts}
    for m in load_component_manifests().values():
        for ref in m.concepts:
            cid, ver = ref.split("@")
            assert (cid, ver) in known, f"{m.name}: unknown concept {ref}"
        for kind in m.applicable_entity_kinds:
            assert kind in known_ids or kind in REQUESTED_REGISTRY_CONCEPTS, f"{m.name}: unknown entity kind {kind}"
    # A requested concept that has since been registered must be removed from the request list.
    assert not (REQUESTED_REGISTRY_CONCEPTS & known_ids)


def _property_names(node: object) -> set[str]:
    out: set[str] = set()
    if isinstance(node, dict):
        out |= set(node.get("properties", {}) if isinstance(node.get("properties"), dict) else {})
        for v in node.values():
            out |= _property_names(v)
    elif isinstance(node, list):
        for v in node:
            out |= _property_names(v)
    return out


def test_component_privacy_is_v1_admissible_and_no_gender_field() -> None:
    for m in load_component_manifests().values():
        assert m.privacy_class in ("public_aggregate", "synthetic_public_aggregate_derived")
        names = _property_names(m.json_schema)
        assert not {n for n in names if "gender" in n}, m.name
    demo = load_component_manifests()["demographics@1.0.0"]
    assert {"age", "recorded_sex"} <= set(demo.json_schema["required"])


def test_latest_trade_crude_lists_the_world_total_concepts_comtrade_emits() -> None:
    """The current trade_crude component binds exactly the world-total concepts the connector produces."""
    latest = latest_manifest(load_component_manifests(), "trade_crude")
    emitted = {c for (_flow, scope), c in FLOW_CONCEPT.items() if scope == "total"} | set(WEIGHT_CONCEPT.values())
    assert {c.split("@")[0] for c in latest.concepts} == emitted
