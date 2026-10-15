"""Natural Earth 1:10m Admin-0 -> geography atlas tables + land-border adjacency (V3, agent A-GEO).

The 1:110m connector (:mod:`reva.adapters.connectors.naturalearth`) stays the *display* geometry. This
connector archives six 1:10m Admin-0 layers (release 5.1.x, public domain) and derives, by explicit and
tested computation (no shapely/pyproj; :mod:`reva.adapters.connectors.atlas_geodesy`):

* **area** per country: WGS 84 ellipsoidal area of the map-unit polygons (``area_km2``; includes inland
  water inside the polygons) and the area of the major *boundary lakes* that the "countries without
  boundary lakes" layer removes (``boundary_lakes_area_km2``, 13 countries);
* **coastline length at 1:10m** (``coastline_length_km|scale=ne_1:10m``): polygon boundary not shared with
  any other Admin-0 polygon. Coastline length is scale-dependent (coastline paradox): this value is only
  meaningful as "Natural Earth 1:10m"; it is much shorter than figures measured on detailed charts
  (e.g. the CIA World Factbook). Caspian Sea shores are reported separately (``caspian_shore_km``) and are
  not coastline; antimeridian/pole cut edges are excluded;
* **land borders**: every polygon edge shared by two different countries (the 1:10m layers are
  topologically clean: every edge is used by at most two polygons) -> pair lengths, typed by matching the
  edge to the Natural Earth boundary-line layers (international / disputed / line of control / indefinite /
  lease / overlay limits; "water indicator" for river/lake boundaries); disputed classes are flagged;
* **landlocked** status (no coastline; Caspian-only shores count as landlocked, the usual convention;
  doubly landlocked when every land neighbour is landlocked);
* **exclaves / enclaves**: polygons of a country connected to each other through shared internal edges
  form components; every component other than the largest that touches foreign land is an exclave
  (``enclosed``: no coast; ``coastal``: has coast, e.g. Kaliningrad, Cabinda, Alaska); a country or
  component with no coast and exactly one land neighbour is an enclave of that neighbour. Exclaves smaller
  than the 1:10m generalisation (Baarle, Büsingen) are not represented;
* **disputed areas** (``ne_10m_admin_0_disputed_areas``): area of each claim/breakaway polygon with the
  Natural Earth note (who administers, who claims), attached to the administering country as Natural Earth
  records it.

Political view: Natural Earth's *default* Admin-0 layers are a de facto view (e.g. release 5.1.1 assigns
Crimea to Russia, notes "Admin. by Russia; Claimed by Ukraine"). Every row says so in its flags
(``ne_view``); the disputed-area rows name the claims. Features that are not crosswalk countries
(Somaliland, N. Cyprus, Siachen Glacier, Bir Tawil, Taiwan, UN buffer zone, sovereign base areas, ...)
keep their Natural Earth code (``provider_geo``) with ``iso3 = null`` and are listed, never dropped or
merged into a claimant. Borders with such features are kept as borders with that feature.

Map units -> crosswalk entities (deterministic, in this order): ``ISO_A3`` if it is a crosswalk key;
else ``ADM0_A3`` if it is a key (e.g. U.S. Minor Outlying Islands, Korean DMZ halves); else
``ISO_A3_EH`` (Natural Earth's "ISO with exceptions handled": Belgian/British regions, Gaza/West Bank ->
PSE, Baikonur lease -> KAZ, Brazilian Island -> BRA, ...); else the one reviewed alias ``KOS -> XKX``
(Natural Earth "Kosovo", World Bank code ``XKX``); else unmapped. Units assigned through ``ISO_A3_EH``
whose Natural Earth TYPE is Indeterminate/Lease/Disputed/Overlay are flagged on the country's rows.

Atlas period: Natural Earth does not date its boundaries; rows carry the one-day retrieval snapshot
``[retrieval date, +1 day)`` with ``ne_version`` in flags.
"""

from __future__ import annotations

import io
import itertools
import json
import zipfile
from collections import Counter, defaultdict
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np
import shapefile  # type: ignore[import-untyped]  # pyshp

from reva.adapters.connectors.atlas_geo_common import atlas_spec, dec, fetch_raw, snapshot_period
from reva.adapters.connectors.atlas_geodesy import (
    AREA_METHOD,
    LENGTH_METHOD,
    polygon_area_m2,
    ring_signed_area_m2,
    vincenty_inverse_m,
)
from reva.adapters.connectors.base import module_digest, schema_fingerprint
from reva.kernel.errors import SourceResponseError
from reva.ports.connectors import (
    ConnectorSpec,
    Cursor,
    FetchBudget,
    FetchItem,
    FetchOutcome,
    FetchPlan,
    Finding,
    ObservationBatch,
    QualityReport,
    RawAcquisition,
)

CONNECTOR_NAME = "atlas.naturalearth_10m"
CONNECTOR_VERSION = "1.0.0"
SOURCE_KEY = "naturalearth.admin0_10m"
PARSER_VERSION = "atlas-ne10m/1"
ATLAS_SOURCE = "naturalearth"
DS_COUNTRY = "admin0_10m"
DS_BORDERS = "admin0_10m_borders"
DS_DISPUTED = "admin0_10m_disputed"
HOST = "naciscdn.org"
BASE_URL = f"https://{HOST}/naturalearth/10m/cultural/"
MAX_LAYER_BYTES = 12_000_000

#: item key -> (Natural Earth layer name, minimum feature count, required fields)
LAYERS: dict[str, tuple[str, int, tuple[str, ...]]] = {
    "map_units": ("ne_10m_admin_0_map_units", 250, ("ADM0_A3", "GU_A3", "ISO_A3", "ISO_A3_EH", "TYPE", "NAME")),
    "countries": ("ne_10m_admin_0_countries", 230, ("ADM0_A3", "ISO_A3", "ISO_A3_EH", "TYPE")),
    "countries_lakes": ("ne_10m_admin_0_countries_lakes", 230, ("ADM0_A3", "ISO_A3", "ISO_A3_EH")),
    "boundary_lines_land": ("ne_10m_admin_0_boundary_lines_land", 400, ("FEATURECLA", "TYPE")),
    "boundary_lines_disputed": ("ne_10m_admin_0_boundary_lines_disputed_areas", 40, ("FEATURECLA",)),
    "disputed_areas": ("ne_10m_admin_0_disputed_areas", 60, ("BRK_A3", "BRK_NAME", "NOTE_BRK", "ADM0_A3", "TYPE")),
}

