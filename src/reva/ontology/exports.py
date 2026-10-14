"""Standards-aligned *derived* exports: PROV-O lineage, OWL-Time extents, GeoSPARQL WKT.

These exports are derived views for interoperability. They are not
authoritative: the authoritative records are the immutable PostgreSQL rows
(``meta.activity``, ``meta.activity_input``, ``meta.activity_output``,
``meta.artifact``) and the content-addressed artifact bytes. Nothing here is
read back into REVA, and no OWL reasoning runs in any hot path.

Vocabulary versions are pinned to released W3C/OGC recommendations, never to
a moving draft URL (architecture §5).
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal
from urllib.parse import quote
from uuid import UUID

from reva.kernel.errors import IncompatibleSemantics, ValidationProblem
from reva.kernel.ids import logical_hash
from reva.kernel.time import Calendar, Precision, TimeExtent, UncertainBound

EXPORT_SCHEMA_VERSION = "reva-prov-jsonld/1"


@dataclass(frozen=True)
class Vocabulary:
    prefix: str
    namespace: str
    release: str
    version_iri: str


VOCABULARIES: tuple[Vocabulary, ...] = (
    Vocabulary("prov", "http://www.w3.org/ns/prov#", "PROV-O, W3C Recommendation 30 April 2013",
               "http://www.w3.org/TR/2013/REC-prov-o-20130430/"),
    Vocabulary("time", "http://www.w3.org/2006/time#", "Time Ontology in OWL, W3C Recommendation 19 October 2017",
               "http://www.w3.org/TR/2017/REC-owl-time-20171019/"),
    Vocabulary("geo", "http://www.opengis.net/ont/geosparql#", "OGC GeoSPARQL 1.0 (OGC 11-052r4)",
               "http://www.opengis.net/doc/IS/geosparql/1.0"),
    Vocabulary("xsd", "http://www.w3.org/2001/XMLSchema#", "XML Schema Datatypes 1.1",
               "http://www.w3.org/TR/2012/REC-xmlschema11-2-20120405/"),
    Vocabulary("rdfs", "http://www.w3.org/2000/01/rdf-schema#", "RDF Schema 1.1",
               "http://www.w3.org/TR/2014/REC-rdf-schema-20140225/"),
)
REVA_NS = "https://reva.invalid/ns/reva#"
JSONLD_CONTEXT: dict[str, str] = {**{v.prefix: v.namespace for v in VOCABULARIES}, "reva": REVA_NS}
DERIVED_NOTICE = (
    "Derived, non-authoritative export. Authoritative provenance is the immutable REVA database records "
    "(meta.activity, meta.activity_input, meta.activity_output, meta.artifact) and content-addressed bytes."
)

CRS84_IRI = "http://www.opengis.net/def/crs/OGC/1.3/CRS84"
EPSG4326_IRI = "http://www.opengis.net/def/crs/EPSG/0/4326"
GREGORIAN_TRS_IRI = "http://www.opengis.net/def/uom/ISO-8601/0/Gregorian"
JULIAN_TRS_IRI = f"{REVA_NS}trs/julian"


def vocabulary_manifest() -> list[dict[str, str]]:
    return [{"prefix": v.prefix, "namespace": v.namespace, "release": v.release, "version_iri": v.version_iri}
            for v in VOCABULARIES]


# --------------------------------------------------------------------------- IRIs and literals


def activity_iri(activity_id: UUID | str) -> str:
    return f"urn:reva:activity:{UUID(str(activity_id))}"


def artifact_iri(sha256: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise ValidationProblem(f"not a sha256 hex digest: {sha256!r}")
    return f"urn:reva:artifact:sha256:{sha256}"


def agent_iri(actor: str) -> str:
    if not actor:
        raise ValidationProblem("activity actor is required")
    return f"urn:reva:agent:{quote(actor, safe='')}"


def _datetime_literal(v: datetime | str, what: str) -> dict[str, str]:
    dt = datetime.fromisoformat(v) if isinstance(v, str) else v
    if not isinstance(dt, datetime) or dt.tzinfo is None:
        raise ValidationProblem(f"{what} must be a timezone-aware timestamp")
    return {"@value": dt.isoformat(), "@type": "xsd:dateTime"}


# --------------------------------------------------------------------------- PROV-O


def prov_jsonld(
    activities: Sequence[Mapping[str, Any]],
    artifacts: Sequence[Mapping[str, Any]],
    usages: Sequence[Mapping[str, Any]] = (),
    generations: Sequence[Mapping[str, Any]] = (),
    *,
    focus_artifact: str | None = None,
) -> dict[str, Any]:
    """PROV-O compatible JSON-LD of an activity/artifact lineage from plain dicts.

    * activity: ``id, kind, origin, code_digest, environment_digest, actor,
      started_at, ended_at`` and ``parameters`` (exported as a digest only);
    * artifact: ``sha256, media_type, byte_size`` (+ optional ``schema_version``,
      ``privacy_class``);
    * usage: ``activity_id, artifact_sha256, ordinal, selector``;
    * generation: ``activity_id, artifact_sha256``.

    Every reference must resolve. ``prov:wasDerivedFrom`` is deliberately not
    emitted: using and generating does not by itself establish derivation.
    """
    acts: dict[str, Mapping[str, Any]] = {}
    for a in activities:
        iri = activity_iri(a["id"])
        if iri in acts:
            raise ValidationProblem(f"duplicate activity {a['id']}")
        acts[iri] = a
    arts: dict[str, Mapping[str, Any]] = {}
    for x in artifacts:
        iri = artifact_iri(x["sha256"])
        arts[iri] = x
    used: dict[str, list[dict[str, Any]]] = {k: [] for k in acts}
    generated_by: dict[str, list[str]] = {}
    for u in usages:
        a_iri, e_iri = activity_iri(u["activity_id"]), artifact_iri(u["artifact_sha256"])
        if a_iri not in acts or e_iri not in arts:
            raise ValidationProblem(f"usage references unknown activity/artifact: {u['activity_id']} "
                                    f"{u['artifact_sha256']}")
        used[a_iri].append({
            "@type": "prov:Usage",
            "prov:entity": {"@id": e_iri},
            "reva:ordinal": int(u.get("ordinal", 0)),
            "reva:selector": {"@value": dict(u.get("selector") or {}), "@type": "@json"},
        })
    for g in generations:
        a_iri, e_iri = activity_iri(g["activity_id"]), artifact_iri(g["artifact_sha256"])
        if a_iri not in acts or e_iri not in arts:
            raise ValidationProblem(f"generation references unknown activity/artifact: {g['activity_id']} "
                                    f"{g['artifact_sha256']}")
        generated_by.setdefault(e_iri, []).append(a_iri)
    if focus_artifact is not None and artifact_iri(focus_artifact) not in arts:
        raise ValidationProblem(f"focus artifact {focus_artifact} not in lineage")

    graph: list[dict[str, Any]] = []
    agents: set[str] = set()
    for iri in sorted(acts):
        a = acts[iri]
        ag = agent_iri(str(a["actor"]))
        agents.add(ag)
        params_digest = a.get("parameters_sha256") or logical_hash(dict(a.get("parameters") or {}))
        node: dict[str, Any] = {
            "@id": iri,
            "@type": "prov:Activity",
            "prov:startedAtTime": _datetime_literal(a["started_at"], "started_at"),
            "prov:endedAtTime": _datetime_literal(a["ended_at"], "ended_at"),
            "prov:wasAssociatedWith": {"@id": ag},
            "reva:kind": str(a["kind"]),
            "reva:origin": str(a["origin"]),
            "reva:codeDigest": str(a["code_digest"]),
            "reva:environmentDigest": str(a["environment_digest"]),
            "reva:parametersDigest": params_digest,
            "rdfs:comment": "Computational activity (not a real-world event or process).",
        }
        uses = sorted(used[iri], key=lambda x: x["reva:ordinal"])
        if uses:
            node["prov:used"] = [{"@id": x["prov:entity"]["@id"]} for x in uses]
            node["prov:qualifiedUsage"] = uses
        graph.append(node)
    for iri in sorted(arts):
        x = arts[iri]
        node = {
            "@id": iri,
            "@type": "prov:Entity",
            "reva:sha256": x["sha256"],
            "reva:mediaType": str(x["media_type"]),
            "reva:byteSize": int(x["byte_size"]),
        }
        for k_in, k_out in (("schema_version", "reva:schemaVersion"), ("privacy_class", "reva:privacyClass")):
            if x.get(k_in) is not None:
                node[k_out] = str(x[k_in])
        if iri in generated_by:
            node["prov:wasGeneratedBy"] = [{"@id": a} for a in sorted(generated_by[iri])]
        graph.append(node)
    for ag in sorted(agents):
        graph.append({"@id": ag, "@type": ["prov:Agent", "prov:SoftwareAgent"]})
    _check_acyclic(acts, used, generated_by)
    doc: dict[str, Any] = {
        "@context": dict(JSONLD_CONTEXT),
        "@id": f"urn:reva:export:{logical_hash({'g': graph})}",
        "@type": "prov:Bundle",
        "reva:exportSchema": EXPORT_SCHEMA_VERSION,
        "reva:authoritative": False,
        "rdfs:comment": DERIVED_NOTICE,
        "reva:vocabularies": {"@value": vocabulary_manifest(), "@type": "@json"},
        "@graph": graph,
    }
    if focus_artifact is not None:
        doc["reva:focus"] = {"@id": artifact_iri(focus_artifact)}
    return doc


def _check_acyclic(acts: Mapping[str, Any], used: Mapping[str, list[dict[str, Any]]],
                   generated_by: Mapping[str, list[str]]) -> None:
    """Provenance must be a DAG: activity -> used entity -> generating activity."""
    edges = {a: {g for u in used[a] for g in generated_by.get(u["prov:entity"]["@id"], [])} for a in acts}
    state: dict[str, int] = {}

    def visit(n: str) -> None:
        stack = [(n, iter(sorted(edges[n])))]
        state[n] = 1
        while stack:
            node, it = stack[-1]
            nxt = next(it, None)
            if nxt is None:
                state[node] = 2
                stack.pop()
            elif state.get(nxt) == 1:
                raise ValidationProblem(f"provenance cycle through {nxt}")
            elif nxt not in state:
                state[nxt] = 1
                stack.append((nxt, iter(sorted(edges[nxt]))))

    for a in acts:
        if a not in state:
            visit(a)


@dataclass
class LineageData:
    activities: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    usages: list[dict[str, Any]] = field(default_factory=list)
    generations: list[dict[str, Any]] = field(default_factory=list)
    truncated: bool = False


def load_lineage(conn: Any, artifact_sha256: str, *, max_activities: int = 5000) -> LineageData:
    """Walk the upstream lineage of an artifact from the database (read-only).

    ``conn`` is a SQLAlchemy ``Connection``. Upstream means: activities that
    generated the artifact, the artifacts they used, and so on.
    """
    from sqlalchemy import bindparam, text

    artifact_iri(artifact_sha256)
    data = LineageData()
    seen_art: set[str] = set()
    seen_act: set[str] = set()
    frontier = {artifact_sha256}
    while frontier:
        seen_art |= frontier
        rows = conn.execute(
            text("SELECT activity_id, artifact_sha256 FROM meta.activity_output WHERE artifact_sha256 IN :s")
            .bindparams(bindparam("s", expanding=True)), {"s": sorted(frontier)}
        ).mappings().all()
        new_acts = {str(r["activity_id"]) for r in rows} - seen_act
        if len(seen_act) + len(new_acts) > max_activities:
            data.truncated = True
            break
        seen_act |= new_acts
        if not new_acts:
            break
        ins = conn.execute(
            text("SELECT activity_id, ordinal, artifact_sha256, selector FROM meta.activity_input "
                 "WHERE activity_id IN :a").bindparams(bindparam("a", expanding=True)), {"a": sorted(new_acts)}
        ).mappings().all()
        frontier = {r["artifact_sha256"] for r in ins} - seen_art
    if seen_act:
        acts = conn.execute(
            text("SELECT id, kind, origin, code_digest, parameters, environment_digest, actor, started_at, ended_at "
                 "FROM meta.activity WHERE id IN :a").bindparams(bindparam("a", expanding=True)),
            {"a": sorted(seen_act)},
        ).mappings().all()
        data.activities = [dict(r) for r in acts]
        ids = sorted(seen_act)
        data.usages = [dict(r) for r in conn.execute(
            text("SELECT activity_id, ordinal, artifact_sha256, selector FROM meta.activity_input "
                 "WHERE activity_id IN :a").bindparams(bindparam("a", expanding=True)), {"a": ids}).mappings()]
        data.generations = [dict(r) for r in conn.execute(
            text("SELECT activity_id, artifact_sha256 FROM meta.activity_output "
                 "WHERE activity_id IN :a").bindparams(bindparam("a", expanding=True)), {"a": ids}).mappings()]
    shas = {artifact_sha256} | {u["artifact_sha256"] for u in data.usages} | {
        g["artifact_sha256"] for g in data.generations}
    data.artifacts = [dict(r) for r in conn.execute(
        text("SELECT sha256, media_type, byte_size, schema_version, privacy_class FROM meta.artifact "
             "WHERE sha256 IN :s").bindparams(bindparam("s", expanding=True)), {"s": sorted(shas)}).mappings()]
    if len(data.artifacts) != len(shas):
        raise ValidationProblem("lineage references artifacts without meta.artifact rows")
    return data


def export_artifact_lineage(conn: Any, artifact_sha256: str, *, max_activities: int = 5000) -> dict[str, Any]:
    data = load_lineage(conn, artifact_sha256, max_activities=max_activities)
    doc = prov_jsonld(data.activities, data.artifacts, data.usages, data.generations, focus_artifact=artifact_sha256)
    doc["reva:truncated"] = data.truncated
    return doc


# --------------------------------------------------------------------------- OWL-Time


_UNIT_TYPE = {Precision.year: "time:unitYear", Precision.month: "time:unitMonth",
              Precision.day: "time:unitDay", Precision.instant: "time:unitDay"}


def _date_literal(d: date, precision: Precision) -> tuple[str, dict[str, str]]:
    if precision is Precision.year:
        return "time:inXSDGYear", {"@value": f"{d.year:04d}", "@type": "xsd:gYear"}
    if precision is Precision.month:
        return "time:inXSDGYearMonth", {"@value": f"{d.year:04d}-{d.month:02d}", "@type": "xsd:gYearMonth"}
    return "time:inXSDDate", {"@value": d.isoformat(), "@type": "xsd:date"}


def _instant(d: date | None, precision: Precision, calendar: Calendar,
             uncertainty: UncertainBound | None) -> dict[str, Any]:
    node: dict[str, Any] = {"@type": "time:Instant"}
    if d is not None:
        if calendar in (Calendar.gregorian, Calendar.proleptic_gregorian):
            # XSD date types are (proleptic) Gregorian.
            prop, lit = _date_literal(d, precision)
            node[prop] = lit
        else:
            desc: dict[str, Any] = {
                "@type": "time:GeneralDateTimeDescription",
                "time:hasTRS": {"@id": JULIAN_TRS_IRI},
                "time:unitType": {"@id": _UNIT_TYPE[precision]},
                "time:year": {"@value": f"{d.year:04d}", "@type": "time:generalYear"},
            }
            if precision in (Precision.month, Precision.day, Precision.instant):
                desc["time:month"] = {"@value": f"--{d.month:02d}", "@type": "time:generalMonth"}
            if precision in (Precision.day, Precision.instant):
                desc["time:day"] = {"@value": f"---{d.day:02d}", "@type": "time:generalDay"}
            node["time:inDateTime"] = desc
    if uncertainty is not None:
        node["reva:earliestPossible"] = {"@value": uncertainty.earliest.isoformat(), "@type": "xsd:date"}
        node["reva:latestPossible"] = {"@value": uncertainty.latest.isoformat(), "@type": "xsd:date"}
    if d is None and uncertainty is None:
        node["reva:unknown"] = True
    return node


def time_extent_to_owl_time(te: TimeExtent, iri: str | None = None) -> dict[str, Any]:
    """OWL-Time-like JSON-LD node for a :class:`TimeExtent`.

    Intervals are REVA half-open ``[start, end)``; OWL-Time has no half-open
    notion, so ``reva:endExclusive`` states it. A 1 January point stays a
    ``time:Instant`` (never widened to the year).
    """
    if te.kind == "instant":
        node = _instant(te.start, te.precision, te.calendar, te.start_uncertainty)
    else:
        node = {"@type": "time:ProperInterval",
                "time:hasBeginning": _instant(te.start, te.precision, te.calendar, te.start_uncertainty)}
        if te.end is not None or te.end_uncertainty is not None:
            node["time:hasEnd"] = _instant(te.end, te.precision, te.calendar, te.end_uncertainty)
            node["reva:endExclusive"] = True
        else:
            node["reva:openEnded"] = True
    if iri is not None:
        node = {"@id": iri, **node}
    node["reva:calendar"] = te.calendar.value
    node["reva:precision"] = te.precision.value
    if te.label:
        node["rdfs:label"] = te.label
    return node


# --------------------------------------------------------------------------- GeoSPARQL

_WKT_HEAD = re.compile(r"^\s*(POINT|MULTIPOINT|LINESTRING|MULTILINESTRING|POLYGON|MULTIPOLYGON|GEOMETRYCOLLECTION|"
                       r"TRIANGLE|TIN|POLYHEDRALSURFACE)(\s+(Z|M|ZM))?\s*(EMPTY\s*$|\()", re.IGNORECASE)
_WKT_WORDS = re.compile(r"\b(POINT|MULTIPOINT|LINESTRING|MULTILINESTRING|POLYGON|MULTIPOLYGON|GEOMETRYCOLLECTION|"
                        r"TRIANGLE|TIN|POLYHEDRALSURFACE|ZM|Z|M|EMPTY)\b", re.IGNORECASE)
_NUM = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")


def _check_wkt(wkt: str) -> None:
    if not _WKT_HEAD.match(wkt):
        raise ValidationProblem("not a WKT geometry")
    depth = 0
    for ch in wkt:
        depth += (ch == "(") - (ch == ")")
        if depth < 0:
            raise ValidationProblem("unbalanced parentheses in WKT")
    if depth != 0:
        raise ValidationProblem("unbalanced parentheses in WKT")
    if re.search(r"\b(nan|inf|infinity)\b", wkt, re.IGNORECASE):
        raise ValidationProblem("non-finite coordinates in WKT")
    body = wkt[wkt.find("("):] if "(" in wkt else ""
    leftovers = _WKT_WORDS.sub("", _NUM.sub("", body))
    if re.search(r"[^\s(),]", leftovers):
        raise ValidationProblem("invalid coordinate tokens in WKT")
    if not all(math.isfinite(float(n)) for n in _NUM.findall(body)):
        raise ValidationProblem("non-finite coordinates in WKT")


def wkt_literal(wkt: str, *, crs: Literal["OGC:CRS84", "EPSG:4326"] = "OGC:CRS84",
                axis_order: Literal["lon_lat", "lat_lon"] = "lon_lat") -> dict[str, str]:
    """GeoSPARQL ``geo:wktLiteral`` with an explicit CRS IRI.

    GeoSPARQL 1.0 WKT literals use the axis order of the CRS: CRS84 is
    longitude/latitude, EPSG:4326 is latitude/longitude. PostGIS SRID 4326
    geometries are stored longitude/latitude, so they are exported as CRS84.
    Exporting lon/lat coordinates under EPSG:4326 would silently swap axes and
    is refused.
    """
    _check_wkt(wkt)
    if crs == "OGC:CRS84":
        if axis_order != "lon_lat":
            raise IncompatibleSemantics("CRS84 WKT literals require longitude/latitude axis order")
        iri = CRS84_IRI
    else:
        if axis_order != "lat_lon":
            raise IncompatibleSemantics(
                "EPSG:4326 WKT literals require latitude/longitude axis order; export lon/lat data as OGC:CRS84")
        iri = EPSG4326_IRI
    return {"@value": f"<{iri}> {wkt.strip()}", "@type": "geo:wktLiteral"}


def geometry_node(geometry_iri: str, wkt: str, *, feature_iri: str | None = None,
                  authoritative: bool = False, **kw: Any) -> dict[str, Any]:
    """A ``geo:Geometry`` node; display geometries are flagged non-authoritative."""
    node: dict[str, Any] = {"@id": geometry_iri, "@type": "geo:Geometry", "geo:asWKT": wkt_literal(wkt, **kw),
                            "reva:authoritative": authoritative}
    if feature_iri is not None:
        return {"@id": feature_iri, "@type": "geo:Feature", "geo:hasGeometry": node}
    return node


def iter_iris(doc: Mapping[str, Any]) -> Iterable[str]:
    """All node ``@id`` values in an export's ``@graph`` (for tests and consumers)."""
    for n in doc.get("@graph", []):
        yield n["@id"]
