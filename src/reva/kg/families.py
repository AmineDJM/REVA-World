"""Universal relation families (REVA V4): resolver from source properties to families.

``registry/kg/relations_v4.yaml`` (owner V4CORE) defines the families (``is_a``, ``part_of``/``has_part``,
``made_of``, ``located_in``, ``occurs_during``, ``produces``/``produced_by``, ``owns``/``owned_by``,
``supplies``/``buys_from``, ``trades_with``, ``eats``, ``interacts_with``, ``associated_with``, ``measured_by``,
``discovered_by``, ``invented_by``, ``regulates``, ``context``) and the core mappings; every V4 agent appends its
own mappings in ``registry/kg/relations_v4.d/<agent>.yaml`` (format: ``docs/dev/v4_v4core.md``). Both are checked
against ``registry/kg/relations_v4.schema.json`` plus the semantic rules of :func:`validate`.

Honesty:

* No family is causal. A source-asserted causal property (``never_map``) can never be mapped; it is read through
  the causal loader / ``/v1/causal`` as an attributed claim.
* A mapping only says how to *read* an edge the source published. It never merges entities and never maps by
  name similarity.
* A share (``made_of`` composition, ``owned_by`` stake) is read only from the qualifier the mapping names; when
  absent it is ``None`` (Missing), never zero.

Usage::

    reg = load_registry()
    reg.edge_family("P361", "out")   # -> EdgeFamily(family='part_of', label='fait partie de', ...)
    reg.edge_family("P361", "in")    # -> EdgeFamily(family='has_part', ...)
    reg.properties_for({"made_of"})  # -> {('P186', 'out'), ...}  (pid, traversal direction from the subject side)

CLI: ``uv run python -m reva.kg.families check`` validates every file and prints a one-line summary per family.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import jsonschema
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
KG_REGISTRY = REPO_ROOT / "registry" / "kg"
RELATIONS_FILE = KG_REGISTRY / "relations_v4.yaml"
RELATIONS_DIR = KG_REGISTRY / "relations_v4.d"
RELATIONS_SCHEMA = KG_REGISTRY / "relations_v4.schema.json"
PROPERTY_REQUESTS_DIR = KG_REGISTRY / "property_requests"

PID_RE = re.compile(r"^(P[1-9][0-9]{0,9}|[a-z][a-z0-9_]{1,31}:[a-z][a-z0-9_]{0,63})$")
WIKIDATA_PID_RE = re.compile(r"^P[1-9][0-9]{0,9}$")
#: Source-asserted causal Wikidata properties (KGCORE ``CAUSAL_PIDS``): never a family.
CAUSAL_PIDS = frozenset({"P828", "P1542", "P1478", "P1479", "P1536", "P1537"})

Direction = Literal["out", "in"]


class FamilyError(ValueError):
    pass


def _measured_by_property() -> Any:
    from reva.kg.model import KgProperty

    return KgProperty(
        pid="atlas:measured_by",
        value_kind="item",
        reva_predicate="atlas_measured_by",
        label_en="is measured by the atlas series",
        label_fr="est mesuré par la série de l'atlas",
        inverse_name="atlas_series_about",
        inverse_label_en="atlas series about",
        inverse_label_fr="série de l'atlas portant sur",
    )


#: Dataset predicate "entity -> atlas series" (family ``measured_by``): object ``atlas:<catalog concept id>``,
#: qualifiers ``concept`` (catalog id), ``iso3`` (geography of the series, when one) and ``table``. A loader
#: registers it with :func:`reva.kg.model.register_properties` before loading such statements.
ATLAS_MEASURED_BY = _measured_by_property()


@dataclass(frozen=True)
class Family:
    id: str
    label: Mapping[str, str]
    inverse_label: Mapping[str, str]
    inverse_family: str | None
    symmetric: bool
    group: str
    path_cost: float
    description: Mapping[str, str]
    share_allowed: bool = False
    causal: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": dict(self.label),
            "inverse_label": dict(self.inverse_label),
            "inverse_family": self.inverse_family,
            "symmetric": self.symmetric,
            "group": self.group,
            "path_cost": self.path_cost,
            "share_allowed": self.share_allowed,
            "causal": False,
            "description": dict(self.description),
        }


@dataclass(frozen=True)
class Mapping_:
    property: str
    family: str
    direction: Literal["forward", "reverse"] = "forward"
    subtype: str | None = None
    source: str | None = None
    share_qualifier: str | None = None
    share_scale: str | None = None
    subject_kinds: tuple[str, ...] = ()
    object_kinds: tuple[str, ...] = ()
    note: str | None = None
    #: File the mapping was read from (``relations_v4.yaml`` or ``relations_v4.d/<agent>.yaml``).
    origin: str = "relations_v4.yaml"
    agent: str = "V4CORE"


@dataclass(frozen=True)
class EdgeFamily:
    """How one stored edge reads when traversed from one of its ends."""

    family: str
    #: Label of the relation from the traversal start to the neighbour (fr/en).
    label: Mapping[str, str]
    #: True when the label is the family's inverse label (family has no paired inverse family).
    inverse: bool
    mapping: Mapping_
    causal: bool = False


@dataclass(frozen=True)
class FamilyRegistry:
    families: Mapping[str, Family]
    mappings: Mapping[str, Mapping_]
    never_map: frozenset[str]
    causal_name_markers: tuple[str, ...]
    files: tuple[str, ...] = field(default=())

    # ---------------------------------------------------------------------------------------- lookups
    def family(self, family_id: str) -> Family:
        try:
            return self.families[family_id]
        except KeyError as e:
            raise FamilyError(f"unknown relation family {family_id!r}") from e

    def mapping(self, pid: str) -> Mapping_ | None:
        return self.mappings.get(pid)

    def edge_family(self, pid: str, direction: Direction) -> EdgeFamily | None:
        """Family of the edge ``subject -pid-> object`` read from the subject (``out``) or from the object
        (``in``). ``None`` when the property is not mapped (it is then not part of any family)."""
        m = self.mappings.get(pid)
        if m is None:
            return None
        fam = self.families[m.family]
        # A ``reverse`` mapping means "object <family> subject": flip the reading direction.
        forward = (direction == "out") == (m.direction == "forward")
        if forward or fam.symmetric:
            return EdgeFamily(fam.id, dict(fam.label), False, m)
        if fam.inverse_family:
            inv = self.families[fam.inverse_family]
            return EdgeFamily(inv.id, dict(inv.label), False, m)
        return EdgeFamily(fam.id, dict(fam.inverse_label), True, m)

    def properties_for(self, family_ids: Iterable[str]) -> set[tuple[str, Direction]]:
        """(pid, traversal direction) pairs whose reading from the traversal start is one of ``family_ids``.
        A family id whose reading is the inverse of a non-paired family (``made_of`` read from the material)
        is matched by the family id too: the family is what is allowed, in either reading."""
        wanted = set(family_ids)
        for f in wanted:
            self.family(f)
        out: set[tuple[str, Direction]] = set()
        for pid in self.mappings:
            for d in ("out", "in"):
                ef = self.edge_family(pid, d)
                if ef is not None and ef.family in wanted:
                    out.add((pid, d))
        return out

    def pids_for(self, family_ids: Iterable[str]) -> set[str]:
        return {p for p, _ in self.properties_for(family_ids)}

    def summary(self) -> dict[str, Any]:
        counts: dict[str, int] = {f: 0 for f in self.families}
        for m in self.mappings.values():
            counts[m.family] += 1
        return {
            "families": len(self.families),
            "mappings": len(self.mappings),
            "per_family": counts,
            "files": list(self.files),
        }


# ---------------------------------------------------------------------------------------------- share
WIKIDATA_PERCENT_UNIT = "http://www.wikidata.org/entity/Q11229"
_SCALE = {"fraction": Decimal(1), "percent": Decimal(100), "per_mille": Decimal(1000), "ppm": Decimal(1_000_000)}


def share_of(mapping: Mapping_, qualifiers: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The share a statement's qualifiers give for a mapping that declares one; ``None`` = Missing (never 0).

    Returns ``{"value": <decimal string as given>, "fraction": <decimal string or None>, "unit": ..., "qualifier":
    ...}``. ``fraction`` is only computed for a dimensionless scale (fraction/percent/per_mille/ppm); with
    ``source_unit`` it stays ``None`` and the value is quoted as the source gives it.
    """
    if mapping.share_qualifier is None or not qualifiers:
        return None
    raw = qualifiers.get(mapping.share_qualifier)
    if isinstance(raw, list):
        vals = [v for v in raw if v is not None]
        if len(vals) != 1:  # several shares (or none): ambiguous, report Missing rather than pick one
            return None
        raw = vals[0]
    unit = None
    if isinstance(raw, Mapping):
        unit = raw.get("unit")
        raw = raw.get("amount", raw.get("value"))
    if raw is None or isinstance(raw, bool):
        return None
    text = str(raw).strip()
    try:
        dec = Decimal(text)
    except InvalidOperation:
        return None
    if not dec.is_finite():
        return None
    frac = None
    scale = mapping.share_scale or "source_unit"
    dimensionless = unit in (None, "", "1") or (scale == "percent" and unit == WIKIDATA_PERCENT_UNIT)
    if scale in _SCALE and dimensionless:
        frac = str((dec / _SCALE[scale]).normalize())
    return {
        "value": text.lstrip("+"),
        "fraction": frac,
        "unit": unit,
        "scale": scale,
        "qualifier": mapping.share_qualifier,
    }