RIGHTS = {
    "licence": "Public domain",
    "licence_url": "https://www.naturalearthdata.com/about/terms-of-use/",
    "attribution": "Made with Natural Earth (1:10m Cultural Vectors, Admin 0)",
}

#: Reviewed provider-code alias (the only non-mechanical mapping): Natural Earth ADM0_A3 KOS is "Kosovo",
#: whose crosswalk key is the World Bank code XKX (Natural Earth's own WB_A3 field says "KSV").
REVIEWED_ALIASES = {"KOS": "XKX"}
FLAGGED_EH_TYPES = {"Indeterminate", "Lease", "Disputed", "Overlay"}

#: Natural Earth boundary-line FEATURECLA -> REVA class
LINE_CLASSES = {
    "International boundary (verify)": "international",
    "Disputed (please verify)": "disputed",
    "Line of control (please verify)": "line_of_control",
    "Indefinite (please verify)": "indefinite",
    "Indeterminant frontier": "indeterminate_frontier",
    "Lease limit": "lease_limit",
    "Overlay limit": "overlay_limit",
    "Unrecognized": "unrecognized",
    # boundary_lines_disputed_areas
    "Claim boundary": "claim_boundary",
    "Breakaway": "breakaway",
    "Reference line": "reference_line",
    "Elusive frontier": "elusive_frontier",
}
DISPUTED_CLASSES = frozenset(
    {
        "disputed",
        "line_of_control",
        "indefinite",
        "indeterminate_frontier",
        "unrecognized",
        "claim_boundary",
        "breakaway",
        "elusive_frontier",
    }
)
#: The Caspian Sea (endorheic) lies entirely inside this box, which contains no ocean coast.
CASPIAN_BOX = (46.0, 36.4, 55.5, 47.3)
Q = 10_000_000  # coordinate quantisation for edge matching: 1e-7 degree (~1 cm)
SCALE_LABEL = "ne_1:10m"


class NaturalEarth10mError(SourceResponseError):
    pass


def _err(msg: str, kind: str = "schema_drift") -> NaturalEarth10mError:
    return NaturalEarth10mError(msg, retryable=False, kind=kind)


# ============================================================================== shapefile reading
@dataclass
class Feature:
    idx: int
    rec: dict[str, Any]
    rings: list[np.ndarray]  # each (n, 2) lon/lat, closed


@dataclass
class Layer:
    name: str
    version: str | None
    fields: list[str]
    features: list[Feature]


def read_layer(body: bytes, name: str) -> Layer:
    try:
        zf = zipfile.ZipFile(io.BytesIO(body))
    except zipfile.BadZipFile as e:
        raise _err(f"{name}: not a zip archive") from e
    members: dict[str, bytes] = {}
    for info in zf.infolist():
        ext = info.filename.rsplit(".", 1)[-1].lower()
        if ext in ("shp", "shx", "dbf", "cpg", "prj") or info.filename.upper().endswith("VERSION.TXT"):
            if info.file_size > 60_000_000:
                raise _err(f"{name}: member {info.filename} too large", kind="too_large")
            members[ext if not info.filename.upper().endswith("VERSION.TXT") else "version"] = zf.read(info)
    if not all(k in members for k in ("shp", "shx", "dbf")):
        raise _err(f"{name}: archive lacks shp/shx/dbf")
    version = members["version"].decode("utf-8", "replace").strip() if "version" in members else None
    reader = shapefile.Reader(
        shp=io.BytesIO(members["shp"]),
        shx=io.BytesIO(members["shx"]),
        dbf=io.BytesIO(members["dbf"]),
        encoding="utf-8",
    )
    fields = [f[0] for f in reader.fields[1:]]
    feats: list[Feature] = []
    for i, sr in enumerate(reader.iterShapeRecords()):
        pts = np.asarray(sr.shape.points, dtype=float)
        parts = [*sr.shape.parts, len(pts)]
        rings = [pts[a:b] for a, b in itertools.pairwise(parts) if b - a >= 2]
        rec = {k: (v.strip() if isinstance(v, str) else v) for k, v in sr.record.as_dict().items()}
        feats.append(Feature(i, rec, rings))
    return Layer(name, version, fields, feats)


# ============================================================================== entity assignment
def assign_entity(rec: Mapping[str, Any], keys: frozenset[str] | set[str]) -> tuple[str | None, str]:
    """(crosswalk key or None, rule) for a Natural Earth Admin-0 record (rules in the module docstring)."""
    iso, adm, eh = str(rec.get("ISO_A3") or ""), str(rec.get("ADM0_A3") or ""), str(rec.get("ISO_A3_EH") or "")
    if iso in keys:
        return iso, "ISO_A3"
    if adm in keys:
        return adm, "ADM0_A3"
    if eh in keys:
        return eh, "ISO_A3_EH"
    if adm in REVIEWED_ALIASES and REVIEWED_ALIASES[adm] in keys:
        return REVIEWED_ALIASES[adm], "reviewed_alias"
    return None, "unmapped"


def _crosswalk_keys() -> frozenset[str]:
    from reva.atlas.tables import known_country_keys

    return known_country_keys()


# ============================================================================== geometry model
@dataclass
class Polygon:
    pid: int
    unit: int  # map-unit feature index
    outer: np.ndarray
    holes: list[np.ndarray] = field(default_factory=list)

    @property
    def area_m2(self) -> float:
        a = abs(ring_signed_area_m2(self.outer[:, 0], self.outer[:, 1]))
        return a - sum(abs(ring_signed_area_m2(h[:, 0], h[:, 1])) for h in self.holes)


def _point_in_ring(x: float, y: float, ring: np.ndarray) -> bool:
    xs, ys = ring[:, 0], ring[:, 1]
    x1, y1, x2, y2 = xs[:-1], ys[:-1], xs[1:], ys[1:]
    cond = (y1 > y) != (y2 > y)
    with np.errstate(divide="ignore", invalid="ignore"):
        xint = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
    return bool(np.count_nonzero(cond & (x < xint)) % 2)


