"""Shared plumbing of the V6INFRA transport & logistics connectors (agent V6INFRA, V6).

Used by :mod:`atlas_unlocode` (UN/LOCODE), :mod:`atlas_ports` (NGA World Port Index KG layer),
:mod:`atlas_portwatch` (IMF PortWatch) and :mod:`atlas_flights` (OurAirports KG layer).

ADM1 placement (``located_in`` a first-level subdivision), two labelled bases, never by name:

* ``published_code`` -- the provider publishes an ISO 3166-2 code for the facility (UN/LOCODE ``Subdivision``
  column, OurAirports ``iso_region``). The edge is drawn to ``iso3166_2:<code>`` only when that code names exactly
  one usable Natural Earth admin-1 polygon (the ADM1 entities V4CLIMATE loaded, same ids); otherwise the code is
  kept as a literal and counted (``code_not_an_ne_adm1``), never re-mapped.
* ``point_in_polygon`` -- REVA computation: the facility's published coordinates fall in exactly one usable Natural
  Earth 1:10m admin-1 polygon (shapely STRtree ``covered_by``), the same rule as V4CLIMATE's GHCN stations
  (:func:`atlas_ghcnm.locate`). Coastal facilities (ports) whose point lies just outside the generalised 1:10m
  coastline may use ``nearest_polygon``: the unique nearest usable polygon within ``coastal_deg`` degrees (planar), with the
  distance in the qualifiers. When the polygon's country differs from the country the provider publishes for the
  facility, no ADM1 edge is drawn (the disagreement is counted and qualified on the country statement).

Natural Earth admin-1 (public domain) is fetched through the run like any other raw file (archived; identical
bytes are stored once by the content-addressed store) from the URL V4CLIMATE uses.

``measured_by``: a facility whose values live in an atlas table points to the ``data_series`` entity
``atlas:<source>.<dataset>`` (the V4ECON convention); the values are read from the atlas, never from the KG.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from reva.adapters.connectors import atlas_ghcnm as gh
from reva.adapters.connectors import atlas_infra_common as ic

NE_URL = gh.URLS["ne_admin1"]
NE_HOST = gh.NE_HOST
NE_NAME = "ne_10m_admin_1_states_provinces.zip"
NE_RIGHTS = dict(gh.NE_RIGHTS)
NE_MEDIA = ("application/zip", "application/octet-stream")
ISO_3166_2_RE = gh.ISO_3166_2_RE
MEASURED_BY = "infra:measured_by"

#: namespaced predicates shared by the V6INFRA loaders: pid -> (value kind, reva predicate, label en, label fr)
SHARED_PREDICATES: dict[str, tuple[str, str, str, str]] = {
    MEASURED_BY: (
        "item",
        "infra_measured_by",
        "measured by (atlas data series)",
        "mesuré par (série de l'atlas)",
    ),
}


# --------------------------------------------------------------------------- coordinates
_DMS_RE = re.compile(r"^\s*(\d{1,3})°(\d{1,2})'(\d{1,2}(?:\.\d+)?)\"([NSEW])\s*$")
_LOCODE_COORD_RE = re.compile(r"^(\d{2})(\d{2})([NS]) (\d{3})(\d{2})([EW])$")


def _dec(x: Decimal, places: int = 6) -> str:
    q = x.quantize(Decimal(1).scaleb(-places))
    s = format(q.normalize(), "f")
    return "0" if s in ("-0", "") else s


def dms_to_decimal(text: str) -> str | None:
    """``30°20'00"N`` -> ``30.333333`` (degrees + minutes/60 + seconds/3600; S and W negative). None if malformed."""
    m = _DMS_RE.match(text or "")
    if not m:
        return None
    d, mi, s, h = Decimal(m.group(1)), Decimal(m.group(2)), Decimal(m.group(3)), m.group(4)
    if mi > 60 or s > 60:  # the WPI writes e.g. 16°60'00"S (= 17°00'00"S): 60 is arithmetically exact, kept
        return None
    v = d + mi / 60 + s / 3600
    if (h in "NS" and v > 90) or (h in "EW" and v > 180):
        return None
    return _dec(-v if h in "SW" else v)


def locode_coordinates(text: str) -> tuple[str, str] | None:
    """UN/LOCODE ``4230N 00131E`` (degrees and minutes) -> (lat, lon) decimal strings. None if empty/malformed."""
    m = _LOCODE_COORD_RE.match((text or "").strip())
    if not m:
        return None
    lat = Decimal(m.group(1)) + Decimal(m.group(2)) / 60
    lon = Decimal(m.group(4)) + Decimal(m.group(5)) / 60
    if int(m.group(2)) >= 60 or int(m.group(5)) >= 60 or lat > 90 or lon > 180:
        return None
    return _dec(-lat if m.group(3) == "S" else lat), _dec(-lon if m.group(6) == "W" else lon)


