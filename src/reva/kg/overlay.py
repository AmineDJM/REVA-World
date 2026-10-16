"""Territorial overlay: which historical polity polygon covered how much of a present-day country (V3, KGPOLITY).

This module is pure computation (no database, no network). It intersects sourced polygons of historical polities
(Cliopatria rows, :mod:`reva.adapters.connectors.atlas_cliopatria`) with present-day countries (Natural Earth 1:10m
Admin-0 *map units*, grouped into crosswalk entities with A-GEO's rules,
:func:`reva.adapters.connectors.atlas_naturalearth10m.assign_entity`) and returns, per polity row and country,
the overlap area and the share of the country's area it represents. :func:`spans` folds consecutive rows of one
polity into runs ("polity P covered between s_min and s_max of country C's area from year a to year b") that the
connector loads as ``geo:covered_territory_of`` statements.

Method (:data:`METHOD`)
-----------------------
* Geometries are read as given (lon/lat, EPSG:4326 / CRS84) and repaired with ``shapely.make_valid`` when GEOS
  reports them invalid (the repair is counted, never silent).
* Intersection: GEOS (shapely 2) planar intersection in lon/lat. Both sources draw edges as straight lines in
  lon/lat, so the planar intersection is the intersection of the two regions as the sources define them.
* Areas: WGS 84 ellipsoidal area of the result's rings with the authalic-latitude transform
  (:func:`reva.adapters.connectors.atlas_geodesy.ring_signed_area_m2`, the same code A-GEO uses for country
  areas). The country area is computed the same way, so shares are ratios of like quantities.
* Identical geometries (by digest) are intersected once (Cliopatria repeats polygons across rows).
* A share is ``overlap / country area`` (0..1, rounded to 4 decimals; values above 1 from rounding are clamped).
  A run keeps rows whose share is at least ``min_share`` (default 1 %): smaller overlaps are mostly the
  generalisation mismatch between two independently drawn border sets (coastlines, rivers) and are counted in
  the diagnostics, not stored.

Honesty: the result is *derived* (computed by REVA from two sourced geometries). Cliopatria's borders are
approximate ("these maps reflect only one version of the territory held by past polities", Cliopatria README),
its years are sampling steps, and Natural Earth's default Admin-0 layer is a de facto view of today's borders.
A share says how much of today's area lies inside the polygon the source drew; it says nothing about
sovereignty, effective control, population or legitimacy, and nothing causal.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import IO, Any

import numpy as np
import shapely  # type: ignore[import-untyped]
from shapely.geometry import MultiPolygon, Polygon, shape  # type: ignore[import-untyped]

from reva.adapters.connectors.atlas_geodesy import ring_signed_area_m2

OVERLAY_VERSION = "reva.kg.overlay/1"
METHOD = (
    "overlay/1: GEOS (shapely 2.1) planar intersection in lon/lat (EPSG:4326) of the Cliopatria row polygon and "
    "the Natural Earth 1:10m Admin-0 map units of the country (make_valid on invalid input); areas = WGS 84 "
    "ellipsoidal ring areas via the authalic latitude (reva.adapters.connectors.atlas_geodesy); share = overlap "
    "area / country area"
)
DEFAULT_MIN_SHARE = 0.01
MAX_PIECE_VERTICES = 400
TILE_DEG = 4.0  # spatial index cell size (degrees); pieces only speed the overlay up, results do not depend on it
SHARE_DECIMALS = 4
MAX_FEATURE_BYTES = 32_000_000  # one GeoJSON feature (the largest Cliopatria v0.2.1 feature is ~0.3 MB)
_READ_CHUNK = 1 << 20


class OverlayError(ValueError):
    """Input the overlay cannot use (malformed GeoJSON, oversize feature, empty country layer)."""


# ---------------------------------------------------------------------------------------------- digests, areas
def canonical_json_sha256(obj: Any) -> str:
    """sha256 of the canonical JSON text (sorted keys, compact) of a parsed GeoJSON geometry."""
    text = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def wkb_sha256(geom: Any) -> str:
    """sha256 of the normalised little-endian 2D WKB of a shapely geometry (a stable geometry digest)."""
    return hashlib.sha256(shapely.to_wkb(shapely.normalize(geom), byte_order=1, output_dimension=2)).hexdigest()


def _ring_area_m2(coords: Any) -> float:
    a = np.asarray(coords, dtype=float)
    if a.shape[0] < 4:
        return 0.0
    return abs(ring_signed_area_m2(a[:, 0], a[:, 1]))


def ellipsoidal_area_km2(geom: Any) -> float:
    """WGS 84 ellipsoidal area (km²) of the polygonal parts of a shapely geometry (lines/points count 0)."""
    if geom is None or geom.is_empty:
        return 0.0
    t = geom.geom_type
    if t == "Polygon":
        a = _ring_area_m2(geom.exterior.coords) - sum(_ring_area_m2(r.coords) for r in geom.interiors)
        return max(a, 0.0) / 1e6
    if t in ("MultiPolygon", "GeometryCollection"):
        return sum(ellipsoidal_area_km2(g) for g in geom.geoms)
    return 0.0


def share_text(x: float) -> str:
    """A share as a strict decimal string with :data:`SHARE_DECIMALS` places, clamped to 0..1."""
    if not math.isfinite(x):
        raise OverlayError("non-finite share")
    return f"{min(max(x, 0.0), 1.0):.{SHARE_DECIMALS}f}"


def km2_text(x: float) -> str:
    return f"{max(x, 0.0):.1f}"


# ---------------------------------------------------------------------------------------------- countries
@dataclass(frozen=True)
class Territory:
    """A present-day country (crosswalk entity) or a Natural Earth unit outside the crosswalk."""

    key: str  # crosswalk key (ISO3, XKX, ...) or "naturalearth:<ADM0_A3>"
    iso3: str | None
    label: str
    geometry: Any = field(repr=False, compare=False)
    area_km2: float
    geometry_sha256: str
    units: tuple[str, ...]  # Natural Earth map units (GU_A3) grouped into this territory
    rules: tuple[str, ...]  # assignment rules used (ISO_A3, ADM0_A3, ISO_A3_EH, reviewed_alias, unmapped)


@dataclass
class CountryLayer:
    """Territories plus a tiled spatial index: each territory is cut into ``tile_deg`` lon/lat cells so that an
    intersection only touches small pieces (cells fully inside a polity are added by area, no intersection)."""

    territories: list[Territory]
    ne_version: str | None
    unmapped_units: list[str]
    tile_deg: float = TILE_DEG
    _pieces: Any = field(default=None, repr=False)
    _piece_terr: Any = field(default=None, repr=False)
    _piece_area: Any = field(default=None, repr=False)
    _tree: Any = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not self.territories:
            raise OverlayError("empty country layer")
        pieces: list[Any] = []
        owner: list[int] = []
        for ti, t in enumerate(self.territories):
            for piece in _tiles(t.geometry, self.tile_deg):
                pieces.append(piece)
                owner.append(ti)
        self._pieces = np.array(pieces, dtype=object)
        self._piece_terr = np.array(owner, dtype=np.int64)
        self._piece_area = np.array([ellipsoidal_area_km2(p) for p in pieces], dtype=float)
        shapely.prepare(self._pieces)
        self._tree = shapely.STRtree(self._pieces)

    @property
    def piece_count(self) -> int:
        return len(self._pieces)

    def by_key(self) -> dict[str, Territory]:
        return {t.key: t for t in self.territories}

    def overlaps_km2(self, geom: Any) -> dict[int, float]:
        """Territory index -> overlap area (km²) with ``geom`` (a valid polygonal shapely geometry)."""
        idx = self._tree.query(geom)
        if len(idx) == 0:
            return {}
        idx = np.sort(idx)
        pieces = self._pieces[idx]
        shapely.prepare(geom)
        inside = shapely.contains_properly(geom, pieces)
        areas = np.where(inside, self._piece_area[idx], 0.0)
        part = ~inside
        if part.any():
            inter = shapely.intersection(geom, pieces[part])
            areas[part] = [ellipsoidal_area_km2(g) for g in inter]
        out: dict[int, float] = defaultdict(float)
        for ti, a in zip(self._piece_terr[idx], areas, strict=True):
            if a > 0:
                out[int(ti)] += float(a)
        return dict(out)


def _polys(g: Any) -> list[Any]:
    return [p for p in getattr(g, "geoms", [g]) if p.geom_type == "Polygon" and not p.is_empty]


def _tiles(geom: Any, deg: float, max_vertices: int = MAX_PIECE_VERTICES) -> list[Any]:
    """``geom`` cut into pieces by recursive halving of the bounding box along its longer side until a piece has
    at most ``max_vertices`` vertices and spans at most ``deg`` degrees (each cut works on an already small
    piece, so building the index stays cheap for large countries)."""
    out: list[Any] = []
    stack = _polys(geom)
    while stack:
        g = stack.pop()
        minx, miny, maxx, maxy = g.bounds
        w, h = maxx - minx, maxy - miny
        if shapely.get_num_coordinates(g) <= max_vertices and max(w, h) <= deg:
            out.append(g)
            continue
        if w >= h:
            mid = (minx + maxx) / 2
            halves = (shapely.box(minx - 1, miny - 1, mid, maxy + 1), shapely.box(mid, miny - 1, maxx + 1, maxy + 1))
        else:
            mid = (miny + maxy) / 2
            halves = (shapely.box(minx - 1, miny - 1, maxx + 1, mid), shapely.box(minx - 1, mid, maxx + 1, maxy + 1))
        for half in halves:
            stack.extend(_polys(shapely.intersection(g, half)))
    return out


def _valid(geom: Any, counters: Counter[str] | None = None, what: str = "geometry") -> Any:
    if geom.is_valid:
        return geom
    if counters is not None:
        counters[f"{what}_repaired"] += 1
    fixed = shapely.make_valid(geom)
    polys = [g for g in getattr(fixed, "geoms", [fixed]) if g.geom_type in ("Polygon", "MultiPolygon")]
    if not polys:
        return Polygon()
    return shapely.union_all(polys) if len(polys) > 1 else polys[0]


def country_layer_from_ne(body: bytes, *, keys: Iterable[str] | None = None) -> CountryLayer:
    """Natural Earth ``ne_10m_admin_0_map_units`` zip -> one territory per crosswalk entity (A-GEO's rules:
    ISO_A3 > ADM0_A3 > ISO_A3_EH > reviewed alias KOS->XKX); units outside the crosswalk stay separate
    territories ``naturalearth:<ADM0_A3>`` (Taiwan, Somaliland, N. Cyprus, Kashmir parts...), never merged into
    a claimant."""
    from reva.adapters.connectors.atlas_naturalearth10m import (
        _crosswalk_keys,
        assign_entity,
        organise_polygons,
        read_layer,
    )

    layer = read_layer(body, "ne_10m_admin_0_map_units")
    kset = frozenset(keys) if keys is not None else _crosswalk_keys()
    groups: dict[str, list[Any]] = defaultdict(list)
    meta: dict[str, dict[str, Any]] = {}
    unmapped: list[str] = []
    pid = 0
    for f in layer.features:
        key, rule = assign_entity(f.rec, kset)
        if key is None:
            adm = str(f.rec.get("ADM0_A3") or f.rec.get("GU_A3") or f"unit{f.idx}")
            key = f"naturalearth:{adm}"
            unmapped.append(adm)
        polys = organise_polygons(f, pid)
        pid += len(polys)
        groups[key].extend(Polygon(p.outer, p.holes) for p in polys)
        m = meta.setdefault(key, {"units": [], "rules": set(), "labels": []})
        m["units"].append(str(f.rec.get("GU_A3") or f.rec.get("ADM0_A3") or ""))
        m["rules"].add(rule)
        m["labels"].append(str(f.rec.get("NAME_EN") or f.rec.get("NAME") or f.rec.get("ADMIN") or key))
    terrs = []
    for key in sorted(groups):
        ps = [p if p.is_valid else _valid(p) for p in groups[key]]
        geom = _valid(MultiPolygon([q for p in ps for q in getattr(p, "geoms", [p]) if not q.is_empty]))
        m = meta[key]
        iso3 = None if key.startswith("naturalearth:") else key
        label = m["labels"][0] if len(set(m["labels"])) == 1 else (key if iso3 else m["labels"][0])
        terrs.append(
            Territory(
                key=key,
                iso3=iso3,
                label=label,
                geometry=geom,
                area_km2=ellipsoidal_area_km2(geom),
                geometry_sha256=wkb_sha256(geom),
                units=tuple(sorted(set(m["units"]))),
                rules=tuple(sorted(m["rules"])),
            )
        )
    return CountryLayer(terrs, layer.version, sorted(set(unmapped)))


def country_layer_from_geometries(
    items: Iterable[tuple[str, str | None, str, Any]], version: str = "test"
) -> CountryLayer:
    """A layer from ``(key, iso3, label, shapely geometry)`` tuples (tests, synthetic worlds)."""
    terrs = []
    for key, iso3, label, g in items:
        g = _valid(g)
        terrs.append(Territory(key, iso3, label, g, ellipsoidal_area_km2(g), wkb_sha256(g), (key,), ("given",)))
    return CountryLayer(terrs, version, [])


# ---------------------------------------------------------------------------------------------- GeoJSON stream
def iter_geojson_features(stream: IO[bytes], *, max_feature_bytes: int = MAX_FEATURE_BYTES) -> Iterator[dict[str, Any]]:
    """Yield the features of a GeoJSON FeatureCollection one at a time with bounded memory.

    The ``"features": [`` array is located, then each element is decoded with ``JSONDecoder.raw_decode`` from a
    sliding buffer (independent of line breaks). A feature larger than ``max_feature_bytes`` is refused."""
    dec = json.JSONDecoder()
    text_stream = io.TextIOWrapper(stream, encoding="utf-8")
    buf = ""
    eof = False

    def fill() -> bool:
        nonlocal buf, eof
        if eof:
            return False
        chunk = text_stream.read(_READ_CHUNK)
        if not chunk:
            eof = True
            return False
        buf += chunk
        return True

    # locate the features array
    while True:
        i = buf.find('"features"')
        if i >= 0:
            j = buf.find("[", i)
            if j >= 0:
                buf = buf[j + 1 :]
                break
        if len(buf) > max_feature_bytes:
            raise OverlayError("GeoJSON: no features array in the leading bytes")
        if not fill():
            raise OverlayError("GeoJSON: no features array")
    while True:
        k = 0
        while True:
            while k < len(buf) and buf[k] in " \t\r\n,":
                k += 1
            if k < len(buf) or not fill():
                break
        buf = buf[k:]
        if not buf:
            raise OverlayError("GeoJSON: truncated features array")
        if buf[0] == "]":
            return
        while True:
            try:
                obj, end = dec.raw_decode(buf)
                break
            except json.JSONDecodeError as e:
                if len(buf) > max_feature_bytes:
                    raise OverlayError("GeoJSON feature larger than the bound") from e
                if not fill():
                    raise OverlayError(f"GeoJSON: malformed feature ({e.msg})") from e
        if end > max_feature_bytes:
            raise OverlayError("GeoJSON feature larger than the bound")
        if not isinstance(obj, dict) or obj.get("type") != "Feature":
            raise OverlayError("GeoJSON: features array holds a non-Feature")
        yield obj
        buf = buf[end:]


# ---------------------------------------------------------------------------------------------- overlay
@dataclass(frozen=True)
class Hit:
    """The overlap of one polity row geometry with one territory."""

    territory: str
    overlap_km2: float
    share: float  # overlap / territory area


@dataclass
class Overlayer:
    """Intersects polity geometries with a :class:`CountryLayer` (identical geometries are computed once)."""

    layer: CountryLayer
    counters: Counter[str] = field(default_factory=Counter)
    _cache: dict[str, tuple[float, list[Hit]]] = field(default_factory=dict, repr=False)
    errors: list[dict[str, Any]] = field(default_factory=list)

    def overlay(self, geometry: Mapping[str, Any], geometry_sha256: str | None = None) -> tuple[float, list[Hit]]:
        """``(polity area km², hits)`` for a GeoJSON geometry object (every overlap > 0, unfiltered)."""
        digest = geometry_sha256 or canonical_json_sha256(geometry)
        got = self._cache.get(digest)
        if got is not None:
            self.counters["cache_hits"] += 1
            return got
        geom = _valid(shape(geometry), self.counters, "polity_geometry")
        area = ellipsoidal_area_km2(geom)
        hits: list[Hit] = []
        terrs = self.layer.territories
        try:
            got_km2 = self.layer.overlaps_km2(geom)
        except shapely.errors.GEOSException:
            try:
                got_km2 = self.layer.overlaps_km2(shapely.set_precision(geom, 1e-7))
                self.counters["intersection_snapped"] += 1
            except shapely.errors.GEOSException as e:
                self.counters["intersection_failed"] += 1
                if len(self.errors) < 200:
                    self.errors.append({"geometry_sha256": digest, "error": str(e)[:200]})
                got_km2 = {}
        for j in sorted(got_km2):
            t = terrs[j]
            if t.area_km2 > 0 and got_km2[j] > 0:
                hits.append(Hit(t.key, got_km2[j], got_km2[j] / t.area_km2))
        self.counters["geometries_intersected"] += 1
        self._cache[digest] = (area, hits)
        if len(self._cache) > 4096:  # bounded: repeated polygons are consecutive rows of one polity
            self._cache.pop(next(iter(self._cache)))
        return area, hits


# ---------------------------------------------------------------------------------------------- runs
@dataclass(frozen=True)
class RowRef:
    """One provider row of a polity (what :func:`spans` needs)."""

    row: int  # ordinal of the feature in the file (0-based)
    polity: str  # the provider's entity name
    from_year: int  # provider's year integers, as written
    to_year: int
    geometry_sha256: str


@dataclass
class Span:
    polity: str
    territory: str
    rows: list[tuple[RowRef, Hit]]

    @property
    def from_year(self) -> int:
        return self.rows[0][0].from_year

    @property
    def to_year(self) -> int:
        return self.rows[-1][0].to_year

    @property
    def shares(self) -> list[float]:
        return [h.share for _, h in self.rows]

    def summary(self) -> dict[str, Any]:
        s = self.shares
        return {
            "max": share_text(max(s)),
            "min": share_text(min(s)),
            "first": share_text(s[0]),
            "last": share_text(s[-1]),
        }


def contiguous(a: RowRef, b: RowRef) -> bool:
    """``b`` directly follows ``a`` in time (provider years; the provider writes year 0 between -1 and 1)."""
    return b.from_year <= a.to_year + 1


def spans(
    rows: Iterable[RowRef],
    hits: Mapping[int, Sequence[Hit]],
    *,
    min_share: float = DEFAULT_MIN_SHARE,
) -> tuple[list[Span], Counter[str]]:
    """Fold row hits into runs per (polity, territory).

    For each polity, its rows are ordered by years; a run is a maximal sequence of consecutive rows of that
    polity, contiguous in time, each covering at least ``min_share`` of the territory. A row of the polity that
    does not reach ``min_share`` (or a gap in the polity's own rows) ends the run."""
    by_polity: dict[str, list[RowRef]] = defaultdict(list)
    for r in rows:
        by_polity[r.polity].append(r)
    stats: Counter[str] = Counter()
    out: list[Span] = []
    for polity in sorted(by_polity):
        seq = sorted(by_polity[polity], key=lambda r: (r.from_year, r.to_year, r.row))
        open_runs: dict[str, Span] = {}
        prev: RowRef | None = None
        for r in seq:
            here = {}
            for h in hits.get(r.row, ()):
                if h.share >= min_share:
                    here[h.territory] = h
                else:
                    stats["below_min_share"] += 1
            for terr in list(open_runs):
                if terr not in here or prev is None or not contiguous(prev, r):
                    out.append(open_runs.pop(terr))
            for terr, h in sorted(here.items()):
                if terr in open_runs:
                    open_runs[terr].rows.append((r, h))
                else:
                    open_runs[terr] = Span(polity, terr, [(r, h)])
            prev = r
        out.extend(open_runs[t] for t in sorted(open_runs))
    out.sort(key=lambda s: (s.polity, s.territory, s.from_year))
    stats["spans"] = len(out)
    return out, stats