def organise_polygons(feature: Feature, start_pid: int) -> list[Polygon]:
    """Split a shapefile polygon record into outer rings (clockwise) with their holes (counter-clockwise)."""
    outers: list[Polygon] = []
    holes: list[np.ndarray] = []
    for ring in feature.rings:
        s = ring_signed_area_m2(ring[:, 0], ring[:, 1])
        if s <= 0:
            outers.append(Polygon(start_pid + len(outers), feature.idx, ring))
        else:
            holes.append(ring)
    if not outers and holes:  # orientation not as specified: treat every ring as an outer ring
        return [Polygon(start_pid + i, feature.idx, r) for i, r in enumerate(holes)]
    for h in holes:
        x, y = float(h[0, 0]), float(h[0, 1])
        cands = [p for p in outers if _point_in_ring(x, y, p.outer)] or outers
        best = min(cands, key=lambda p: abs(ring_signed_area_m2(p.outer[:, 0], p.outer[:, 1])))
        best.holes.append(h)
    return outers


@dataclass
class Segments:
    """Every polygon edge with its owner, length and canonical (quantised) key."""

    key: np.ndarray  # (n, 4) int64 canonical endpoints
    poly: np.ndarray  # polygon id
    hole: np.ndarray  # bool: on a hole ring
    length_m: np.ndarray
    mid: np.ndarray  # (n, 2) midpoints
    artificial: np.ndarray  # bool: antimeridian or pole cut


def _ring_segments(ring: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    q = np.round(ring * Q).astype(np.int64)
    a, b = q[:-1], q[1:]
    keep = np.any(a != b, axis=1)
    a, b = a[keep], b[keep]
    swap = (a[:, 0] > b[:, 0]) | ((a[:, 0] == b[:, 0]) & (a[:, 1] > b[:, 1]))
    lo = np.where(swap[:, None], b, a)
    hi = np.where(swap[:, None], a, b)
    key = np.hstack([lo, hi])
    r0, r1 = ring[:-1][keep], ring[1:][keep]
    length = vincenty_inverse_m(r0[:, 0], r0[:, 1], r1[:, 0], r1[:, 1])
    mid = (r0 + r1) / 2.0
    art = ((np.abs(a[:, 0]) == 180 * Q) & (a[:, 0] == b[:, 0])) | ((a[:, 1] == -90 * Q) & (b[:, 1] == -90 * Q))
    return key, length, mid, art


def build_segments(polys: Sequence[Polygon]) -> Segments:
    keys, pid, hole, lens, mids, arts = [], [], [], [], [], []
    for p in polys:
        for j, ring in enumerate([p.outer, *p.holes]):
            k, ln, md, art = _ring_segments(ring)
            keys.append(k)
            lens.append(ln)
            mids.append(md)
            arts.append(art)
            pid.append(np.full(len(k), p.pid, dtype=np.int64))
            hole.append(np.full(len(k), j > 0))
    if np.isnan(np.concatenate(lens)).any():  # pragma: no cover - Vincenty cannot fail on km-scale edges
        raise _err("geodesic length did not converge", kind="computation")
    return Segments(
        np.vstack(keys),
        np.concatenate(pid),
        np.concatenate(hole),
        np.concatenate(lens),
        np.vstack(mids),
        np.concatenate(arts),
    )


def line_segment_classes(layers: Sequence[Layer]) -> dict[tuple[int, int, int, int], tuple[str, bool, int]]:
    """Canonical edge key -> (REVA boundary class, water indicator, ne_id) from boundary-line layers."""
    out: dict[tuple[int, int, int, int], tuple[str, bool, int]] = {}
    for layer in layers:
        for f in layer.features:
            cls = LINE_CLASSES.get(str(f.rec.get("FEATURECLA")), "other:" + str(f.rec.get("FEATURECLA"))[:40])
            water = str(f.rec.get("TYPE") or "") == "Water Indicator"
            for part in f.rings:
                k, _, _, _ = _ring_segments(part)
                for row in map(tuple, k.tolist()):
                    out.setdefault(row, (cls, water, int(f.rec.get("ne_id") or 0)))
    return out


# ============================================================================== computation
@dataclass
class Unit:
    idx: int
    code: str  # Natural Earth GU_A3 (map-unit code)
    adm0: str
    entity: str | None
    rule: str
    ne_type: str
    name: str

    @property
    def key(self) -> str:
        """Grouping key: crosswalk entity, or the Natural Earth ADM0_A3 code of an unmapped feature."""
        return self.entity if self.entity else f"ne:{self.adm0}"


@dataclass
class Component:
    key: str
    polys: list[int]
    area_m2: float
    coast_m: float = 0.0
    caspian_m: float = 0.0
    neighbours: dict[str, float] = field(default_factory=dict)
    bbox: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)


@dataclass
class PairBorder:
    a: str
    b: str
    length_m: float
    by_class_m: dict[str, float]
    water_m: float

    @property
    def disputed(self) -> bool:
        return any(c in DISPUTED_CLASSES for c, v in self.by_class_m.items() if v > 0)


@dataclass
class GeoComputation:
    ne_version: str | None
    units: list[Unit]
    area_m2: dict[str, float]
    coast_m: dict[str, float]
    caspian_m: dict[str, float]
    inner_unshared_m: dict[str, float]
    borders: dict[tuple[str, str], PairBorder]
    components: dict[str, list[Component]]
    lakes_m2: dict[str, float]
    lakes_rule: dict[str, str]
    diagnostics: dict[str, Any]

    def neighbours(self, key: str) -> dict[str, float]:
        out: dict[str, float] = {}
        for (a, b), pb in self.borders.items():
            if a == key:
                out[b] = pb.length_m
            elif b == key:
                out[a] = pb.length_m
        return out

    def landlocked(self, key: str) -> bool:
        return self.coast_m.get(key, 0.0) == 0.0


class _UF:
    def __init__(self, n: int) -> None:
        self.p = list(range(n))

    def find(self, x: int) -> int:
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[max(ra, rb)] = min(ra, rb)