def decimal_text(text: str | None) -> str | None:
    """A provider decimal (``48.2833``) as a canonical decimal string; None when not a finite decimal."""
    if text is None:
        return None
    t = str(text).strip()
    if not re.match(r"^-?\d+(\.\d+)?([eE]-?\d+)?$", t):
        return None
    return _dec(Decimal(t))


def coordinate_value(lat: str, lon: str) -> dict[str, Any]:
    return {"type": "globe_coordinate", "lat": lat, "lon": lon, "globe": "earth"}


# --------------------------------------------------------------------------- Natural Earth admin-1
@dataclass
class Adm1Index:
    """Natural Earth admin-1 polygons with the usable ISO 3166-2 codes (unique, well-formed)."""

    recs: list[gh.Adm1]
    geoms: list[Any]
    usable: set[str]
    fetched: ic.Fetched
    report: dict[str, Any]
    _tree: Any = None
    by_code: dict[str, gh.Adm1] = field(default_factory=dict)

    @classmethod
    def from_fetched(cls, f: ic.Fetched, *, origin: str) -> Adm1Index:
        recs, geoms, report = gh.read_ne_admin1(f.body, origin=origin)
        usable = gh.usable_codes(recs)
        idx = cls(recs, geoms, usable, f, report)
        idx.by_code = {r.code: r for r in recs if r.code in usable}
        return idx

    def place(self, lat: str, lon: str) -> gh.Adm1 | None:
        """The unique usable subdivision whose polygon covers the point (None: offshore, overlap, unusable code)."""
        from shapely import STRtree  # type: ignore[import-untyped]
        from shapely.geometry import Point  # type: ignore[import-untyped]

        if self._tree is None:
            self._tree = STRtree(list(self.geoms))
        p = Point(float(lon), float(lat))
        hits = [int(i) for i in self._tree.query(p, predicate="covered_by")]
        if len(hits) == 1 and self.recs[hits[0]].code in self.usable:
            return self.recs[hits[0]]
        return None

    def nearest(self, lat: str, lon: str, max_deg: float) -> tuple[gh.Adm1, float] | None:
        """The unique usable polygon nearest to the point within ``max_deg`` degrees (planar distance in degrees)."""
        from shapely import STRtree
        from shapely.geometry import Point

        if self._tree is None:
            self._tree = STRtree(list(self.geoms))
        p = Point(float(lon), float(lat))
        idx, dist = self._tree.query_nearest(p, max_distance=max_deg, return_distance=True, all_matches=True)
        hits = sorted({int(i) for i in idx})
        if len(hits) == 1 and self.recs[hits[0]].code in self.usable:
            return self.recs[hits[0]], float(dist[0])
        return None

    def ref(self) -> dict[str, Any]:
        return kg_ref(self.fetched, file="ne_10m_admin_1_states_provinces.shp", licence=NE_RIGHTS["licence"])


async def fetch_ne_admin1(run: Any) -> Adm1Index:
    f = await ic.fetch_archived(run, NE_URL, name=NE_NAME, key="naturalearth:admin1_10m", media_types=NE_MEDIA,
                                max_bytes=30_000_000)  # fmt: skip
    return Adm1Index.from_fetched(f, origin=run.origin)


@dataclass
class Placement:
    """Result of placing one facility in an ADM1 (edge or the reason there is none)."""

    code: str | None
    basis: str | None
    reason: str
    quals: dict[str, Any] = field(default_factory=dict)


def place_facility(
    idx: Adm1Index | None,
    *,
    country_iso2: str | None,
    published_code: str | None = None,
    lat: str | None = None,
    lon: str | None = None,
    coastal_deg: float | None = None,
) -> Placement:
    """Published ISO 3166-2 code first (if it names a usable Natural Earth subdivision), else point in polygon.

    ``country_iso2`` is the country the provider publishes for the facility; a subdivision of another country is
    never linked (border generalisation, disputed areas, provider errors stay visible as counted reasons)."""
    if idx is None:
        return Placement(None, None, "no_admin1_index")
    if published_code:
        code = published_code.strip().upper()
        if ISO_3166_2_RE.match(code) and code in idx.usable:
            if country_iso2 and code[:2] != country_iso2:
                return Placement(None, None, "published_code_other_country")
            a = idx.by_code[code]
            return Placement(code, "published_code", "published_code",
                             {"basis": "published_code", "published_code": published_code,
                              "ne_adm1_code": a.adm1_code})  # fmt: skip
    if lat is None or lon is None:
        return Placement(None, None, "published_code_not_an_ne_adm1_and_no_coordinates" if published_code
                         else "no_code_no_coordinates")  # fmt: skip
    a = idx.place(lat, lon)
    basis, extra = "point_in_polygon", {}
    if a is None and coastal_deg:
        near = idx.nearest(lat, lon, coastal_deg)
        if near is None:
            return Placement(None, None, "outside_usable_polygons")
        a, dist = near
        basis = "nearest_polygon"
        extra = {"distance_deg": round(dist, 6), "max_distance_deg": coastal_deg,
                 "note": "point outside every polygon (coast generalised at 1:10m); the nearest polygon (unique "
                 f"nearest, planar distance) within {coastal_deg} degrees is used"}  # fmt: skip
    if a is None:
        return Placement(None, None, "outside_usable_polygons")
    if country_iso2 and a.code[:2] != country_iso2:
        return Placement(None, None, f"{basis}_other_country", {"coordinates_fall_in": a.code})
    q: dict[str, Any] = {"basis": basis, "derived": basis,
                         "method": "shapely STRtree point covered_by Natural Earth 1:10m admin-1 polygon" if
                         basis == "point_in_polygon" else "shapely STRtree query_nearest within max_distance_deg",
                         "ne_adm1_code": a.adm1_code, "ne_feature_index": a.index, "ne_raw_sha256": idx.fetched.sha256,
                         **extra}  # fmt: skip
    if published_code:
        q["published_code_not_an_ne_adm1"] = published_code
    return Placement(a.code, basis, basis, q)


