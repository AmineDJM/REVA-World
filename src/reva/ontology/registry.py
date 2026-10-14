"""Versioned YAML registries validated against kernel contracts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from reva.kernel.ids import logical_hash
from reva.kernel.records import Concept

REGISTRY_ROOT = Path(__file__).resolve().parents[3] / "registry"


def load_yaml(path: Path) -> Any:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


@dataclass(frozen=True)
class Registry:
    root: Path
    concepts: tuple[Concept, ...]
    entities: tuple[dict[str, Any], ...]
    sources: tuple[dict[str, Any], ...]
    mappings: tuple[dict[str, Any], ...]
    models: tuple[dict[str, Any], ...]
    policies: dict[str, Any]
    requirements: dict[str, Any]

    def concept(self, concept_id: str) -> Concept:
        for c in self.concepts:
            if c.concept_id == concept_id:
                return c
        raise KeyError(concept_id)

    def manifest(self) -> dict[str, Any]:
        """Ontology manifest pinned by releases and runs."""
        return {
            "schema": "reva-ontology-manifest/1",
            "concepts": sorted(
                [{"concept_id": c.concept_id, "version": c.version, "sha256": concept_hash(c)} for c in self.concepts],
                key=lambda x: (x["concept_id"], x["version"]),
            ),
            "mappings": sorted(
                [{"mapping_key": m["mapping_key"], "version": m["version"], "kind": m["mapping_kind"]}
                 for m in self.mappings],
                key=lambda x: (x["mapping_key"], x["version"]),
            ),
        }


def concept_hash(c: Concept) -> str:
    return logical_hash(c.model_dump(mode="json"))


def load_registry(root: Path | None = None) -> Registry:
    root = root or REGISTRY_ROOT
    concepts: list[Concept] = []
    for p in sorted((root / "concepts").glob("*.yaml")):
        for raw in load_yaml(p)["concepts"]:
            concepts.append(Concept.model_validate(raw))
    ids = [(c.concept_id, c.version) for c in concepts]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate concept versions in registry")
    entities = tuple(load_yaml(root / "entities" / "countries.yaml")["entities"])
    sources = tuple(load_yaml(p) for p in sorted((root / "sources").glob("*.yaml")))
    mappings: list[dict[str, Any]] = []
    for p in sorted((root / "mappings").glob("*.yaml")):
        doc = load_yaml(p)
        for m in doc["mappings"]:
            mappings.append({**m, "connector": doc["connector"]})
    models = tuple(load_yaml(root / "models" / "methods.yaml")["models"])
    policies = {p.stem: load_yaml(p) for p in sorted((root / "policies").glob("*.yaml"))}
    requirements = load_yaml(root / "requirements" / "mvp.yaml")
    known = {c.concept_id for c in concepts}
    for m in mappings:
        if m["target"]["concept_id"] not in known:
            raise ValueError(f"mapping {m['mapping_key']} targets unknown concept")
    return Registry(root, tuple(concepts), entities, sources, tuple(mappings), models, policies, requirements)