def compute(layers: Mapping[str, Layer], keys: frozenset[str] | set[str]) -> GeoComputation:
    mu = layers["map_units"]
    units = []
    for f in mu.features:
        ent, rule = assign_entity(f.rec, keys)
        units.append(
            Unit(
                f.idx,
                str(f.rec.get("GU_A3") or f.rec.get("ADM0_A3")),
                str(f.rec.get("ADM0_A3")),
                ent,
                rule,
                str(f.rec.get("TYPE") or ""),
                str(f.rec.get("NAME") or ""),
            )
        )
    polys: list[Polygon] = []
    for f in mu.features:
        polys.extend(organise_polygons(f, len(polys)))
    pkey = [units[p.unit].key for p in polys]
    parea = [p.area_m2 for p in polys]
    area: dict[str, float] = defaultdict(float)
    for k, pa_m2 in zip(pkey, parea, strict=True):
        area[k] += pa_m2

    seg = build_segments(polys)
    uniq, inverse, counts = np.unique(seg.key, axis=0, return_inverse=True, return_counts=True)
    inverse = inverse.reshape(-1)
    if counts.max(initial=0) > 2:
        raise _err(f"topology: an edge is used by {int(counts.max())} polygons (expected <= 2)", kind="topology")
    order = np.argsort(inverse, kind="stable")
    starts = np.concatenate([[0], np.cumsum(counts)[:-1]])

    line_cls = line_segment_classes([layers["boundary_lines_land"], layers["boundary_lines_disputed"]])
    uf = _UF(len(polys))
    coast: dict[str, float] = defaultdict(float)
    caspian: dict[str, float] = defaultdict(float)
    inner: dict[str, float] = defaultdict(float)
    pcoast: dict[int, float] = defaultdict(float)
    pcasp: dict[int, float] = defaultdict(float)
    pnb: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    borders: dict[tuple[str, str], PairBorder] = {}
    untyped_m = 0.0
    artificial_m = 0.0
    x0, y0, x1, y1 = CASPIAN_BOX
    for g in range(len(uniq)):
        o = order[starts[g] : starts[g] + counts[g]]
        i = int(o[0])
        L = float(seg.length_m[i])
        if counts[g] == 1:
            pi = int(seg.poly[i])
            k = pkey[pi]
            if seg.artificial[i]:
                artificial_m += L
                continue
            if seg.hole[i]:
                inner[k] += L
                continue
            mx, my = seg.mid[i]
            if x0 <= mx <= x1 and y0 <= my <= y1:
                caspian[k] += L
                pcasp[pi] += L
            else:
                coast[k] += L
                pcoast[pi] += L
            continue
        pa, pb_ = int(seg.poly[o[0]]), int(seg.poly[o[1]])
        ka, kb = pkey[pa], pkey[pb_]
        if ka == kb:
            uf.union(pa, pb_)
            continue
        pnb[pa][kb] += L
        pnb[pb_][ka] += L
        lo_k, hi_k = sorted((ka, kb))
        pair = borders.get((lo_k, hi_k))
        if pair is None:
            pair = borders[(lo_k, hi_k)] = PairBorder(lo_k, hi_k, 0.0, defaultdict(float), 0.0)
        pair.length_m += L
        cls = line_cls.get(tuple(uniq[g].tolist()))
        if cls is None:
            pair.by_class_m["untyped"] += L
            untyped_m += L
        else:
            pair.by_class_m[cls[0]] += L
            if cls[1]:
                pair.water_m += L

    # Polygons of one country split at the antimeridian (Russia/Chukotka, Fiji, Antarctica, ...) are one piece.
    am: dict[str, list[tuple[int, int, float, float]]] = defaultdict(list)
    for p in polys:
        for side in (-180.0, 180.0):
            on = p.outer[np.abs(p.outer[:, 0] - side) < 1e-9]
            if len(on) >= 2:
                am[pkey[p.pid]].append((p.pid, int(side), float(on[:, 1].min()), float(on[:, 1].max())))
    for lst in am.values():
        for pid_a, sa, lo_a, hi_a in lst:
            for pid_b, sb, lo_b, hi_b in lst:
                if sa < sb and lo_a <= hi_b and lo_b <= hi_a:
                    uf.union(pid_a, pid_b)

    comps: dict[int, Component] = {}
    for p in polys:
        r = uf.find(p.pid)
        c = comps.get(r)
        bb = (
            float(p.outer[:, 0].min()),
            float(p.outer[:, 1].min()),
            float(p.outer[:, 0].max()),
            float(p.outer[:, 1].max()),
        )
        if c is None:
            c = comps[r] = Component(pkey[p.pid], [], 0.0, bbox=bb)
        c.polys.append(p.pid)
        c.area_m2 += parea[p.pid]
        c.coast_m += pcoast.get(p.pid, 0.0)
        c.caspian_m += pcasp.get(p.pid, 0.0)
        for nk, ln in pnb.get(p.pid, {}).items():
            c.neighbours[nk] = c.neighbours.get(nk, 0.0) + ln
        c.bbox = (min(c.bbox[0], bb[0]), min(c.bbox[1], bb[1]), max(c.bbox[2], bb[2]), max(c.bbox[3], bb[3]))
    by_key: dict[str, list[Component]] = defaultdict(list)
    for c in comps.values():
        by_key[c.key].append(c)
    for comp_list in by_key.values():
        comp_list.sort(key=lambda c: -c.area_m2)

    # Boundary lakes: countries layer minus countries-without-boundary-lakes layer, per ADM0_A3 feature.
    lakes: dict[str, float] = defaultdict(float)
    lakes_rule: dict[str, str] = {}
    ca = {f.rec["ADM0_A3"]: f for f in layers["countries"].features}
    cl = {f.rec["ADM0_A3"]: f for f in layers["countries_lakes"].features}
    for adm, f in ca.items():
        g2 = cl.get(adm)
        if g2 is None:
            continue
        d = polygon_area_m2([(r[:, 0], r[:, 1]) for r in f.rings]) - polygon_area_m2(
            [(r[:, 0], r[:, 1]) for r in g2.rings]
        )
        if d > 1e6:  # > 1 km2
            ent, rule = assign_entity(f.rec, keys)
            k = ent or f"ne:{adm}"
            lakes[k] += d
            lakes_rule[k] = f"ADM0_A3 {adm} via {rule}"

    # Cross-check: map units vs countries layer area per ADM0_A3 (same release, same geometry)
    mu_by_adm: dict[str, float] = defaultdict(float)
    for p in polys:
        mu_by_adm[units[p.unit].adm0] += parea[p.pid]
    worst = 0.0
    for adm, f in ca.items():
        a_c = polygon_area_m2([(r[:, 0], r[:, 1]) for r in f.rings])
        if adm in mu_by_adm and a_c > 1e7:
            worst = max(worst, abs(mu_by_adm[adm] - a_c) / a_c)

    total_shared = float(sum(pb.length_m for pb in borders.values()))
    diagnostics = {
        "polygons": len(polys),
        "edges": len(seg.key),
        "unique_edges": len(uniq),
        "shared_edges": int(np.count_nonzero(counts == 2)),
        "border_km_total": round(total_shared / 1e3, 1),
        "border_km_untyped": round(untyped_m / 1e3, 1),
        "border_typed_share": round(1 - untyped_m / total_shared, 4) if total_shared else None,
        "artificial_cut_km_excluded": round(artificial_m / 1e3, 1),
        "inner_unshared_km": {k: round(v / 1e3, 1) for k, v in inner.items() if v > 0},
        "map_units_vs_countries_max_rel_area_diff": worst,
        "unmapped_units": sorted({u.adm0 for u in units if u.entity is None}),
    }
    return GeoComputation(
        mu.version,
        units,
        dict(area),
        dict(coast),
        dict(caspian),
        dict(inner),
        borders,
        dict(by_key),
        dict(lakes),
        lakes_rule,
        diagnostics,
    )