# ---------------------------------------------------------------------------------------------- measured_by
#: KG kinds of the ``atlas:<source>.<dataset>`` table entities the V4 loaders create (V4ECON ``data_series``,
#: V4MIND ``atlas_table``, V4ECO ``atlas_dataset``). Catalog concepts are never KG entities of these kinds.
ATLAS_TABLE_KINDS = frozenset({"atlas_table", "data_series", "atlas_dataset"})
#: ``<source>.<dataset>`` atlas table key (same alphabet as ``reva.atlas.catalog.SERIES_KEY_RE``).
ATLAS_TABLE_RE = re.compile(r"^([a-z0-9][a-z0-9_-]{0,23})\.([a-z0-9][a-z0-9_-]{0,38})$")
_CLASSIF_RE = re.compile(r"^[A-Z0-9_]{1,60}$")
_GEO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
_IND_RE = re.compile(r"^[^\s][^\n]{0,199}$")


def measured_by_target(other: str, qualifiers: Any, kind: str | None = None) -> dict[str, Any] | None:
    """What a ``measured_by`` edge points at in the atlas, for both conventions the V4 loaders use.

    * ``kind: "concept"`` — V4CORE's :data:`ATLAS_MEASURED_BY`: object ``atlas:<catalog concept id>`` (or qualifier
      ``concept``), geography in qualifier ``iso3``; read with ``/v1/atlas/series``.
    * ``kind: "table"`` — V4ECON / V4MIND / V4ECO: object ``atlas:<source>.<dataset>`` (a table entity of kind
      ``data_series`` / ``atlas_table`` / ``atlas_dataset``), with the qualifiers that select the subject's cells:
      ``table`` or ``atlas_table`` (table key), ``provider_geo`` (the provider's region code: regions are never
      countries), ``indicators`` (exact indicator ids), ``classif1`` (ILOSTAT classification folded into the
      indicator as ``classif1=<code>``); read with ``/v1/atlas/tables/{source}/{dataset}/cells``.

    Only identifiers the statement carries are used (no name matching). ``None`` when nothing usable is given.
    """
    q: Mapping[str, Any] = qualifiers if isinstance(qualifiers, Mapping) else {}
    oid = other.split(":", 1)[1] if other.startswith("atlas:") else None
    concept = q.get("concept")
    table = q.get("table") or q.get("atlas_table")
    iso3 = q.get("iso3")
    iso3_s = str(iso3)[:3].upper() if isinstance(iso3, str) and iso3 else None
    if not concept and not table and oid and kind in ATLAS_TABLE_KINDS:
        table = oid
    if not concept and isinstance(table, str) and (m := ATLAS_TABLE_RE.match(table)):
        geo = q.get("provider_geo")
        inds = q.get("indicators")
        ind_list = [str(i) for i in inds if isinstance(i, str) and _IND_RE.match(i)] if isinstance(inds, list) else []
        classif = q.get("classif1")
        contains = f"classif1={classif}" if isinstance(classif, str) and _CLASSIF_RE.match(classif) else None
        return {
            "kind": "table",
            "concept": None,
            "table": table,
            "source": m.group(1),
            "dataset": m.group(2),
            "provider_geo": str(geo) if isinstance(geo, str) and _GEO_RE.match(geo) else None,
            "iso3": iso3_s,
            "indicators": ind_list[:50],
            "indicator_contains": contains,
        }
    concept = concept or oid
    if not concept:
        return None
    return {
        "kind": "concept",
        "concept": str(concept)[:200],
        "table": str(table)[:100] if table else None,
        "source": None,
        "dataset": None,
        "provider_geo": None,
        "iso3": iso3_s,
        "indicators": [],
        "indicator_contains": None,
    }


