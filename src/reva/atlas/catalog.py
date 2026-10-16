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
THEMES_PATH = REGISTRY_ROOT / "atlas" / "themes.yaml"


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
    theme: str | None = None
    fr_label: str | None = None  # "missing": no French label yet (labels has no "fr")

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "labels": dict(self.labels),
            "definition": self.definition,
            "unit": self.unit,
            "domain": self.domain,
            "subdomain": self.subdomain,
            "theme": self.theme,
            "fr_label": self.fr_label,
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
    themes: dict[str, dict[str, str]] = field(default_factory=dict)  # theme id -> labels
    unthemed: list[str] = field(default_factory=list)  # concepts no theme rule/default covers (reported)

    def by_domain(self, domain: str | None = None, theme: str | None = None) -> list[Concept]:
        return [
            c
            for c in self.concepts.values()
            if (domain is None or c.domain == domain) and (theme is None or c.theme == theme)
        ]

    def search(
        self,
        q: str | None = None,
        domain: str | None = None,
        subdomain: str | None = None,
        theme: str | None = None,
    ) -> list[Concept]:
        needle = (q or "").strip().lower()
        out = []
        for c in self.concepts.values():
            if domain and c.domain != domain:
                continue
            if theme and c.theme != theme:
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
    for c in doc["concepts"]:
        if "fr" not in c["labels"] and c.get("fr_label") != "missing":
            out.append(f"concept {c['id']}: labels.fr missing (declare fr_label: missing, never copy English)")
        if "fr" in c["labels"] and c.get("fr_label") == "missing":
            out.append(f"concept {c['id']}: fr_label: missing but labels.fr is given")
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


@dataclass(frozen=True)
class Themes:
    """``registry/atlas/themes.yaml``: theme labels, ordered rules and per-domain defaults."""

    labels: dict[str, dict[str, str]]
    rules: tuple[dict[str, str], ...]
    domain_defaults: dict[str, str]
    subdomain_themed_domains: frozenset[str] = frozenset()
    subdomain_aliases: dict[str, str] = field(default_factory=dict)

    def theme_of(self, domain: str, subdomain: str | None, concept_id: str, own: str | None = None) -> str | None:
        if own:
            return own
        for r in self.rules:
            if r.get("domain") not in (None, domain):
                continue
            if "subdomain" in r and r["subdomain"] != subdomain:
                continue
            if "id_prefix" in r and not concept_id.startswith(r["id_prefix"]):
                continue
            return r["theme"]
        if domain in self.subdomain_themed_domains and subdomain:
            if subdomain in self.labels:
                return subdomain
            if subdomain in self.subdomain_aliases:
                return self.subdomain_aliases[subdomain]
        return self.domain_defaults.get(domain)


def load_themes(path: Path = THEMES_PATH) -> Themes:
    """Load and check the theme registry (every rule/default names a declared theme)."""
    doc = yaml.safe_load(path.read_text()) or {}
    labels = {str(k): dict(v) for k, v in (doc.get("themes") or {}).items()}
    rules = tuple(dict(r) for r in (doc.get("rules") or ()))
    defaults = {str(k): str(v) for k, v in (doc.get("domain_defaults") or {}).items()}
    aliases = {str(k): str(v) for k, v in (doc.get("subdomain_aliases") or {}).items()}
    bad = sorted(
        {r.get("theme", "") for r in rules if r.get("theme") not in labels}
        | {v for v in defaults.values() if v not in labels}
        | {v for v in aliases.values() if v not in labels}
    )
    if bad or any(set(r) - {"domain", "subdomain", "id_prefix", "theme"} for r in rules):
        raise ValidationProblem("atlas themes.yaml invalid", unknown_themes=bad)
    for k, lab in labels.items():
        if not re.match(r"^[a-z][a-z0-9_]{0,39}$", k) or not lab.get("fr") or not lab.get("en"):
            raise ValidationProblem("atlas theme needs an id and fr/en labels", theme=k)
    return Themes(labels, rules, defaults, frozenset(doc.get("subdomain_themed_domains") or ()), aliases)


#: Safe YAML loader, libyaml-backed when available (10x faster on the 9 MB World Bank catalog).
_YAML_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
_PARSED: dict[tuple[str, str], tuple[Any, list[str]]] = {}


_PARSE_FORMAT = "atlas-catalog-parsed/1"


