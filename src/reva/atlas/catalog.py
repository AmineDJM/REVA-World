"""Atlas concept catalog: ``registry/atlas/catalog/<domain>.yaml`` (schema ``_schema.json``).

Each file declares the concepts of one domain. A concept names its provider series in
best-available order (``source_order``: ``"<source>.<dataset>.<indicator>"`` — source and
dataset have no dots, so the string splits on its first two dots; or a mapping
``{source, dataset, indicator}``). Concepts are **provider-declared** (definitions as the
providers publish them, not human-reviewed) and say so (``provider_declared: true``).

:func:`load_catalog` validates every file; with ``strict=False`` (the API) an invalid file is
skipped and reported in ``Catalog.errors`` instead of breaking every other domain.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import jsonschema
import yaml

from reva.kernel.errors import ValidationProblem
from reva.ontology.registry import REGISTRY_ROOT

CATALOG_DIR = REGISTRY_ROOT / "atlas" / "catalog"
SCHEMA_PATH = CATALOG_DIR / "_schema.json"


@dataclass(frozen=True)
class Series:
    source: str
    dataset: str
    indicator: str

    @property
    def key(self) -> str:
        return f"{self.source}.{self.dataset}.{self.indicator}"

    @classmethod
    def parse(cls, item: str | dict[str, Any]) -> Series:
        if isinstance(item, dict):
            return cls(item["source"], item["dataset"], item["indicator"])
        parts = item.split(".", 2)
        if len(parts) != 3 or not all(parts):
            raise ValidationProblem(f"series {item!r} is not '<source>.<dataset>.<indicator>'")
        return cls(*parts)


@dataclass(frozen=True)
class Concept:
    id: str
    labels: dict[str, str]
    definition: str
    unit: str
    domain: str
    subdomain: str | None
    source_order: tuple[Series, ...]
    notes: str | None = None
    value_kind: str = "numeric"
    tags: tuple[str, ...] = ()
    file: str = ""
    provider_declared: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "labels": dict(self.labels),
            "definition": self.definition,
            "unit": self.unit,
            "domain": self.domain,
            "subdomain": self.subdomain,
            "value_kind": self.value_kind,
            "source_order": [s.key for s in self.source_order],
            "provider_declared": self.provider_declared,
            "review_status": "provider_declared_not_human_reviewed",
            "notes": self.notes,
            "tags": list(self.tags),
            "catalog_file": self.file,
        }


@dataclass
class Catalog:
    concepts: dict[str, Concept] = field(default_factory=dict)
    domains: dict[str, dict[str, Any]] = field(default_factory=dict)
    errors: list[dict[str, str]] = field(default_factory=list)
    files: dict[str, str] = field(default_factory=dict)  # file name -> sha256 of bytes

    def by_domain(self, domain: str | None = None) -> list[Concept]:
        return [c for c in self.concepts.values() if domain is None or c.domain == domain]

    def search(self, q: str | None = None, domain: str | None = None, subdomain: str | None = None) -> list[Concept]:
        needle = (q or "").strip().lower()
        out = []
        for c in self.concepts.values():
            if domain and c.domain != domain:
                continue
            if subdomain and c.subdomain != subdomain:
                continue
            if needle:
                hay = " ".join([c.id, c.labels.get("fr", ""), c.labels.get("en", ""), c.definition, *c.tags]).lower()
                if needle not in hay:
                    continue
            out.append(c)
        return sorted(out, key=lambda c: (c.domain, c.subdomain or "", c.id))

    def series_rows(self, concept_ids: Iterable[str] | None = None) -> list[tuple[str, int, str, str, str]]:
        """(concept_id, rank, source, dataset, indicator) for the given (default: all) concepts."""
        ids = list(self.concepts) if concept_ids is None else list(concept_ids)
        return [
            (cid, rank, s.source, s.dataset, s.indicator)
            for cid in ids
            if cid in self.concepts
            for rank, s in enumerate(self.concepts[cid].source_order)
        ]


@lru_cache(maxsize=1)
def catalog_schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text())  # type: ignore[no-any-return]


def _validate_doc(doc: Any, name: str) -> list[str]:
    v = jsonschema.Draft202012Validator(catalog_schema())
    errs = [f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}" for e in v.iter_errors(doc)]
    if errs:
        return errs[:20]
    out = []
    stem = Path(name).stem
    if doc["domain"] != stem:
        out.append(f"domain {doc['domain']!r} must equal the file name {stem!r}")
    for c in doc["concepts"]:
        if c["domain"] != doc["domain"]:
            out.append(f"concept {c['id']}: domain {c['domain']!r} != file domain {doc['domain']!r}")
        keys = [Series.parse(s).key for s in c["source_order"]]
        if len(set(keys)) != len(keys):
            out.append(f"concept {c['id']}: duplicate series in source_order")
    return out


def load_catalog(directory: Path = CATALOG_DIR, *, strict: bool = True) -> Catalog:
    """Load and validate every ``<domain>.yaml``. ``strict`` raises on the first problem."""
    import hashlib

    cat = Catalog()
    files = sorted(p for p in directory.glob("*.y*ml") if not p.name.startswith("_"))
    for p in files:
        data = p.read_bytes()
        try:
            doc = yaml.safe_load(data)
        except yaml.YAMLError as e:
            errs = [f"invalid YAML: {str(e)[:200]}"]
        else:
            errs = _validate_doc(doc, p.name)
        if not errs:
            dup = [c["id"] for c in doc["concepts"] if c["id"] in cat.concepts]
            if dup:
                errs = [f"concept ids already declared in another file: {', '.join(dup[:10])}"]
        if errs:
            if strict:
                raise ValidationProblem(f"atlas catalog {p.name} invalid", errors=errs)
            cat.errors.extend({"file": p.name, "error": e} for e in errs)
            continue
        cat.files[p.name] = hashlib.sha256(data).hexdigest()
        cat.domains[doc["domain"]] = {"labels": doc.get("labels"), "file": p.name, "concepts": len(doc["concepts"])}
        for c in doc["concepts"]:
            cat.concepts[c["id"]] = Concept(
                id=c["id"],
                labels=dict(c["labels"]),
                definition=c["definition"],
                unit=c["unit"],
                domain=c["domain"],
                subdomain=c.get("subdomain"),
                source_order=tuple(Series.parse(s) for s in c["source_order"]),
                notes=c.get("notes"),
                value_kind=c.get("value_kind", "numeric"),
                tags=tuple(c.get("tags") or ()),
                file=p.name,
            )
    return cat


_CACHE: dict[str, Any] = {}


def cached_catalog(directory: Path = CATALOG_DIR) -> Catalog:
    """Non-strict catalog, reloaded when any catalog file changes (mtime/size fingerprint)."""
    fp = tuple(
        (p.name, p.stat().st_mtime_ns, p.stat().st_size)
        for p in sorted(directory.glob("*.y*ml"))
        if not p.name.startswith("_")
    )
    key = str(directory)
    hit = _CACHE.get(key)
    if hit is None or hit[0] != fp:
        hit = (fp, load_catalog(directory, strict=False))
        _CACHE[key] = hit
    return hit[1]  # type: ignore[no-any-return]


SERIES_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,23}\.[a-z0-9][a-z0-9_-]{0,38}\..+$")