# ---------------------------------------------------------------------------------------------- loading
def _load_yaml(path: Path) -> Any:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


@lru_cache(maxsize=1)
def _schema() -> dict[str, Any]:
    doc: dict[str, Any] = json.loads(RELATIONS_SCHEMA.read_text(encoding="utf-8"))
    return doc


def _schema_problems(doc: Any, part: str, where: str) -> list[str]:
    schema = _schema()
    sub = {"$schema": schema["$schema"], "$defs": schema["$defs"], "$ref": f"#/$defs/{part}"}
    v = jsonschema.Draft202012Validator(sub)
    return [
        f"{where}: schema: {'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}"
        for e in sorted(v.iter_errors(doc), key=lambda e: list(e.absolute_path))
    ]


def requested_properties(directory: Path = PROPERTY_REQUESTS_DIR) -> set[str]:
    """Wikidata properties requested in ``registry/kg/property_requests/*.yaml`` (not yet allow-listed)."""
    out: set[str] = set()
    for f in sorted(directory.glob("*.yaml")):
        doc = _load_yaml(f) or []
        items = doc if isinstance(doc, list) else (doc.get("properties") or doc.get("requests") or [])
        out |= {str(i["property"]) for i in items if isinstance(i, Mapping) and i.get("property")}
    return out