# --------------------------------------------------------------------------- KG helpers
def kg_ref(f: ic.Fetched, *, file: str | None = None, licence: str, **extra: Any) -> dict[str, Any]:
    return {
        "source_file": file or f.name,
        "url": f.url,
        "raw_sha256": f.sha256,
        "retrieved_at": f.retrieved_at.isoformat(),
        "licence": licence,
        **extra,
    }


class KgBuilder:
    """Collects entities and statements of one KG source key (one statement id per subject/property/discriminator)."""

    def __init__(self, source_key: str, revision: str) -> None:
        self.source_key = source_key
        self.revision = revision[:100]
        self.entities: list[Any] = []
        self.statements: list[Any] = []
        self.counts: Counter[str] = Counter()
        self._ids: set[str] = set()
        self._ents: set[str] = set()

    def entity(self, qid: str, kind: str, label: str, *, description: str, aliases: Sequence[str] = (),
               labels: Mapping[str, str] | None = None) -> None:  # fmt: skip
        from reva.kg.model import KgEntity

        if qid in self._ents:
            raise ValueError(f"duplicate entity {qid}")
        self._ents.add(qid)
        al = sorted({a for a in aliases if a and a != label})
        self.entities.append(KgEntity(qid=qid, kind=kind, labels={"en": label[:480], **(labels or {})},
                                      descriptions={"en": description[:480]}, aliases={"en": al} if al else {},
                                      source_revision=self.revision))  # fmt: skip
        self.counts[f"entities_{kind}"] += 1

    def st(self, subject: str, pid: str, *, raw: str, refs: list[dict[str, Any]], obj: str | None = None,
           value: dict[str, Any] | None = None, quals: Mapping[str, Any] | None = None, disc: str = "") -> None:  # fmt: skip
        from reva.kg.model import KgStatement

        sid = f"{subject}:{pid.split(':')[-1]}" + (f":{disc}" if disc else "")
        if sid in self._ids:
            raise ValueError(f"duplicate statement {sid}")
        self._ids.add(sid)
        self.statements.append(KgStatement(subject_qid=subject, property_pid=pid, object_qid=obj, value=value,
                                           qualifiers=dict(quals or {}), references=refs, source_key=self.source_key,
                                           source_revision=self.revision, statement_id=sid[:200], raw_sha256=raw))  # fmt: skip
        self.counts[f"statements_{pid}"] += 1

    def data_series(self, table: str, title: str) -> str:
        qid = f"atlas:{table}"
        if qid not in self._ents:
            self.entity(qid, "data_series", f"atlas table {table}: {title}"[:480],
                        description=f"REVA atlas table {table} (values are read from the atlas, not the KG)")  # fmt: skip
        return qid


def load(run: Any, kb: KgBuilder, predicates: Mapping[str, tuple[str, str, str, str]], wikidata_pids: Sequence[str],
         raws: Sequence[str], retrieved_at: Any, parameters: Mapping[str, Any]) -> dict[str, Any]:  # fmt: skip
    """Register the predicates and load the entities/statements (bounded batches); no engine -> not loaded."""
    from reva.adapters.connectors import atlas_climate_common as cc

    if run.engine is None:
        return {"loaded": False, "entities": len(kb.entities), "statements": len(kb.statements)}
    cc.register_props(run.engine, {**SHARED_PREDICATES, **predicates}, wikidata_pids)
    rep = cc.load_kg(run.engine, run.store, entities=kb.entities, statements=kb.statements,
                     source_key=kb.source_key, raw_sha256s=list(raws), retrieved_at=retrieved_at,
                     origin=run.origin, parameters=dict(parameters))  # fmt: skip
    return {"loaded": True, "entities": len(kb.entities), "statements": len(kb.statements), "load": rep,
            "counts": dict(kb.counts)}  # fmt: skip


def country_qid(iso3: str | None, qids: Mapping[str, str]) -> str | None:
    return qids.get(iso3 or "") if iso3 else None
