"""Coverage domains and places (V8 COUVERTURE): the rows and columns of the coverage matrix.

``registry/coverage/domains.yaml`` declares the domains (each with the atlas themes, atlas sources and KG kinds it
groups) and the French labels of the UN M49 regions used as columns. Assignment rules are documented in that file and
implemented here; nothing in this module reads the database.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from reva.ontology.registry import REGISTRY_ROOT

DOMAINS_PATH = REGISTRY_ROOT / "coverage" / "domains.yaml"
CROSSWALK_PATH = REGISTRY_ROOT / "entities" / "country_crosswalk.json"
COUNTRY_QIDS_PATH = REGISTRY_ROOT / "entities" / "country_qids.json"
OTHER = "other"


@dataclass(frozen=True)
class Domain:
    id: str
    fr: str
    en: str
    themes: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    kg_kinds: tuple[str, ...] = ()
    kg_kind_sources: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


def _match(pattern: str, value: str) -> bool:
    return pattern == value or (pattern.endswith("*") and value.startswith(pattern[:-1]))


@dataclass(frozen=True)
class Place:
    """One country-level entity of the crosswalk (ISO3 or World Bank XKX/CHI) with its M49 region."""

    iso3: str
    label: str
    label_fr: str | None
    iso2: str | None
    region: str
    subregion: str
    placement: str  # "m49" | "reva" (REVA placement, see domains.yaml region_overrides)
    placement_basis: str | None = None


@dataclass(frozen=True)
class DomainRegistry:
    domains: tuple[Domain, ...]
    other: Domain
    regions: Mapping[str, str]  # M49 region (en) -> fr
    subregions: Mapping[str, str]
    overrides: Mapping[str, Mapping[str, str]]
    by_theme: Mapping[str, str] = field(default_factory=dict)
    by_source: Mapping[str, str] = field(default_factory=dict)
    by_table: Mapping[str, str] = field(default_factory=dict)
    by_connector: Mapping[str, str] = field(default_factory=dict)
    sha256: str = ""

    @property
    def ids(self) -> list[str]:
        return [d.id for d in self.domains]

    def get(self, domain_id: str) -> Domain | None:
        if domain_id == self.other.id:
            return self.other
        return next((d for d in self.domains if d.id == domain_id), None)

    def domain_of_kind(self, kind: str, source_key: str | None = None) -> str:
        if source_key is not None:
            for d in self.domains:
                if any(_match(p, source_key) for p in d.kg_kind_sources.get(kind, ())):
                    return d.id
        for d in self.domains:
            if any(_match(k, kind) for k in d.kg_kinds):
                return d.id
        return self.other.id

    def domain_of_table(
        self, dataset_key: str, source: str, themes: Iterable[str] = (), connectors: Iterable[str] = ()
    ) -> tuple[str, str]:
        """(domain id, basis) of one atlas table: explicit table, declaring connector, atlas source, majority catalog
        theme, other."""
        if dataset_key in self.by_table:
            return self.by_table[dataset_key], "table"
        for c in connectors:
            if c in self.by_connector:
                return self.by_connector[c], "connector"
        if source in self.by_source:
            return self.by_source[source], "source"
        counts = Counter(self.by_theme[t] for t in themes if t in self.by_theme)
        if counts:
            order = {d: i for i, d in enumerate(self.ids)}
            best = sorted(counts.items(), key=lambda kv: (-kv[1], order.get(kv[0], 999)))[0][0]
            return best, "catalog_theme"
        return self.other.id, "unclassified"


def problems(doc: Any, known_themes: Iterable[str] | None = None) -> list[str]:
    """Validation problems of a domains document (empty list when valid)."""
    out: list[str] = []
    if not isinstance(doc, dict) or doc.get("schema") != "reva-v8-coverage-domains/1":
        return ["schema must be reva-v8-coverage-domains/1"]
    seen: dict[str, dict[str, str]] = {"id": {}, "theme": {}, "source": {}, "kind": {}, "table": {}, "connector": {}}
    for d in doc.get("domains") or []:
        did = d.get("id")
        if not isinstance(did, str) or not did or not d.get("fr") or not d.get("en"):
            out.append(f"domain {did!r}: id, fr and en are required")
            continue
        for key, vals in (
            ("id", [did]),
            ("theme", d.get("themes") or []),
            ("source", d.get("sources") or []),
            ("kind", d.get("kg_kinds") or []),
            ("table", d.get("tables") or []),
            ("connector", d.get("connectors") or []),
        ):
            for v in vals:
                if v in seen[key]:
                    out.append(f"{key} {v!r} is in two domains ({seen[key][v]}, {did})")
                seen[key][v] = did
    if known_themes is not None:
        missing = sorted(set(known_themes) - set(seen["theme"]))
        if missing:
            out.append(f"atlas themes without a domain: {missing}")
        unknown = sorted(set(seen["theme"]) - set(known_themes))
        if unknown:
            out.append(f"unknown atlas themes: {unknown}")
        for kind, srcs in (d.get("kg_kind_sources") or {}).items():
            for src in srcs:
                k = f"{kind}|{src}"
                if k in seen.setdefault("kind_source", {}):
                    out.append(f"kind/source {k!r} is in two domains ({seen['kind_source'][k]}, {did})")
                seen["kind_source"][k] = did
    other = doc.get("other") or {}
    if other.get("id") != OTHER:
        out.append("other.id must be 'other'")
    for iso3, o in (doc.get("region_overrides") or {}).items():
        if o.get("region") not in (doc.get("regions") or {}) or o.get("subregion") not in (doc.get("subregions") or {}):
            out.append(f"region override {iso3}: unknown region/subregion")
        if not o.get("basis"):
            out.append(f"region override {iso3}: basis required")
    return out


def _sha(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


@lru_cache(maxsize=4)
def load_domains(path: Path = DOMAINS_PATH) -> DomainRegistry:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    bad = problems(doc)
    if bad:
        raise ValueError(f"{path}: {bad}")
    domains = tuple(
        Domain(
            id=d["id"],
            fr=d["fr"],
            en=d["en"],
            themes=tuple(d.get("themes") or ()),
            sources=tuple(d.get("sources") or ()),
            kg_kinds=tuple(d.get("kg_kinds") or ()),
            kg_kind_sources={k: tuple(v) for k, v in (d.get("kg_kind_sources") or {}).items()},
        )
        for d in doc["domains"]
    )
    o = doc["other"]
    return DomainRegistry(
        domains=domains,
        other=Domain(id=o["id"], fr=o["fr"], en=o["en"]),
        regions={k: v["fr"] for k, v in (doc.get("regions") or {}).items()},
        subregions={k: v["fr"] for k, v in (doc.get("subregions") or {}).items()},
        overrides=dict(doc.get("region_overrides") or {}),
        by_theme={t: d.id for d in domains for t in d.themes},
        by_source={s: d.id for d in domains for s in d.sources},
        by_table={t: dd["id"] for dd in doc["domains"] for t in (dd.get("tables") or [])},
        by_connector={c: dd["id"] for dd in doc["domains"] for c in (dd.get("connectors") or [])},
        sha256=_sha(path),
    )


@lru_cache(maxsize=2)
def load_places(crosswalk: Path = CROSSWALK_PATH, qids: Path = COUNTRY_QIDS_PATH) -> tuple[Place, ...]:
    """Every crosswalk entity with its M49 region (or the REVA placement of domains.yaml), sorted by ISO3."""
    reg = load_domains()
    cw = json.loads(crosswalk.read_text(encoding="utf-8"))
    fr: dict[str, str] = {}
    try:
        for k, v in json.loads(qids.read_text(encoding="utf-8")).get("countries", {}).items():
            if v.get("label_fr"):
                fr[k] = str(v["label_fr"])
    except FileNotFoundError:  # pragma: no cover - registry file always present in the repo
        pass
    out: list[Place] = []
    for e in cw["entities"]:
        iso3 = str(e["key"])
        region, sub, placement, basis = e.get("m49_region"), e.get("m49_subregion"), "m49", None
        if iso3 in reg.overrides:
            o = reg.overrides[iso3]
            region, sub, placement, basis = o["region"], o["subregion"], "reva", o.get("basis")
        if not region or not sub:
            region, sub, placement, basis = "Antarctica", "Antarctica", "reva", "no M49 region in the crosswalk"
        out.append(
            Place(
                iso3=iso3,
                label=str(e.get("label") or iso3),
                label_fr=e.get("label_fr") or fr.get(iso3),
                iso2=e.get("iso2"),
                region=str(region),
                subregion=str(sub),
                placement=placement,
                placement_basis=basis,
            )
        )
    return tuple(sorted(out, key=lambda p: p.iso3))


def country_qid_map(qids: Path = COUNTRY_QIDS_PATH) -> dict[str, str]:
    """Wikidata QID -> crosswalk key (reviewed P298 / enwiki mapping of registry/entities/country_qids.json)."""
    doc = json.loads(qids.read_text(encoding="utf-8"))
    return {str(v["qid"]): str(k) for k, v in doc.get("countries", {}).items() if v.get("qid")}