def kg_allowlist() -> set[str]:
    """The KG property allow-list (Wikidata ``properties.yaml`` + namespaced dataset predicates)."""
    from reva.links.taxonomy import kg_allowlist as _allow

    return _allow()


def _mapping(d: Mapping[str, Any], origin: str, agent: str) -> Mapping_:
    share = d.get("share") or {}
    return Mapping_(
        property=str(d["property"]),
        family=str(d["family"]),
        direction=d.get("direction", "forward"),
        subtype=d.get("subtype"),
        source=d.get("source"),
        share_qualifier=share.get("qualifier"),
        share_scale=share.get("scale"),
        subject_kinds=tuple(d.get("subject_kinds") or ()),
        object_kinds=tuple(d.get("object_kinds") or ()),
        note=d.get("note"),
        origin=origin,
        agent=agent,
    )


def _read_all(
    core_path: Path, dropin_dir: Path
) -> tuple[dict[str, Any] | None, list[tuple[str, dict[str, Any]]], list[str]]:
    problems: list[str] = []
    core = _load_yaml(core_path)
    problems += _schema_problems(core, "core", core_path.name)
    dropins: list[tuple[str, dict[str, Any]]] = []
    if dropin_dir.is_dir():
        for f in sorted(dropin_dir.iterdir()):
            if f.name.startswith(".") or f.suffix not in (".yaml", ".yml"):
                if f.is_file() and not f.name.startswith("."):
                    problems.append(f"relations_v4.d/{f.name}: only <agent>.yaml files are read")
                continue
            doc = _load_yaml(f)
            where = f"relations_v4.d/{f.name}"
            ps = _schema_problems(doc, "dropin", where)
            if not ps and f.stem != str(doc["agent"]).lower():
                ps.append(f"{where}: file name must be <agent lowercase>.yaml (agent {doc['agent']})")
            problems += ps
            if not ps:
                dropins.append((where, doc))
    return core, dropins, problems


