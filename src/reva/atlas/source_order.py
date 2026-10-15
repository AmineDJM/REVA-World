"""REVA headline concepts with a cross-source fallback order (V3, agent ATLASOPS).

``registry/atlas/source_order/<theme>.yaml`` (schema ``_schema.json``) declares *REVA headline
concepts* (``reva.<theme>.<name>``): population, area, GDP, unemployment, wages, life expectancy, HDI,
CO2 per capita, government balance, trade balance, inflation, Gini, internet use, homicide rate...
Each lists, in best-available order, provider series that **define the same quantity the same way**,
each with a written justification (and caveats); series that do not are recorded under
``not_equivalent`` with the reason and never answer. Optionally a ``derived`` step computes the
concept with an exact formula (``ratio`` / ``difference``) from two sourced inputs of the same country
and calendar year, used only when no listed series reports the cell.

Unlike the provider catalog (:mod:`reva.atlas.catalog`, one provider family per concept), headline
concepts are REVA's own cross-provider equivalences: still ``provider_declared`` series, but the
equivalence itself is an agent judgement and says so (``review_status``).

The query side (``reva.atlas.query.AtlasQuery.headline_*``) picks per (country, calendar year) the
first listed series that reports a value (projections excluded unless allowed), names it, and lists
every series consulted with its status (answered / also reported / no value / projection only /
unit mismatch / not in the snapshot).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

import jsonschema
import yaml

from reva.atlas.catalog import THEMES_PATH, Series, load_themes
from reva.kernel.errors import ValidationProblem
from reva.ontology.registry import REGISTRY_ROOT

SOURCE_ORDER_DIR = REGISTRY_ROOT / "atlas" / "source_order"
SCHEMA_PATH = SOURCE_ORDER_DIR / "_schema.json"
REVIEW_STATUS = "agent_declared_equivalence_not_human_reviewed"


@dataclass(frozen=True)
class HeadlineSeries:
    series: Series
    justification: str
    scale: Decimal = Decimal(1)
    reported_unit: str | None = None
    caveats: str | None = None

    @property
    def key(self) -> str:
        return self.series.key

    def as_dict(self) -> dict[str, Any]:
        return {
            "series": self.key,
            "justification": self.justification,
            "scale": str(self.scale),
            "reported_unit": self.reported_unit,
            "caveats": self.caveats,
        }


@dataclass(frozen=True)
class Operand:
    concept: str | None = None
    series: Series | None = None
    scale: Decimal = Decimal(1)
    reported_unit: str | None = None

    @property
    def label(self) -> str:
        return self.concept if self.concept else self.series.key  # type: ignore[union-attr]

    def as_dict(self) -> dict[str, Any]:
        if self.concept:
            return {"concept": self.concept}
        return {"series": self.label, "scale": str(self.scale), "reported_unit": self.reported_unit}


@dataclass(frozen=True)
class Derived:
    formula: str  # ratio | difference
    alternatives: tuple[tuple[Operand, Operand], ...]
    justification: str
    scale: Decimal = Decimal(1)

    def as_dict(self) -> dict[str, Any]:
        return {
            "formula": self.formula,
            "scale": str(self.scale),
            "alternatives": [[a.as_dict(), b.as_dict()] for a, b in self.alternatives],
            "justification": self.justification,
        }


@dataclass(frozen=True)
class HeadlineConcept:
    id: str
    theme: str
    labels: dict[str, str]
    definition: str
    unit: str
    series: tuple[HeadlineSeries, ...]
    derived: Derived | None = None
    projections: str = "exclude"
    caveats: str | None = None
    not_equivalent: tuple[dict[str, str], ...] = ()
    related_catalog_concepts: tuple[str, ...] = ()
    file: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "theme": self.theme,
            "labels": dict(self.labels),
            "definition": self.definition,
            "unit": self.unit,
            "projections": self.projections,
            "series": [s.as_dict() for s in self.series],
            "derived": self.derived.as_dict() if self.derived else None,
            "caveats": self.caveats,
            "not_equivalent": [dict(x) for x in self.not_equivalent],
            "related_catalog_concepts": list(self.related_catalog_concepts),
            "review_status": REVIEW_STATUS,
            "file": self.file,
        }


@dataclass
class HeadlineRegistry:
    concepts: dict[str, HeadlineConcept] = field(default_factory=dict)
    files: dict[str, str] = field(default_factory=dict)  # file -> sha256
    errors: list[dict[str, str]] = field(default_factory=list)

    def by_theme(self, theme: str | None = None) -> list[HeadlineConcept]:
        return [c for c in self.concepts.values() if theme is None or c.theme == theme]

    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(sorted(self.files.items())).encode()).hexdigest()


@lru_cache(maxsize=1)
def _schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text())  # type: ignore[no-any-return]


def _dec(v: Any) -> Decimal:
    return Decimal(str(v)) if v is not None else Decimal(1)


def _operand(o: dict[str, Any]) -> Operand:
    if "concept" in o:
        return Operand(concept=o["concept"])
    return Operand(series=Series.parse(o["series"]), scale=_dec(o.get("scale")), reported_unit=o.get("reported_unit"))


def _check_doc(doc: dict[str, Any], name: str, themes: set[str]) -> list[str]:
    v = jsonschema.Draft202012Validator(_schema())
    errs = [f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}" for e in v.iter_errors(doc)]
    if errs:
        return errs[:20]
    out = []
    if doc["theme"] != Path(name).stem:
        out.append(f"theme {doc['theme']!r} must equal the file name")
    if doc["theme"] not in themes:
        out.append(f"theme {doc['theme']!r} is not declared in registry/atlas/themes.yaml")
    for c in doc["concepts"]:
        if not c["id"].startswith(f"reva.{doc['theme']}."):
            out.append(f"{c['id']}: id must start with reva.{doc['theme']}.")
        keys = [s["series"] for s in c.get("series") or []]
        if len(set(keys)) != len(keys):
            out.append(f"{c['id']}: duplicate series")
        for s in c.get("series") or []:
            if _dec(s.get("scale")) != 1 and not s.get("reported_unit"):
                out.append(f"{c['id']}: {s['series']} has a scale but no reported_unit")
        for alt in (c.get("derived") or {}).get("alternatives") or []:
            for o in alt:
                if "series" in o and _dec(o.get("scale")) != 1 and not o.get("reported_unit"):
                    out.append(f"{c['id']}: derived operand {o['series']} has a scale but no reported_unit")
    return out


def load_headlines(directory: Path = SOURCE_ORDER_DIR, *, strict: bool = True) -> HeadlineRegistry:
    """Load and validate every ``<theme>.yaml`` (``strict`` raises on the first problem)."""
    themes = set(load_themes(THEMES_PATH).labels)
    reg = HeadlineRegistry()
    for p in sorted(x for x in directory.glob("*.y*ml") if not x.name.startswith("_")):
        data = p.read_bytes()
        try:
            doc = yaml.safe_load(data)
            errs = _check_doc(doc, p.name, themes)
        except yaml.YAMLError as e:
            errs = [f"invalid YAML: {str(e)[:200]}"]
        if not errs:
            dup = [c["id"] for c in doc["concepts"] if c["id"] in reg.concepts]
            if dup:
                errs = [f"headline ids already declared: {dup[:10]}"]
        if errs:
            if strict:
                raise ValidationProblem(f"atlas source_order {p.name} invalid", errors=errs)
            reg.errors.extend({"file": p.name, "error": e} for e in errs)
            continue
        reg.files[p.name] = hashlib.sha256(data).hexdigest()
        for c in doc["concepts"]:
            d = c.get("derived")
            reg.concepts[c["id"]] = HeadlineConcept(
                id=c["id"],
                theme=doc["theme"],
                labels=dict(c["labels"]),
                definition=c["definition"],
                unit=c["unit"],
                series=tuple(
                    HeadlineSeries(
                        series=Series.parse(s["series"]),
                        justification=s["justification"],
                        scale=_dec(s.get("scale")),
                        reported_unit=s.get("reported_unit"),
                        caveats=s.get("caveats"),
                    )
                    for s in c.get("series") or ()
                ),
                derived=Derived(
                    formula=d["formula"],
                    alternatives=tuple((_operand(a), _operand(b)) for a, b in d["alternatives"]),
                    justification=d["justification"],
                    scale=_dec(d.get("scale")),
                )
                if d
                else None,
                projections=c.get("projections", "exclude"),
                caveats=c.get("caveats"),
                not_equivalent=tuple(dict(x) for x in c.get("not_equivalent") or ()),
                related_catalog_concepts=tuple(c.get("related_catalog_concepts") or ()),
                file=p.name,
            )
    # derived operands that name concepts must exist and must not be derived-only (no recursion)
    for c in list(reg.concepts.values()):
        for a, b in c.derived.alternatives if c.derived else ():
            for o in (a, b):
                if o.concept is None:
                    continue
                tgt = reg.concepts.get(o.concept)
                msg = None
                if tgt is None:
                    msg = f"{c.id}: derived operand {o.concept} is not a headline concept"
                elif not tgt.series:
                    msg = f"{c.id}: derived operand {o.concept} has no reported series"
                if msg:
                    if strict:
                        raise ValidationProblem(msg)
                    reg.errors.append({"file": c.file, "error": msg})
    return reg


_CACHE: dict[str, Any] = {}


def cached_headlines(directory: Path = SOURCE_ORDER_DIR) -> HeadlineRegistry:
    """Non-strict registry, reloaded when a file changes (mtime/size fingerprint)."""
    fp = tuple(
        (p.name, p.stat().st_mtime_ns, p.stat().st_size)
        for p in [*sorted(directory.glob("*.y*ml")), THEMES_PATH]
        if not p.name.startswith("_")
    )
    hit = _CACHE.get(str(directory))
    if hit is None or hit[0] != fp:
        hit = (fp, load_headlines(directory, strict=False))
        _CACHE[str(directory)] = hit
    return hit[1]  # type: ignore[no-any-return]
