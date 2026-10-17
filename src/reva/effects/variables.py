"""Effect variables (``registry/effects/variables.yaml``) and id resolution for causes and effects.

Resolution is by identifier only: an atlas concept id must exist in the atlas catalog, a variable id in
``variables.yaml``, an event type id in ``registry/events/taxonomy.yaml``. Nothing is matched by name."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from reva.effects.vocab import VAR_ID_RE
from reva.kernel.errors import ValidationProblem
from reva.ontology.registry import REGISTRY_ROOT

VARIABLES_PATH = REGISTRY_ROOT / "effects" / "variables.yaml"


@dataclass(frozen=True)
class EffectVariable:
    id: str
    labels: dict[str, str]
    definition: str
    unit: str
    atlas_concepts: tuple[str, ...]
    atlas_note: str | None = None
    #: atlas concepts that are the (annual) growth rate of this level variable -- a curated identifier link,
    #: not the same quantity (FIX4B, audit M3): a cumulative-growth response is written on the level variable.
    growth_concepts: tuple[str, ...] = ()
    #: the effect variable that is the growth rate of this level variable (curated id link, FIX4B)
    growth_variable: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "labels": dict(self.labels),
            "definition": self.definition,
            "definition_by": "reva",
            "unit": self.unit,
            "atlas_concepts": list(self.atlas_concepts),
            "atlas_note": self.atlas_note,
        }


@dataclass(frozen=True)
class Variables:
    items: dict[str, EffectVariable]
    sha256: str

    def by_atlas_concept(self, concept_id: str) -> list[str]:
        return sorted(v.id for v in self.items.values() if concept_id in v.atlas_concepts)

    def level_of_growth_concept(self, concept_id: str) -> list[str]:
        """Level variables whose curated ``growth_concepts`` list this growth-rate concept."""
        return sorted(v.id for v in self.items.values() if concept_id in v.growth_concepts)

    def growth_ids(self) -> dict[str, set[str]]:
        """Ids that are growth rates, per kind (``variable`` / ``atlas_concept``), from the curated links."""
        var: set[str] = set()
        con: set[str] = set()
        for v in self.items.values():
            con.update(v.growth_concepts)
            if v.growth_variable:
                var.add(v.growth_variable)
        for gid in list(var):
            gv = self.items.get(gid)
            if gv is not None:
                con.update(gv.atlas_concepts)
        return {"variable": var, "atlas_concept": con}


def parse_variables(data: bytes, *, known_concepts: set[str] | None = None) -> Variables:
    doc = yaml.safe_load(data)
    if not isinstance(doc, dict) or doc.get("schema") != "reva-effect-variables/1":
        raise ValidationProblem("variables.yaml: schema must be reva-effect-variables/1")
    out: dict[str, EffectVariable] = {}
    for i, v in enumerate(doc.get("variables") or []):
        vid = v.get("id") if isinstance(v, dict) else None
        if not isinstance(vid, str) or not VAR_ID_RE.match(vid) or vid in out:
            raise ValidationProblem("variables.yaml: invalid or duplicate id", index=i, id=str(vid))
        labels = v.get("labels") or {}
        if not isinstance(labels, dict) or not labels.get("en") or not labels.get("fr"):
            raise ValidationProblem("variables.yaml: en and fr labels are required", id=vid)
        if not str(v.get("definition") or "").strip() or not str(v.get("unit") or "").strip():
            raise ValidationProblem("variables.yaml: definition and unit are required", id=vid)
        concepts = tuple(v.get("atlas_concepts") or ())
        growth = tuple(v.get("growth_concepts") or ())
        gvar = v.get("growth_variable")
        if gvar is not None and (not isinstance(gvar, str) or not VAR_ID_RE.match(gvar)):
            raise ValidationProblem("variables.yaml: invalid growth_variable", id=vid)
        if known_concepts is not None:
            bad = [c for c in (*concepts, *growth) if c not in known_concepts]
            if bad:
                raise ValidationProblem("variables.yaml: unknown atlas concept", id=vid, concepts=bad)
        out[vid] = EffectVariable(
            vid,
            {"en": str(labels["en"]), "fr": str(labels["fr"])},
            str(v["definition"]).strip(),
            str(v["unit"]).strip(),
            concepts,
            v.get("atlas_note"),
            growth,
            gvar,
        )
    for vid, ev in out.items():
        if ev.growth_variable and ev.growth_variable not in out:
            raise ValidationProblem(
                "variables.yaml: unknown growth_variable", id=vid, growth_variable=ev.growth_variable
            )
    return Variables(out, hashlib.sha256(data).hexdigest())


@lru_cache(maxsize=4)
def _cached(path: str, mtime: float) -> Variables:
    return parse_variables(Path(path).read_bytes())


def load_variables(path: Path = VARIABLES_PATH) -> Variables:
    return _cached(str(path), path.stat().st_mtime)


@dataclass(frozen=True)
class Resolver:
    """Which ids exist, per kind. ``atlas`` / ``event_types`` = None skips that check (pure unit tests)."""

    variables: Variables
    atlas: dict[str, dict[str, str]] | None  # concept id -> labels
    event_types: dict[str, dict[str, str]] | None  # type id -> labels

    def growth_info(self, kind: str, id_: str) -> tuple[bool, str | None]:
        """(is a growth rate, curated growth variable of this level variable) -- identifier links only."""
        is_growth = id_ in self.variables.growth_ids().get(kind, set())
        gvar = self.variables.items[id_].growth_variable if kind == "variable" and id_ in self.variables.items else None
        return is_growth, gvar

    def known(self, kind: str, id_: str) -> bool:
        if kind == "variable":
            return id_ in self.variables.items
        if kind == "atlas_concept":
            return self.atlas is None or id_ in self.atlas
        if kind == "event_type":
            return self.event_types is None or id_ in self.event_types
        return kind == "event"

    def labels(self, kind: str, id_: str) -> dict[str, str] | None:
        if kind == "variable" and id_ in self.variables.items:
            return dict(self.variables.items[id_].labels)
        if kind == "atlas_concept" and self.atlas and id_ in self.atlas:
            return self.atlas[id_]
        if kind == "event_type" and self.event_types and id_ in self.event_types:
            return self.event_types[id_]
        return None


@lru_cache(maxsize=1)
def _atlas_labels() -> dict[str, dict[str, str]]:
    from reva.atlas.catalog import cached_catalog

    cat = cached_catalog()
    return {
        cid: {"en": c.labels.get("en", cid), "fr": c.labels.get("fr", c.labels.get("en", cid))}
        for cid, c in cat.concepts.items()
    }


@lru_cache(maxsize=1)
def _event_type_labels() -> dict[str, dict[str, str]]:
    from reva.events.taxonomy import load_taxonomy

    tax = load_taxonomy()
    out: dict[str, dict[str, str]] = {}
    for tid, t in tax.types.items():
        en = getattr(t, "label_en", None) or tid
        out[tid] = {"en": str(en), "fr": str(getattr(t, "label_fr", None) or en)}
    return out


def default_resolver() -> Resolver:
    return Resolver(load_variables(), _atlas_labels(), _event_type_labels())