def validate(
    core_path: Path = RELATIONS_FILE,
    dropin_dir: Path = RELATIONS_DIR,
    *,
    allowlist: set[str] | None = None,
    requested: set[str] | None = None,
) -> list[str]:
    """Schema + semantic checks of the core file and every drop-in. Empty list = valid.

    ``allowlist``/``requested`` default to the repo's KG allow-list and property requests; pass sets in tests.
    """
    core, dropins, problems = _read_all(core_path, dropin_dir)
    if problems or core is None:
        return problems
    if allowlist is None:
        allowlist = kg_allowlist()
    if requested is None:
        requested = requested_properties()
    fams = {f["id"]: f for f in core["families"]}
    ids = [f["id"] for f in core["families"]]
    for dup in sorted({i for i in ids if ids.count(i) > 1}):
        problems.append(f"duplicate family id {dup}")
    for f in core["families"]:
        if f.get("causal") is not False:  # the schema already enforces it; belt and braces
            problems.append(f"{f['id']}: a relation family is never causal")
        inv = f["inverse_family"]
        if inv is not None:
            if inv not in fams:
                problems.append(f"{f['id']}: inverse_family {inv} is not a family")
            elif fams[inv]["inverse_family"] != f["id"]:
                problems.append(f"{f['id']}: inverse_family {inv} must point back to {f['id']}")
            elif fams[inv]["label"] != f["inverse_label"]:
                problems.append(f"{f['id']}: inverse_label must equal the label of {inv}")
        if f["symmetric"] and (inv is not None or f["label"] != f["inverse_label"]):
            problems.append(f"{f['id']}: a symmetric family has no inverse family and equal labels")
        for lang in ("en", "fr"):
            text = (f["label"][lang] + " " + f["inverse_label"][lang]).lower()
            if re.search(r"\b(cause|causes|caused|because|parce que|à cause|cause de)\b", text):
                problems.append(f"{f['id']}: causal wording in a family label")
    never = set(core["never_map"]["properties"]) | CAUSAL_PIDS
    markers = tuple(core["never_map"]["causal_name_markers"])
    seen: dict[str, str] = {}
    all_maps: list[tuple[str, Mapping[str, Any]]] = [("relations_v4.yaml", m) for m in core["mappings"]]
    for where, doc in dropins:
        all_maps += [(where, m) for m in doc["mappings"]]
    for where, m in all_maps:
        pid = str(m["property"])
        tag = f"{where}: {pid}"
        if m["family"] not in fams:
            problems.append(f"{tag}: unknown family {m['family']}")
            continue
        if pid in seen:
            problems.append(f"{tag}: already mapped in {seen[pid]} (one family per property)")
        seen[pid] = where
        if pid in never:
            problems.append(f"{tag}: source-asserted causal property, never a family (use the causal loader)")
        if ":" in pid:
            name = pid.split(":", 1)[1]
            for mk in markers:
                if mk in name.split("_") or mk in name:
                    problems.append(
                        f"{tag}: name contains causal marker {mk!r}; causal claims go through the causal loader"
                    )
                    break
        elif WIKIDATA_PID_RE.match(pid):
            if pid not in allowlist and pid not in requested:
                problems.append(
                    f"{tag}: Wikidata property neither in registry/kg/properties.yaml nor requested in "
                    "registry/kg/property_requests/"
                )
        else:  # pragma: no cover - schema pattern already refuses it
            problems.append(f"{tag}: invalid property id")
        if m.get("share") and not fams[m["family"]].get("share_allowed"):
            problems.append(f"{tag}: family {m['family']} does not carry a share")
    return problems


@lru_cache(maxsize=4)
def load_registry(core_path: Path = RELATIONS_FILE, dropin_dir: Path = RELATIONS_DIR) -> FamilyRegistry:
    """Load and structurally validate families + all mappings. Allow-list membership is checked by
    :func:`validate` (tests / CLI), not here, so that the API keeps serving when an agent's predicate is not yet
    registered (its statements just do not exist yet)."""
    core, dropins, problems = _read_all(core_path, dropin_dir)
    if not problems:
        problems = [
            p
            for p in validate(core_path, dropin_dir, allowlist=set(), requested=set())
            if "neither in registry/kg/properties.yaml" not in p
        ]
    if problems or core is None:
        raise FamilyError("; ".join(problems[:10]))
    fams = {
        f["id"]: Family(
            id=f["id"],
            label=dict(f["label"]),
            inverse_label=dict(f["inverse_label"]),
            inverse_family=f["inverse_family"],
            symmetric=bool(f["symmetric"]),
            group=f["group"],
            path_cost=float(f["path_cost"]),
            description=dict(f["description"]),
            share_allowed=bool(f.get("share_allowed", False)),
        )
        for f in core["families"]
    }
    maps: dict[str, Mapping_] = {}
    for m in core["mappings"]:
        maps[str(m["property"])] = _mapping(m, "relations_v4.yaml", "V4CORE")
    for where, doc in dropins:
        for m in doc["mappings"]:
            maps[str(m["property"])] = _mapping(m, where, str(doc["agent"]))
    files = ("relations_v4.yaml", *(w for w, _ in dropins))
    return FamilyRegistry(
        families=fams,
        mappings=maps,
        never_map=frozenset(core["never_map"]["properties"]) | CAUSAL_PIDS,
        causal_name_markers=tuple(core["never_map"]["causal_name_markers"]),
        files=files,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] != "check":
        print("usage: python -m reva.kg.families check", file=sys.stderr)
        return 2
    problems = validate()
    for p in problems:
        print(f"PROBLEM {p}")
    if problems:
        return 1
    reg = load_registry()
    s = reg.summary()
    print(f"ok: {s['families']} families, {s['mappings']} mappings from {len(s['files'])} file(s)")
    for fid, n in s["per_family"].items():
        print(f"  {fid:16s} {n}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
