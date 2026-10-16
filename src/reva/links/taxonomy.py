"""Loading and checking the link taxonomy (``registry/links/link_types.yaml``) and the hub list
(``registry/links/hubs.yaml``)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import jsonschema
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
LINKS_DIR = REPO_ROOT / "registry" / "links"
LINK_TYPES_FILE = LINKS_DIR / "link_types.yaml"
LINK_TYPES_SCHEMA = LINKS_DIR / "link_types.schema.json"
HUBS_FILE = LINKS_DIR / "hubs.yaml"
KG_PROPERTIES_FILE = REPO_ROOT / "registry" / "kg" / "properties.yaml"

#: Properties whose statements are causal claims *of the source*. Only these may be worded causally
#: ("selon <source>, X a pour cause Y"). P828/P1542 are KGCORE's ``CAUSAL_PIDS``; the four others are
#: their immediate-cause / contributing-factor refinements, listed with them in the V3 honesty rules.
CAUSAL_PIDS = frozenset({"P828", "P1542", "P1478", "P1536", "P1479", "P1537"})


class TaxonomyError(ValueError):
    pass


@dataclass(frozen=True)
class LinkType:
    id: str
    labels: Mapping[str, str]
    inverse_labels: Mapping[str, str]
    scope: str
    directed: bool
    method: str
    properties: tuple[str, ...]
    concepts: tuple[str, ...]
    sources: tuple[str, ...]
    temporal_kind: str
    weight_kind: str
    weight_unit: str | None
    honesty: Mapping[str, str]
    status: str
    path_cost: float
    raw: Mapping[str, Any] = field(repr=False, default_factory=dict)

    @property
    def atlas(self) -> Mapping[str, Any] | None:
        """The dyadic atlas spec (``derivation.atlas``) of an ``atlas_dyadic`` type, if declared."""
        return (self.raw.get("derivation") or {}).get("atlas")


@dataclass(frozen=True)
class EdgeType:
    property: str
    labels: Mapping[str, str]
    inverse_labels: Mapping[str, str]
    path_cost: float
    causal: bool = False
    causal_phrase: Mapping[str, str] | None = None


@dataclass(frozen=True)
class Taxonomy:
    link_types: Mapping[str, LinkType]
    edge_types: Mapping[str, EdgeType]

    def link(self, link_type: str) -> LinkType:
        try:
            return self.link_types[link_type]
        except KeyError as e:
            raise TaxonomyError(f"unknown link type {link_type!r}") from e


@dataclass(frozen=True)
class Hubs:
    blocked: frozenset[str]
    penalised: Mapping[str, float]
    excluded_properties: frozenset[str]
    degree_threshold: int
    degree_per_doubling: float


def _load_yaml(path: Path) -> Any:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def validate_taxonomy(doc: Mapping[str, Any], kg_properties: set[str] | None = None) -> list[str]:
    """Schema + semantic checks. Returns a list of problems (empty = valid)."""
    problems: list[str] = []
    schema = json.loads(LINK_TYPES_SCHEMA.read_text(encoding="utf-8"))
    v = jsonschema.Draft202012Validator(schema)
    for err in sorted(v.iter_errors(doc), key=lambda e: list(e.absolute_path)):
        problems.append(f"schema: {'/'.join(str(p) for p in err.absolute_path)}: {err.message}")
    if problems:
        return problems
    ids = [d["id"] for d in doc["derived_links"]]
    for dup in sorted({i for i in ids if ids.count(i) > 1}):
        problems.append(f"duplicate link type id {dup}")
    pids = [e["property"] for e in doc["statement_edges"]]
    for dup in sorted({p for p in pids if pids.count(p) > 1}):
        problems.append(f"duplicate statement edge {dup}")
    for d in doc["derived_links"]:
        if not d["honesty"]["fr"].startswith("relié via") or not d["honesty"]["en"].startswith("connected via"):
            problems.append(f"{d['id']}: honesty wording must start with 'relié via' / 'connected via'")
        if d["weight"]["kind"] == "source_quantity" and d["derivation"]["method"] != "atlas_dyadic":
            problems.append(f"{d['id']}: only atlas/dataset quantities may weight a link")
        if d["weight"]["kind"] == "derived_index" and d["derivation"]["method"] != "atlas_computed":
            problems.append(f"{d['id']}: a derived index weight needs method atlas_computed (formula in basis)")
        if d["derivation"]["method"] == "atlas_computed" and d["weight"]["kind"] != "derived_index":
            problems.append(f"{d['id']}: an atlas_computed link must label its weight derived_index")
        if d["derivation"].get("atlas") and d["derivation"]["method"] != "atlas_dyadic":
            problems.append(f"{d['id']}: derivation.atlas is only for atlas_dyadic types")
        text = (d["labels"]["en"] + " " + d["honesty"]["en"]).lower()
        for word in (
            ("alliance", "friendship", "ally", "allies") if d["derivation"]["method"] == "atlas_computed" else ()
        ):
            if word in text:
                problems.append(f"{d['id']}: a computed index is not an {word}")
        for word in ("cause", "because", "parce que", "à cause"):
            if word in d["honesty"]["en"].lower() or word in d["honesty"]["fr"].lower():
                problems.append(f"{d['id']}: causal wording in a derived link")
    for e in doc["statement_edges"]:
        causal = bool(e.get("causal"))
        if causal and e["property"] not in CAUSAL_PIDS:
            problems.append(f"{e['property']}: causal wording reserved to {sorted(CAUSAL_PIDS)}")
        if e["property"] in CAUSAL_PIDS and not causal:
            problems.append(f"{e['property']}: causal property must be flagged causal (attribution wording)")
        if kg_properties is not None and e["property"] not in kg_properties:
            problems.append(f"{e['property']}: not in the KG allow-list (registry/kg/properties.yaml)")
    return problems


def kg_allowlist() -> set[str]:
    """Wikidata properties (registry/kg/properties.yaml) plus the namespaced dataset predicates registered by the
    KG layers (registry/kg/predicates_*.yaml; KGGEOPOL's GEOPOL_PROPERTIES)."""
    doc = _load_yaml(KG_PROPERTIES_FILE)
    out = {str(p["property"]) for p in doc.get("properties", [])}
    for f in sorted(KG_PROPERTIES_FILE.parent.glob("predicates_*.yaml")):
        out |= {str(p["pid"]) for p in (_load_yaml(f) or {}).get("predicates", [])}
    try:
        from reva.adapters.connectors.atlas_geopol_common import GEOPOL_PROPERTIES

        out |= set(GEOPOL_PROPERTIES)
    except ImportError:  # pragma: no cover
        pass
    try:
        from reva.adapters.connectors.atlas_peacekeeping import UNPK_PROPERTIES

        out |= set(UNPK_PROPERTIES)
    except ImportError:  # pragma: no cover
        pass
    return out


@lru_cache(maxsize=4)
def load_taxonomy(path: Path = LINK_TYPES_FILE) -> Taxonomy:
    doc = _load_yaml(path)
    problems = validate_taxonomy(doc)
    if problems:
        raise TaxonomyError("; ".join(problems[:10]))
    lts = {}
    for d in doc["derived_links"]:
        der = d["derivation"]
        lts[d["id"]] = LinkType(
            id=d["id"],
            labels=dict(d["labels"]),
            inverse_labels=dict(d.get("inverse_labels") or d["labels"]),
            scope=d["scope"],
            directed=bool(d["directed"]),
            method=der["method"],
            properties=tuple(der.get("properties", ())),
            concepts=tuple(der.get("concepts", ())),
            sources=tuple(der["sources"]),
            temporal_kind=d["temporal"]["kind"],
            weight_kind=d["weight"]["kind"],
            weight_unit=d["weight"].get("unit"),
            honesty=dict(d["honesty"]),
            status=d["status"],
            path_cost=float(d["path_cost"]),
            raw=d,
        )
    ets = {
        e["property"]: EdgeType(
            property=e["property"],
            labels=dict(e["labels"]),
            inverse_labels=dict(e["inverse_labels"]),
            path_cost=float(e["path_cost"]),
            causal=bool(e.get("causal", False)),
            causal_phrase=dict(e["causal_phrase"]) if e.get("causal_phrase") else None,
        )
        for e in doc["statement_edges"]
    }
    return Taxonomy(lts, ets)


@lru_cache(maxsize=4)
def load_hubs(path: Path = HUBS_FILE) -> Hubs:
    doc = _load_yaml(path)
    if doc.get("version") != 1:
        raise TaxonomyError("hubs.yaml: version must be 1")
    blocked = frozenset(str(h["qid"]) for h in doc.get("blocked", []))
    penalised = {str(h["qid"]): float(h["penalty"]) for h in doc.get("penalised", [])}
    if blocked & set(penalised):
        raise TaxonomyError(f"hubs both blocked and penalised: {sorted(blocked & set(penalised))}")
    if any(p < 0 for p in penalised.values()):
        raise TaxonomyError("hub penalties must be >= 0")
    dp = doc.get("degree_penalty") or {}
    return Hubs(
        blocked=blocked,
        penalised=penalised,
        excluded_properties=frozenset(str(p["property"]) for p in doc.get("excluded_properties", [])),
        degree_threshold=int(dp.get("threshold", 64)),
        degree_per_doubling=float(dp.get("per_doubling", 1.0)),
    )