# ============================================================================== atlas rows
def _flags(**kw: Any) -> str:
    return json.dumps(
        {k: v for k, v in kw.items() if v is not None},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def _km(m: float) -> str:
    return dec(m / 1e3, 2)


@dataclass
class RawRef:
    sha256: str
    retrieved_at: datetime
    file: str


def _unit_groups(comp: GeoComputation) -> dict[str, list[Unit]]:
    g: dict[str, list[Unit]] = defaultdict(list)
    for u in comp.units:
        g[u.key].append(u)
    return g


def _provider_geo(key: str, units: Sequence[Unit]) -> str:
    if key.startswith("ne:"):
        return key[3:]
    codes = sorted({u.code for u in units})
    pg = "+".join(codes)
    return pg if len(pg) <= 64 else f"{codes[0]}+{len(codes) - 1}_more"


def atlas_rows(
    comp: GeoComputation,
    disputed: Layer,
    raws: Mapping[str, RawRef],
    keys: frozenset[str] | set[str],
) -> dict[str, list[dict[str, Any]]]:
    """Rows of the three atlas tables (dataset -> rows in ATLAS_SCHEMA column order)."""
    mu_raw = raws["map_units"]
    start, end = snapshot_period(mu_raw.retrieved_at)
    groups = _unit_groups(comp)
    base_flags = {"ne_version": comp.ne_version, "ne_view": "default (de facto) Admin-0", "derived": "computed"}

    def row(
        ds: str,
        indicator: str,
        key: str,
        value: str | None,
        unit: str,
        flags: dict[str, Any],
        raw: RawRef,
        selector: dict[str, Any],
        value_text: str | None = None,
    ) -> dict[str, Any]:
        return {
            "source": ATLAS_SOURCE,
            "dataset": ds,
            "indicator": indicator,
            "concept_id": None,
            "iso3": None if key.startswith("ne:") else key,
            "provider_geo": _provider_geo(key, groups.get(key, [])),
            "entity_qid": None,
            "period_start": start,
            "period_end": end,
            "value": value,
            "value_text": value_text,
            "unit": unit,
            "flags": _flags(**base_flags, **flags),
            "raw_sha256": raw.sha256,
            "selector": json.dumps(selector, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
        }

    out: dict[str, list[dict[str, Any]]] = {DS_COUNTRY: [], DS_BORDERS: [], DS_DISPUTED: []}
    landlocked = {k: comp.landlocked(k) for k in groups}
    for key in sorted(groups):
        us = groups[key]
        sel = {"file": f"{LAYERS['map_units'][0]}.shp", "ne_units": sorted(u.code for u in us)}
        unit_notes = [
            {"ne_unit": u.code, "name": u.name, "ne_type": u.ne_type, "rule": u.rule}
            for u in sorted(us, key=lambda u: u.code)
            if u.rule != "ISO_A3" or u.ne_type in FLAGGED_EH_TYPES
        ]
        flagged = [n for n in unit_notes if n["ne_type"] in FLAGGED_EH_TYPES]
        uf = {
            "ne_units_note": unit_notes or None,
            "includes_contested_units": flagged or None,
            "unmapped_feature": True if key.startswith("ne:") else None,
        }
        R = out[DS_COUNTRY]
        R.append(
            row(
                DS_COUNTRY,
                "area_km2",
                key,
                dec(comp.area_m2.get(key, 0.0) / 1e6, 2),
                "km2",
                {**uf, "method": AREA_METHOD, "includes": "inland water inside Admin-0 polygons"},
                mu_raw,
                {**sel, "computation": "area"},
            )
        )
        if key in comp.lakes_m2:
            R.append(
                row(
                    DS_COUNTRY,
                    "boundary_lakes_area_km2",
                    key,
                    dec(comp.lakes_m2[key] / 1e6, 2),
                    "km2",
                    {
                        "method": AREA_METHOD,
                        "definition": "area removed by ne_10m_admin_0_countries_lakes "
                        "(major boundary lakes) from ne_10m_admin_0_countries",
                        "rule": comp.lakes_rule[key],
                    },
                    raws["countries_lakes"],
                    {
                        "file": f"{LAYERS['countries_lakes'][0]}.shp",
                        "also": f"{LAYERS['countries'][0]}.shp",
                        "computation": "area_difference",
                    },
                )
            )
        coast = comp.coast_m.get(key, 0.0)
        R.append(
            row(
                DS_COUNTRY,
                f"coastline_length_km|scale={SCALE_LABEL}",
                key,
                _km(coast),
                "km",
                {
                    "method": LENGTH_METHOD,
                    "scale": "1:10,000,000",
                    "scale_dependent": True,
                    "definition": "Admin-0 polygon boundary not shared with another Admin-0 polygon, excluding "
                    "Caspian shores and antimeridian/pole cuts; includes islands",
                    "caution": "coastline length depends on map scale; not comparable with large-scale figures",
                },
                mu_raw,
                {**sel, "computation": "unshared_boundary_length"},
            )
        )
        if comp.caspian_m.get(key, 0.0) > 0:
            R.append(
                row(
                    DS_COUNTRY,
                    f"caspian_shore_km|scale={SCALE_LABEL}",
                    key,
                    _km(comp.caspian_m[key]),
                    "km",
                    {
                        "method": LENGTH_METHOD,
                        "scale": "1:10,000,000",
                        "definition": f"unshared boundary inside the Caspian box {list(CASPIAN_BOX)}",
                    },
                    mu_raw,
                    {**sel, "computation": "caspian_shore_length"},
                )
            )
        nb = comp.neighbours(key)
        land_m = sum(nb.values())
        R.append(
            row(
                DS_COUNTRY,
                f"land_border_length_km|scale={SCALE_LABEL}",
                key,
                _km(land_m),
                "km",
                {
                    "method": LENGTH_METHOD,
                    "scale": "1:10,000,000",
                    "definition": "sum of Admin-0 polygon edges shared with other countries/features "
                    "(includes river and lake boundaries)",
                },
                mu_raw,
                {**sel, "computation": "shared_boundary_length"},
            )
        )
        unm = sorted(k[3:] for k in nb if k.startswith("ne:"))
        R.append(
            row(
                DS_COUNTRY,
                "land_neighbours_count",
                key,
                str(len(nb)),
                "count",
                {
                    "neighbours": sorted(k for k in nb if not k.startswith("ne:")),
                    "unmapped_ne_neighbours": unm or None,
                    "definition": "distinct Admin-0 features sharing at least one polygon edge",
                },
                mu_raw,
                {**sel, "computation": "adjacency"},
            )
        )
        ll = landlocked[key]
        doubly = ll and bool(nb) and all(landlocked.get(n, False) for n in nb)
        R.append(
            row(
                DS_COUNTRY,
                "landlocked",
                key,
                None,
                "1",
                {
                    "doubly_landlocked": doubly if ll else None,
                    "caspian_only_shore": True if ll and comp.caspian_m.get(key, 0.0) > 0 else None,
                    "definition": "no coastline at 1:10m (Caspian shores do not count as sea coast)",
                },
                mu_raw,
                {**sel, "computation": "landlocked"},
                value_text="yes" if ll else "no",
            )
        )
        comps = comp.components.get(key, [])
        all_nb = {n for c in comps for n in c.neighbours}
        if comps and sum(c.coast_m for c in comps) == 0 and len(all_nb) == 1:
            host = next(iter(all_nb))
            R.append(
                row(
                    DS_COUNTRY,
                    "enclave_of",
                    key,
                    None,
                    "1",
                    {
                        "definition": "no coastline and exactly one land neighbour (fully surrounded)",
                        "host_unmapped": True if host.startswith("ne:") else None,
                    },
                    mu_raw,
                    {**sel, "computation": "enclave"},
                    value_text=host.removeprefix("ne:"),
                )
            )
        for n, c in enumerate(comps[1:], start=1):
            if not c.neighbours:
                continue  # an island component, not an exclave
            kind = "enclosed" if c.coast_m == 0 and c.caspian_m == 0 else "coastal"
            R.append(
                row(
                    DS_COUNTRY,
                    f"exclave_area_km2|component={n}",
                    key,
                    dec(c.area_m2 / 1e6, 2),
                    "km2",
                    {
                        "exclave_kind": kind,
                        "neighbours_km": {
                            k.removeprefix("ne:"): round(v / 1e3, 2) for k, v in sorted(c.neighbours.items())
                        },
                        "coast_km": round(c.coast_m / 1e3, 2),
                        "bbox": [round(v, 3) for v in c.bbox],
                        "definition": "component (polygons joined by shared internal edges) other than the "
                        "largest one that touches foreign land",
                        "enclave_of": (
                            next(iter(c.neighbours)).removeprefix("ne:")
                            if kind == "enclosed" and len(c.neighbours) == 1
                            else None
                        ),
                    },
                    mu_raw,
                    {**sel, "computation": "exclave", "component_rank_by_area": n},
                )
            )
        R.append(
            row(
                DS_COUNTRY,
                "land_components_count",
                key,
                str(len(comps)),
                "count",
                {"definition": "connected pieces (islands, exclaves and the main territory) at 1:10m"},
                mu_raw,
                {**sel, "computation": "components"},
            )
        )

    # ---- border pairs (both directions)
    B = out[DS_BORDERS]
    lines_raw = raws["boundary_lines_land"]
    for (a, b), pb in sorted(comp.borders.items()):
        for s, t in ((a, b), (b, a)):
            partner_units = groups.get(t, [])
            B.append(
                row(
                    DS_BORDERS,
                    f"land_border_km|partner={t.removeprefix('ne:')}",
                    s,
                    _km(pb.length_m),
                    "km",
                    {
                        "partner_iso3": None if t.startswith("ne:") else t,
                        "partner_unmapped_ne_feature": t[3:] if t.startswith("ne:") else None,
                        "partner_ne_type": sorted({u.ne_type for u in partner_units}) if t.startswith("ne:") else None,
                        "by_class_km": {c: round(v / 1e3, 2) for c, v in sorted(pb.by_class_m.items()) if v > 0},
                        "water_boundary_km": round(pb.water_m / 1e3, 2) if pb.water_m else None,
                        "disputed": pb.disputed,
                        "scale": "1:10,000,000",
                        "method": LENGTH_METHOD,
                        "definition": "length of Admin-0 polygon edges shared by the two features; classes from "
                        "matching Natural Earth boundary-line edges ('untyped' when no line edge matches)",
                    },
                    mu_raw,
                    {
                        "file": f"{LAYERS['map_units'][0]}.shp",
                        "pair": [s.removeprefix("ne:"), t.removeprefix("ne:")],
                        "computation": "shared_boundary_length",
                        "typing": f"{LAYERS['boundary_lines_land'][0]}.shp (sha256 {lines_raw.sha256[:16]}…) + "
                        f"{LAYERS['boundary_lines_disputed'][0]}.shp",
                    },
                )
            )

    # ---- disputed areas
    D = out[DS_DISPUTED]
    draw = raws["disputed_areas"]
    seen: Counter[str] = Counter()
    for f in disputed.features:
        rec = f.rec
        ent, rule = assign_entity(rec, keys)
        brk = str(rec.get("BRK_A3") or f"idx{f.idx}")
        seen[brk] += 1
        pg = brk if seen[brk] == 1 else f"{brk}#{seen[brk]}"
        area_d = polygon_area_m2([(r[:, 0], r[:, 1]) for r in f.rings])
        D.append(
            {
                "source": ATLAS_SOURCE,
                "dataset": DS_DISPUTED,
                "indicator": "disputed_area_km2",
                "concept_id": None,
                "iso3": ent,
                "provider_geo": pg,
                "entity_qid": None,
                "period_start": start,
                "period_end": end,
                "value": dec(abs(area_d) / 1e6, 2),
                "value_text": None,
                "unit": "km2",
                "flags": _flags(
                    **base_flags,
                    method=AREA_METHOD,
                    ne_name=rec.get("BRK_NAME"),
                    ne_note=rec.get("NOTE_BRK") or None,
                    ne_type=rec.get("TYPE"),
                    ne_featurecla=rec.get("featurecla"),
                    ne_adm0_a3=rec.get("ADM0_A3"),
                    ne_sov_a3=rec.get("SOV_A3"),
                    ne_admin=rec.get("ADMIN"),
                    iso3_role="the Admin-0 feature Natural Earth attaches the area to (administering "
                    "side in the de facto view); not a judgement on the claim",
                    iso3_rule=rule,
                ),
                "raw_sha256": draw.sha256,
                "selector": json.dumps(
                    {"file": f"{LAYERS['disputed_areas'][0]}.shp", "record": f.idx, "BRK_A3": brk},
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            }
        )
    return out


# ============================================================================== KG statements
def adjacency_statements(
    comp: GeoComputation,
    qids: Mapping[str, str],
    *,
    raw_sha256: str,
    source_revision: str,
    retrieved_at: datetime,
) -> tuple[list[Any], dict[str, Any]]:
    """``shares border with`` (P47) statements, both directions, for pairs whose two countries have a QID.

    Qualifiers carry the 1:10m shared-border length (decimal string, km), the boundary classes and the
    disputed flag; references name the Natural Earth layer and raw sha256. Pairs with an unmapped Natural
    Earth feature or a country without a QID are skipped and counted (never guessed)."""
    from reva.kg.model import KgStatement

    sts: list[Any] = []
    skipped: Counter[str] = Counter()
    for (a, b), pb in sorted(comp.borders.items()):
        if a.startswith("ne:") or b.startswith("ne:"):
            skipped["unmapped_ne_feature"] += 1
            continue
        if a not in qids or b not in qids:
            skipped["no_qid"] += 1
            continue
        for s, t in ((a, b), (b, a)):
            sts.append(
                KgStatement(
                    subject_qid=qids[s],
                    property_pid="P47",
                    object_qid=qids[t],
                    qualifiers={
                        "border_length_km": {"type": "quantity", "amount": _km(pb.length_m), "unit": "km"},
                        "scale": "1:10,000,000 (Natural Earth 1:10m)",
                        "boundary_classes_km": {c: dec(v / 1e3, 2) for c, v in sorted(pb.by_class_m.items()) if v > 0},
                        "disputed_boundary": pb.disputed,
                        "border_kind": "land (shared Admin-0 polygon edges; includes river/lake boundaries)",
                        "subject_iso3": s,
                        "object_iso3": t,
                        "method": LENGTH_METHOD,
                        "political_view": "Natural Earth default (de facto)",
                    },
                    references=[
                        {
                            "source": "Natural Earth 1:10m Admin 0 map units",
                            "ne_version": source_revision,
                            "url": BASE_URL + LAYERS["map_units"][0] + ".zip",
                            "raw_sha256": raw_sha256,
                            "retrieved_at": retrieved_at.isoformat(),
                            "derived": "computed by A-GEO",
                        }
                    ],
                    source_key=SOURCE_KEY,
                    source_revision=source_revision,
                    statement_id=f"ne10m-border:{s}:{t}",
                    raw_sha256=raw_sha256,
                )
            )
    return sts, {"statements": len(sts), "skipped_pairs": dict(skipped)}


# ============================================================================== connector
class NaturalEarth10mConnector:
    """Archive-only connector: six layer ZIPs become evidence acquisitions; :func:`build` derives the atlas
    tables and the adjacency statements from all of them together."""

    def describe(self) -> ConnectorSpec:
        # atlas_bulk, not naturalearth_asset: the generic runner's naturalearth_asset post-publish hook
        # would turn these layers into 1:110m-style display geometries.
        return atlas_spec(
            name=CONNECTOR_NAME,
            version=CONNECTOR_VERSION,
            source_key=SOURCE_KEY,
            provider="Natural Earth",
            licence=RIGHTS["licence"],
            allowed_hosts=(HOST,),
            dataset_ids=tuple(v[0] for v in LAYERS.values()),
            field_mappings={"map_unit": "GU_A3", "entity": "ISO_A3 > ADM0_A3 > ISO_A3_EH > KOS->XKX"},
            units={"area": "km2", "length": "km"},
            geography=("world",),
            time_semantics="release-versioned geometry; atlas rows dated by retrieval snapshot",
            lineage_family="natural_earth",
            cadence_seconds=30 * 86400,
            revision_strategy="VERSION.txt is the source revision; identical bytes are a no-op",
            max_response_bytes=MAX_LAYER_BYTES,
            max_pages=1,
            parser_version=PARSER_VERSION,
            code_digest=module_digest(NaturalEarth10mConnector),
        )

    def plan(self, cursor: Cursor | None, budget: FetchBudget) -> FetchPlan:
        return FetchPlan(connector=CONNECTOR_NAME, items=fetch_items(), pagination="one ZIP per layer")

    async def fetch(self, item: FetchItem, http: Any) -> RawAcquisition:
        return await fetch_raw(item, http)

    def parse(self, raw: RawAcquisition) -> Iterator[ObservationBatch]:
        name, _, req = LAYERS[raw.item.key]
        layer = read_layer(raw.body, name)
        missing = [f for f in req if f not in layer.fields]
        yield ObservationBatch(
            connector=CONNECTOR_NAME,
            connector_version=CONNECTOR_VERSION,
            item_key=raw.item.key,
            observations=(),
            metadata={"feature_count": len(layer.features), "ne_version": layer.version, "missing_fields": missing},
            schema_fingerprint=schema_fingerprint(sorted(layer.fields)),
        )

    def validate(self, batch: ObservationBatch) -> QualityReport:
        _, minimum, _ = LAYERS[batch.item_key]
        findings = []
        if batch.metadata.get("feature_count", 0) < minimum:
            findings.append(Finding(code="incomplete", severity="blocker", message=f"fewer than {minimum} features"))
        if batch.metadata.get("missing_fields"):
            findings.append(
                Finding(
                    code="schema_drift",
                    severity="blocker",
                    message=f"missing fields {batch.metadata['missing_fields']}",
                )
            )
        return QualityReport(
            outcome="blocked" if findings else "pass",
            findings=tuple(findings),
            received_count=batch.metadata.get("feature_count", 0),
        )

    def next_cursor(self, plan: FetchPlan, outcomes: Sequence[FetchOutcome]) -> Cursor:
        return Cursor(state={"complete": all(o.status in ("published", "unchanged") for o in outcomes)})


def fetch_items() -> tuple[FetchItem, ...]:
    return tuple(
        FetchItem(
            key=k,
            url=f"{BASE_URL}{name}.zip",
            max_bytes=MAX_LAYER_BYTES,
            expected_media_types=("application/zip", "application/octet-stream"),
        )
        for k, (name, _, _) in LAYERS.items()
    )


@dataclass
class NeBuild:
    computation: GeoComputation
    rows: dict[str, list[dict[str, Any]]]
    ne_version: str | None
    raw_sha256s: dict[str, str]


def build(
    raws: Mapping[str, RawAcquisition], raw_sha256s: Mapping[str, str], keys: frozenset[str] | set[str] | None = None
) -> NeBuild:
    """Parse and validate every layer, compute the geography and the atlas rows (no I/O)."""
    keys = keys if keys is not None else _crosswalk_keys()
    conn = NaturalEarth10mConnector()
    layers: dict[str, Layer] = {}
    for k, (name, _, _) in LAYERS.items():
        if k not in raws:
            raise _err(f"layer {k} missing", kind="incomplete")
        for b in conn.parse(raws[k]):
            q = conn.validate(b)
            if q.blocking:
                raise _err(f"{name}: " + "; ".join(f.message for f in q.findings))
        layers[k] = read_layer(raws[k].body, name)
    comp = compute(layers, keys)
    refs = {k: RawRef(raw_sha256s[k], raws[k].retrieved_at, LAYERS[k][0]) for k in LAYERS}
    rows = atlas_rows(comp, layers["disputed_areas"], refs, keys)
    return NeBuild(comp, rows, layers["map_units"].version, dict(raw_sha256s))


async def ingest(
    engine: Any, store: Any, http: Any, *, qids: Mapping[str, str] | None = None, load_kg: bool = True
) -> dict[str, Any]:
    """Fetch the six layers, archive them, register the three atlas tables and load the adjacency
    statements through KGCORE's ``load_batch`` (worker engine). Parsing/computation happen before any
    registration, so a failure registers nothing (archived raw bytes stay archived)."""
    import polars as pl

    from reva.adapters.connectors.atlas_geo_common import archive_raw, load_country_qids
    from reva.atlas.tables import ATLAS_SCHEMA, register_atlas_table

    conn = NaturalEarth10mConnector()
    spec = conn.describe()
    raws = {it.key: await conn.fetch(it, http) for it in fetch_items()}
    origin = next(iter(raws.values())).origin
    shas = {k: archive_raw(engine, store, spec, r, actor="worker:atlas_a_geo") for k, r in raws.items()}
    nb = build(raws, shas)
    tables = {}
    notes = {
        DS_COUNTRY: "Natural Earth 1:10m Admin-0 map units (v{v}): computed WGS84 area, 1:10m coastline, land "
        "borders, landlocked, exclaves; de facto view; unmapped NE features kept (iso3 null)",
        DS_BORDERS: "Natural Earth 1:10m (v{v}): shared Admin-0 edge length per country pair (both directions), "
        "typed by NE boundary lines; disputed flagged",
        DS_DISPUTED: "Natural Earth 1:10m (v{v}) disputed/breakaway/claim areas: computed area, NE note verbatim",
    }
    for ds, rows in nb.rows.items():
        frame = pl.DataFrame(rows, schema=ATLAS_SCHEMA)
        used = sorted(set(frame["raw_sha256"].to_list()))
        tables[ds] = register_atlas_table(
            engine,
            store,
            source=ATLAS_SOURCE,
            dataset=ds,
            frame=frame,
            raw_sha256s=used,
            parser_version=PARSER_VERSION,
            origin=origin,
            coverage_note=notes[ds].format(v=nb.ne_version),
            rights=RIGHTS,
            actor="worker:atlas_a_geo",
        )
    kg: dict[str, Any] = {"loaded": False}
    if load_kg:
        q = dict(qids) if qids is not None else load_country_qids()
        if not q:
            kg = {"loaded": False, "blocked": "registry/entities/country_qids.json (ISO3 -> QID) not available"}
        else:
            from reva.adapters.connectors.atlas_geo_common import ensure_kg_property
            from reva.kg.model import load_batch

            ensure_kg_property(engine, "P47")
            sts, info = adjacency_statements(
                nb.computation,
                q,
                raw_sha256=shas["map_units"],
                source_revision=str(nb.ne_version),
                retrieved_at=raws["map_units"].retrieved_at,
            )
            rep = load_batch(
                engine,
                store,
                statements=sts,
                source_key=SOURCE_KEY,
                raw_sha256s=[shas["map_units"]],
                retrieved_at=raws["map_units"].retrieved_at,
                origin=origin,
                parameters={"connector": CONNECTOR_NAME, "ne_version": nb.ne_version},
            )
            kg = {"loaded": True, **info, "report": rep.as_dict()}
    return {
        "connector": CONNECTOR_NAME,
        "ne_version": nb.ne_version,
        "raw_sha256s": shas,
        "tables": tables,
        "diagnostics": nb.computation.diagnostics,
        "kg": kg,
    }