def _disk_path(name: str, sha: str) -> Path | None:
    """JSON cache of a parsed + validated catalog file (big files: YAML parse + schema validation of
    the 9 MB World Bank catalog take ~15 s). Keyed by the file's and the schema's sha256 and the cache
    format; JSON only (never pickle), written atomically; any read problem means a fresh parse."""
    import hashlib

    from reva.atlas.cachedir import cache_dir, private_dir

    if len(sha) != 64:
        return None
    try:
        base = private_dir(cache_dir() / "catalog")  # private (0700, owned by this user): ATLAS-1
    except Exception:  # a cache problem never blocks loading the catalog
        return None
    schema = hashlib.sha256(SCHEMA_PATH.read_bytes() + _PARSE_FORMAT.encode()).hexdigest()[:16]
    return base / f"{Path(name).stem}-{sha}-{schema}.json"


def _disk_cached(name: str, sha: str) -> tuple[Any, list[str]] | None:
    p = _disk_path(name, sha)
    try:
        if p is None or not p.exists() or p.stat().st_size < 1_000_000:  # only worth it for big files
            return None
        d = json.loads(p.read_bytes())
        if d.get("format") != _PARSE_FORMAT or d.get("sha256") != sha or d.get("file") != name:
            return None
        return d["doc"], list(d["errors"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _disk_store(name: str, sha: str, hit: tuple[Any, list[str]]) -> None:
    import os
    import tempfile

    p = _disk_path(name, sha)
    if p is None or hit[1]:
        return
    try:
        blob = json.dumps({"format": _PARSE_FORMAT, "file": name, "sha256": sha, "doc": hit[0], "errors": hit[1]})
        if len(blob) < 1_000_000:
            return
        fd, tmp = tempfile.mkstemp(dir=p.parent, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            f.write(blob)
        os.replace(tmp, p)
    except Exception:  # a cache, never fatal
        return


def _parsed(name: str, data: bytes) -> tuple[Any, list[str]]:
    """Parsed + validated document, cached per process by (file name, sha256 of its bytes): a file
    is re-parsed and re-validated only when its content changes. Cached docs are never mutated."""
    import hashlib

    key = (name, hashlib.sha256(data).hexdigest())
    hit = _PARSED.get(key)
    if hit is None:
        hit = _disk_cached(name, key[1])
    if hit is None:
        try:
            doc = yaml.load(data, Loader=_YAML_LOADER)  # noqa: S506 - SafeLoader / CSafeLoader only
        except yaml.YAMLError as e:
            hit = (None, [f"invalid YAML: {str(e)[:200]}"])
        else:
            hit = (doc, _validate_doc(doc, name))
            _disk_store(name, key[1], hit)
        for k in [k for k in _PARSED if k[0] == name]:  # one cached version per file
            _PARSED.pop(k)
        _PARSED[key] = hit
    return hit


def load_catalog(directory: Path = CATALOG_DIR, *, strict: bool = True, themes_path: Path = THEMES_PATH) -> Catalog:
    """Load and validate every ``<domain>.yaml``. ``strict`` raises on the first problem."""
    import hashlib

    themes = load_themes(themes_path)
    cat = Catalog(themes=dict(themes.labels))
    files = sorted(p for p in directory.glob("*.y*ml") if not p.name.startswith("_"))
    for p in files:
        data = p.read_bytes()
        doc, errs = _parsed(p.name, data)
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
            theme = themes.theme_of(c["domain"], c.get("subdomain"), c["id"], c.get("theme"))
            if theme not in themes.labels:  # reported, never fatal: a new domain must not break loading
                cat.unthemed.append(c["id"])
                theme = None
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
                theme=theme,
                fr_label=c.get("fr_label"),
            )
    return cat


_CACHE: dict[str, Any] = {}


def cached_catalog(directory: Path = CATALOG_DIR) -> Catalog:
    """Non-strict catalog, reloaded when any catalog file changes (mtime/size fingerprint)."""
    fp = tuple(
        (p.name, p.stat().st_mtime_ns, p.stat().st_size)
        for p in [*sorted(directory.glob("*.y*ml")), THEMES_PATH]
        if not p.name.startswith("_")
    )
    key = str(directory)
    hit = _CACHE.get(key)
    if hit is None or hit[0] != fp:
        hit = (fp, load_catalog(directory, strict=False))
        _CACHE[key] = hit
    return hit[1]  # type: ignore[no-any-return]


SERIES_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,23}\.[a-z0-9][a-z0-9_-]{0,38}\..+$")
