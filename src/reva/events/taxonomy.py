"""Event taxonomy (``registry/events/taxonomy.yaml``): types, roles and the property roles of the event view.

The file is the single place where REVA says which Wikidata classes and which dataset records are *events*, and
what each statement property means for an event (time, place, participant role, structure). Every Wikidata id it
uses was confirmed by a recorded lookup (``registry/events/_recorded/wikidata_verification.json``, written by
``python -m reva.events.verify``); :func:`check_taxonomy` refuses an id without such a record.

Nothing in the taxonomy is causal: participation, place, time and structure (part of, follows) are *context*.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
TAXONOMY_PATH = REPO_ROOT / "registry" / "events" / "taxonomy.yaml"
VERIFICATION_PATH = REPO_ROOT / "registry" / "events" / "_recorded" / "wikidata_verification.json"

TYPE_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,47}$")
QID_ONLY_RE = re.compile(r"^Q[1-9][0-9]{0,11}$")
PID_ANY_RE = re.compile(r"^(P[1-9][0-9]{0,9}|[a-z][a-z0-9_]{1,31}:[a-z][a-z0-9_]{0,63})$")

#: Property roles understood by the view.
TIME_ROLES = ("point_in_time", "start", "end", "inception", "dissolved", "episode_end")
PLACE_ROLES = ("location", "country", "admin_area")
STRUCTURE_ROLES = ("part_of", "has_part", "follows", "followed_by", "significant_event")


@dataclass(frozen=True)
class Role:
    id: str
    label_en: str
    label_fr: str


@dataclass(frozen=True)
class EventType:
    id: str
    label_en: str
    label_fr: str
    parent: str | None
    wikidata_classes: tuple[str, ...]
    roles: tuple[str, ...]
    description: str = ""


@dataclass(frozen=True)
class PropertyRole:
    """What a statement property means for an event.

    ``direction``: ``out`` when the event is the statement's subject, ``in`` when it is the object (e.g. P1344
    *participant in*: ``country --P1344--> event``)."""

    pid: str
    family: str  # time | place | participant | structure | causal | coded
    role: str
    direction: str = "out"
    note: str = ""
    #: When set, the role applies only to statements of this source (e.g. COW's ``P1344`` spells are belligerent
    #: participations; Wikidata's ``P1344`` is generic participation).
    source_key: str | None = None


@dataclass(frozen=True)
class DatasetKind:
    """Dataset records that are events by construction of the dataset (a UCDP conflict, a COW war ...)."""

    id_prefix: str
    source_key: str | None
    type: str
    label_en: str
    coded_type_property: str | None = None
    #: coded value -> type id; ``None`` = the dataset codes such a record as not an event (e.g. an outcome).
    coded_type_map: Mapping[str, str | None] = field(default_factory=dict)
    interval_from_incoming: tuple[str, ...] = ()
    coded_properties: tuple[str, ...] = ()
    note: str = ""
    #: Records of this kind that carry no prefix (e.g. UNTC treaties keyed by their Wikidata QID) are recognised
    #: by a statement of this property from ``source_key``.
    marker_property: str | None = None


@dataclass(frozen=True, eq=False)
class Taxonomy:
    version: int
    types: Mapping[str, EventType]
    roles: Mapping[str, Role]
    properties: tuple[PropertyRole, ...]
    dataset_kinds: tuple[DatasetKind, ...]
    derived: Mapping[str, Any]
    sha256: str
    #: Wikidata class QID -> type ids mapped to it.
    class_index: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    # -- type tree ---------------------------------------------------------------------------------------
    def ancestors(self, type_id: str) -> list[str]:
        out: list[str] = []
        cur = self.types[type_id].parent
        while cur is not None and cur not in out:
            out.append(cur)
            cur = self.types[cur].parent
        return out

    def depth(self, type_id: str) -> int:
        return len(self.ancestors(type_id))

    def descendants(self, type_id: str) -> set[str]:
        out = {type_id}
        changed = True
        while changed:
            changed = False
            for t in self.types.values():
                if t.parent in out and t.id not in out:
                    out.add(t.id)
                    changed = True
        return out

    def most_specific(self, type_ids: Iterable[str]) -> list[str]:
        """Drop types that are ancestors of another type in the set; deepest first, then file order."""
        ids = set(type_ids)
        keep = [t for t in ids if not any(t in self.ancestors(o) for o in ids if o != t)]
        order = list(self.types)
        return sorted(keep, key=lambda t: (-self.depth(t), order.index(t)))

    # -- property roles ----------------------------------------------------------------------------------
    def props(self, family: str, direction: str | None = None) -> dict[str, PropertyRole]:
        """Property id -> role for one family (source-independent entries; see :meth:`role_for`)."""
        return {
            p.pid: p
            for p in self.properties
            if p.family == family and p.source_key is None and (direction is None or p.direction == direction)
        }

    def pids(self, family: str | None = None, direction: str | None = None) -> set[str]:
        return {
            p.pid
            for p in self.properties
            if (family is None or p.family == family) and (direction is None or p.direction == direction)
        }

    def role_for(self, family: str, pid: str, direction: str, source_key: str | None) -> PropertyRole | None:
        """The role of a statement: a source-specific entry first, else the generic one."""
        generic = None
        for p in self.properties:
            if p.family != family or p.pid != pid or p.direction != direction:
                continue
            if p.source_key is not None and p.source_key == source_key:
                return p
            if p.source_key is None:
                generic = p
        return generic

    def dataset_kind_for(self, entity_id: str) -> DatasetKind | None:
        for d in self.dataset_kinds:
            if d.id_prefix and entity_id.startswith(d.id_prefix):
                return d
        return None

    def marker_kinds(self) -> list[DatasetKind]:
        return [d for d in self.dataset_kinds if d.marker_property]

    def role(self, role_id: str) -> dict[str, str]:
        r = self.roles.get(role_id)
        return {"id": role_id, "label_en": r.label_en if r else role_id, "label_fr": r.label_fr if r else role_id}

    def type_ref(self, type_id: str) -> dict[str, Any]:
        t = self.types[type_id]
        return {"id": t.id, "label_en": t.label_en, "label_fr": t.label_fr, "parent": t.parent}


class TaxonomyError(ValueError):
    pass


def _parse(doc: Mapping[str, Any], sha: str) -> Taxonomy:
    roles = {r["id"]: Role(r["id"], r["label_en"], r["label_fr"]) for r in doc.get("roles") or []}
    types: dict[str, EventType] = {}
    for t in doc.get("types") or []:
        types[t["id"]] = EventType(
            id=t["id"],
            label_en=t["label_en"],
            label_fr=t["label_fr"],
            parent=t.get("parent"),
            wikidata_classes=tuple(t.get("wikidata_classes") or ()),
            roles=tuple(t.get("roles") or ()),
            description=t.get("description") or "",
        )
    props = tuple(
        PropertyRole(
            pid=p["pid"],
            family=p["family"],
            role=p["role"],
            direction=p.get("direction", "out"),
            note=p.get("note") or "",
            source_key=p.get("source_key"),
        )
        for p in doc.get("properties") or []
    )
    kinds = tuple(
        DatasetKind(
            id_prefix=d["id_prefix"],
            source_key=d.get("source_key"),
            type=d["type"],
            label_en=d["label_en"],
            coded_type_property=d.get("coded_type_property"),
            coded_type_map=dict(d.get("coded_type_map") or {}),
            interval_from_incoming=tuple(d.get("interval_from_incoming") or ()),
            coded_properties=tuple(d.get("coded_properties") or ()),
            marker_property=d.get("marker_property"),
            note=d.get("note") or "",
        )
        for d in doc.get("dataset_kinds") or []
    )
    idx: dict[str, list[str]] = {}
    for t in types.values():
        for q in t.wikidata_classes:
            idx.setdefault(q, []).append(t.id)
    return Taxonomy(
        version=int(doc.get("version", 1)),
        types=types,
        roles=roles,
        properties=props,
        dataset_kinds=kinds,
        derived=dict(doc.get("derived") or {}),
        sha256=sha,
        class_index={k: tuple(v) for k, v in idx.items()},
    )


def check_taxonomy(tax: Taxonomy, verification: Mapping[str, Any] | None = None) -> list[str]:
    """Problems with the taxonomy (empty = valid). With ``verification`` (the recorded lookups), every Wikidata
    class QID must have a record with labels, and every Wikidata property id a record with a datatype."""
    errs: list[str] = []
    if not tax.types:
        errs.append("no types")
    roots = [t.id for t in tax.types.values() if t.parent is None]
    if len(roots) != 1:
        errs.append(f"exactly one root type expected, got {roots}")
    seen_classes: dict[str, str] = {}
    for t in tax.types.values():
        if not TYPE_ID_RE.match(t.id):
            errs.append(f"type id {t.id!r} invalid")
        if not t.label_en or not t.label_fr:
            errs.append(f"type {t.id}: label_en and label_fr required")
        if t.parent is not None and t.parent not in tax.types:
            errs.append(f"type {t.id}: unknown parent {t.parent!r}")
        for q in t.wikidata_classes:
            if not QID_ONLY_RE.match(q):
                errs.append(f"type {t.id}: invalid class {q!r}")
            if q in seen_classes:
                errs.append(f"class {q} mapped to both {seen_classes[q]} and {t.id}")
            seen_classes[q] = t.id
        for r in t.roles:
            if r not in tax.roles:
                errs.append(f"type {t.id}: unknown role {r!r}")
    for t in tax.types.values():  # cycles
        try:
            anc = tax.ancestors(t.id)
        except KeyError:
            continue
        if t.id in anc:
            errs.append(f"type {t.id}: parent cycle")
    families = {"time": TIME_ROLES, "place": PLACE_ROLES, "structure": STRUCTURE_ROLES}
    seen_props: set[tuple[str, str, str | None]] = set()
    for p in tax.properties:
        if not PID_ANY_RE.match(p.pid):
            errs.append(f"property {p.pid!r} invalid")
        if p.direction not in ("out", "in"):
            errs.append(f"property {p.pid}: direction must be out or in")
        if (p.pid, p.direction, p.source_key) in seen_props:
            errs.append(f"property {p.pid} ({p.direction}) listed twice")
        seen_props.add((p.pid, p.direction, p.source_key))
        if p.family in families and p.role not in families[p.family]:
            errs.append(f"property {p.pid}: role {p.role!r} not in {families[p.family]}")
        elif p.family == "participant" and p.role not in tax.roles:
            errs.append(f"property {p.pid}: unknown participant role {p.role!r}")
        elif p.family not in (*families, "participant", "causal", "coded", "identity"):
            errs.append(f"property {p.pid}: unknown family {p.family!r}")
    for d in tax.dataset_kinds:
        if d.type not in tax.types:
            errs.append(f"dataset kind {d.id_prefix}: unknown type {d.type!r}")
        for v in d.coded_type_map.values():
            if v is not None and v not in tax.types:
                errs.append(f"dataset kind {d.id_prefix}: coded type maps to unknown type {v!r}")
    for name, spec in tax.derived.items():
        if spec.get("type") not in tax.types:
            errs.append(f"derived {name}: unknown type {spec.get('type')!r}")
    if verification is not None:
        ents = verification.get("entities") or {}
        for q in seen_classes:
            rec = ents.get(q)
            if not rec or rec.get("missing") or not (rec.get("labels") or {}).get("en"):
                errs.append(f"class {q} has no recorded verification (run python -m reva.events.verify)")
        for p in tax.properties:
            if p.pid.startswith("P"):
                rec = ents.get(p.pid)
                if not rec or not rec.get("datatype"):
                    errs.append(f"property {p.pid} has no recorded verification")
    return errs


@lru_cache(maxsize=4)
def _load_cached(path: str, mtime: float) -> Taxonomy:
    import hashlib

    raw = Path(path).read_bytes()
    doc = yaml.safe_load(raw)
    if not isinstance(doc, Mapping):
        raise TaxonomyError(f"{path}: not a mapping")
    tax = _parse(doc, hashlib.sha256(raw).hexdigest())
    errs = check_taxonomy(tax)
    if errs:
        raise TaxonomyError("; ".join(errs[:10]))
    return tax


def load_taxonomy(path: Path = TAXONOMY_PATH) -> Taxonomy:
    return _load_cached(str(path), path.stat().st_mtime)


@lru_cache(maxsize=4)
def _verification_cached(path: str, mtime: float) -> dict[str, Any]:
    out: dict[str, Any] = json.loads(Path(path).read_text())
    return out


def load_verification(path: Path = VERIFICATION_PATH) -> dict[str, Any]:
    try:
        return _verification_cached(str(path), path.stat().st_mtime)
    except (OSError, ValueError):
        return {}


@dataclass(frozen=True)
class RecordedClasses:
    """``P279`` claims of class items as recorded by the verification lookups (non-deprecated, with rank,
    revision and raw sha256): the fallback for classes whose ``P279`` statements are not in the KG."""

    parents: Mapping[str, tuple[tuple[str, str, str], ...]]  # class -> ((parent, rank, statement id), ...)
    meta: Mapping[str, Mapping[str, Any]]

    def children(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for c, ps in self.parents.items():
            for p, _rank, _sid in ps:
                out.setdefault(p, []).append(c)
        return out


def recorded_classes(verification: Mapping[str, Any] | None = None) -> RecordedClasses:
    ver = load_verification() if verification is None else verification
    parents: dict[str, tuple[tuple[str, str, str], ...]] = {}
    meta: dict[str, Mapping[str, Any]] = {}
    for q, rec in (ver.get("entities") or {}).items():
        if not isinstance(rec, Mapping) or rec.get("missing") or not str(q).startswith("Q"):
            continue
        ps = tuple(
            (c["o"], c.get("rank") or "normal", c.get("statement") or "")
            for c in rec.get("P279") or []
            if c.get("o") and c.get("rank") != "deprecated"
        )
        if ps:
            parents[q] = ps
        meta[q] = {
            "lastrevid": rec.get("lastrevid"),
            "raw_sha256": rec.get("raw_sha256"),
            "retrieved_at": rec.get("retrieved_at"),
            "labels": rec.get("labels") or {},
        }
    return RecordedClasses(parents, meta)


def taxonomy_summary(tax: Taxonomy) -> list[dict[str, Any]]:
    """The type tree for the API (`GET /v1/events/types`)."""
    return [
        {
            **tax.type_ref(t.id),
            "depth": tax.depth(t.id),
            "wikidata_classes": list(t.wikidata_classes),
            "roles": [tax.role(r) for r in t.roles],
            "description": t.description,
            "dataset_kinds": [
                d.id_prefix for d in tax.dataset_kinds if d.type == t.id or t.id in d.coded_type_map.values()
            ],
        }
        for t in tax.types.values()
    ]


def all_type_ids(tax: Taxonomy, requested: Sequence[str] | None) -> set[str]:
    """Requested types and their descendants (all types when ``requested`` is empty)."""
    if not requested:
        return set(tax.types)
    out: set[str] = set()
    for r in requested:
        if r not in tax.types:
            raise TaxonomyError(f"unknown event type {r!r}")
        out |= tax.descendants(r)
    return out
