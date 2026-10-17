"""NOAA GHCN-Monthly v4 weather stations (KG entities, located in country and ADM1) + selected monthly series.

Agent V4CLIMATE. Atlas source ``atlas.ghcnm`` (source key ``noaa.ghcnm``); ADM1 subdivisions from Natural Earth
(source key ``naturalearth.admin1_10m``).

Source and licence
------------------
NOAA NCEI Global Historical Climatology Network - Monthly, version 4 (``https://www.ncei.noaa.gov/pub/data/ghcn/v4/``):
``ghcnm.tavg.latest.qcf.tar.gz`` (one ``.inv`` station inventory and one ``.dat`` data file of monthly mean
temperature, QCF = "Quality Control, Adjusted, using the Pairwise Homogeneity Algorithm (PHA, Menne and Williams,
2009)"), ``readme.txt`` (format and flag definitions, archived; the definitions quoted below are checked in it at every
run) and ``ghcnm-countries.txt``. A US Government work (NOAA), public domain in the US; NCEI asks users to cite the
dataset. Natural Earth 1:10m Admin-1 (``ne_10m_admin_1_states_provinces.zip``, release 5.1.x): public domain.

What is built
-------------
* **KG, every station of the inventory** (~27,900, source key ``noaa.ghcnm``): entity ``ghcnm:<ID>`` of kind
  ``weather_station`` labelled with the GHCN station name (verbatim), coordinates (``P625``), elevation (``P2044``,
  when not the ``-999.0`` missing code), ``ghcnm:fips_country_code`` (the readme: "First two characters are FIPS
  country code") and ``P17`` country when that FIPS code maps to a crosswalk country (ATLASCORE ``fips`` scheme from
  GeoNames countryInfo) with a QID in ``registry/entities/country_qids.json``; ``ghcnm:valid_months`` (count of
  non-missing QCF values, a derived count) and ``ghcnm:first_year``/``ghcnm:last_year``.
* **ADM1** (``ghcnm:in_adm1`` -> ``iso3166_2:<code>``, family ``located_in``): a REVA point-in-polygon of the GHCN
  coordinates against the Natural Earth Admin-1 polygons; the subdivision is identified only by the ISO 3166-2 code
  Natural Earth publishes for the polygon (``iso_3166_2``) -- never by name. Codes not matching
  ``^[A-Z]{2}-[A-Z0-9]{1,3}$`` (Natural Earth's ``-99-X..`` / ``XX-X..`` placeholders) and codes Natural Earth uses for
  several polygons (ambiguous) are not used. A station whose FIPS-derived country differs from the subdivision's
  country gets **no** ADM1 link (reported: e.g. GHCN-Daily ``ACW...`` stations coded AC but located in Sweden); the
  statement records the basis (``derived: point_in_polygon``, Natural Earth file sha256, ``ne_adm1_code``).
  Entities ``iso3166_2:<code>`` of kind ``region_adm1`` (source key ``naturalearth.admin1_10m``) carry the Natural
  Earth name (en/fr), ``P17`` (only when the code's ISO 3166-1 prefix maps to a crosswalk country with a QID; the
  prefix *is* the country by the ISO 3166-2 definition), Natural Earth's ``adm1_code`` and its published
  ``wikidataid`` as an external-id string (identity bridges are V4LINK's job).
* **Atlas** ``ghcnm.tavg_qcf_monthly``: the monthly QCF TAVG series (°C, hundredths as published divided by 100,
  exact) of the **selected** stations only: per GHCN country code the ``PER_COUNTRY`` stations with the most valid
  monthly values (ties by id), plus the ``GLOBAL_LONGEST`` longest records worldwide. Selection is a size bound
  (documented, not a quality judgement); every station keeps its KG entity. ``provider_geo`` = station id, ``iso3``
  null (a station is not a country), the FIPS country in the flags; DMFLAG/QCFLAG/DSFLAG verbatim per value.

Missing: ``-9999`` values are not rows (never zero). Fixture runs use a *derived excerpt* (``REVA_EXCERPT.json``
member) of the tarball and of the Natural Earth ZIP; an excerpt is refused from a live acquisition.
"""

from __future__ import annotations

import io
import json
import re
import sys
import tarfile
import zipfile
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from reva.adapters.connectors import atlas_climate_common as cc
from reva.adapters.connectors import atlas_infra_common as ic
from reva.adapters.connectors.base import dec_str
from reva.atlas.sources import AtlasSourceSpec
from reva.kernel.errors import SourceResponseError

NAME = "atlas.ghcnm"
VERSION = "1.0.0"
SOURCE_KEY = "noaa.ghcnm"
NE_SOURCE_KEY = "naturalearth.admin1_10m"
ATLAS_SOURCE = "ghcnm"
DATASET = "tavg_qcf_monthly"
PARSER_VERSION = "atlas-ghcnm/1"
HOST = "www.ncei.noaa.gov"
NE_HOST = "naciscdn.org"
BASE = f"https://{HOST}/pub/data/ghcn/v4/"
URLS = {
    "readme": BASE + "readme.txt",
    "countries": BASE + "ghcnm-countries.txt",
    "tavg_qcf": BASE + "ghcnm.tavg.latest.qcf.tar.gz",
    "ne_admin1": f"https://{NE_HOST}/naturalearth/10m/cultural/ne_10m_admin_1_states_provinces.zip",
}
MAX_BYTES = 60_000_000
MIN_STATIONS = 20_000  # truncation guard on the live inventory (27,898 on 2026-10-16)
MIN_NE_FEATURES = 4_000
PER_COUNTRY = 3
GLOBAL_LONGEST = 50
EXCERPT_MEMBER = "REVA_EXCERPT.json"
MISSING = -9999
ISO_3166_2_RE = re.compile(r"^[A-Z]{2}-[A-Z0-9]{1,3}$")
README_QUOTES = {
    "fips": "First two characters are FIPS country code",
    "qcf": "QCF: Quality Control, Adjusted, using the Pairwise Homogeneity",
    "units": "Temperature values are in",
    "missing": "VALUE: monthly value (MISSING=-9999)",
    "elev_missing": "-999.0 = missing",
}
RIGHTS = {
    "licence": "US Government work (NOAA NCEI), public domain in the US; cite GHCN-M v4",
    "attribution": "Menne, M. J., C. N. Williams, B. E. Gleason, J. J. Rennie, and J. H. Lawrimore (2018): The Global "
    "Historical Climatology Network Monthly Temperature Dataset, Version 4. J. Climate; NOAA NCEI GHCN-M v4 QCF",
}
NE_RIGHTS = {
    "licence": "Public domain (Natural Earth)",
    "attribution": "Made with Natural Earth (naturalearthdata.com)",
}

KG_PREDICATES: dict[str, tuple[str, str, str, str]] = {
    "ghcnm:fips_country_code": (
        "string",
        "ghcnm_fips_country_code",
        "GHCN FIPS country code (station id prefix)",
        "code pays FIPS GHCN (préfixe de l'identifiant)",
    ),
    "ghcnm:in_adm1": (
        "item",
        "ghcnm_in_adm1",
        "located in first-level subdivision (point in polygon)",
        "situé dans la subdivision de premier niveau (point dans polygone)",
    ),
    "ghcnm:valid_months": (
        "quantity",
        "ghcnm_valid_months",
        "monthly TAVG values (QCF, non-missing)",
        "valeurs mensuelles TAVG (QCF, non manquantes)",
    ),
    "ghcnm:first_year": ("quantity", "ghcnm_first_year", "first year with a TAVG value", "première année avec TAVG"),
    "ghcnm:last_year": ("quantity", "ghcnm_last_year", "last year with a TAVG value", "dernière année avec TAVG"),
    "ghcnm:tavg_series": (
        "string",
        "ghcnm_tavg_series",
        "monthly TAVG series in the atlas",
        "série mensuelle TAVG dans l'atlas",
    ),
}
NE_PREDICATES: dict[str, tuple[str, str, str, str]] = {
    "naturalearth:adm1_code": (
        "string",
        "naturalearth_adm1_code",
        "Natural Earth adm1_code",
        "code adm1 Natural Earth",
    ),
    "naturalearth:wikidataid": (
        "external_id",
        "naturalearth_wikidataid",
        "Wikidata id published by Natural Earth",
        "identifiant Wikidata publié par Natural Earth",
    ),
    "naturalearth:type_en": (
        "string",
        "naturalearth_type_en",
        "subdivision type (Natural Earth)",
        "type de subdivision (Natural Earth)",
    ),
}
WIKIDATA_PIDS = ("P17", "P625", "P2044")


class GhcnmError(SourceResponseError):
    pass


def _err(msg: str, kind: str = "schema_drift") -> GhcnmError:
    return GhcnmError(msg, retryable=False, kind=kind)


def code_digest() -> str:
    return ic.code_digest(sys.modules[__name__], cc)


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=NAME,
        version=VERSION,
        source_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="NOAA NCEI GHCN-Monthly v4 (stations, QCF TAVG); Natural Earth 1:10m Admin-1 (ADM1 polygons)",
        licence=f"{RIGHTS['licence']}; Natural Earth: public domain",
        allowed_hosts=(HOST, NE_HOST),
        datasets=(DATASET,),
        dataset_ids=tuple(URLS.values()),
        cadence_seconds=90 * ic.DAY,
        planned_requests=len(URLS),
        max_response_bytes=MAX_BYTES,
        min_interval_seconds=2.0,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(),
        notes="all GHCN-M stations as KG entities (country by FIPS prefix, ADM1 by point-in-polygon); "
        "selected stations' monthly TAVG in the atlas",
    )


# --------------------------------------------------------------------------- parsing
@dataclass
class Station:
    id: str
    lat: str
    lon: str
    elev: str
    name: str
    line: int
    valid: int = 0
    first: int | None = None
    last: int | None = None


@dataclass
class Ghcn:
    version_dir: str
    stations: dict[str, Station]
    dat_name: str
    inv_name: str
    excerpt: dict[str, Any] | None = None
    dat_lines: dict[str, list[tuple[int, str]]] = field(default_factory=dict)  # selected stations only


def check_readme(body: bytes) -> dict[str, str]:
    text = re.sub(r"\s+", " ", body.decode("utf-8", "replace"))
    missing = [k for k, q in README_QUOTES.items() if re.sub(r"\s+", " ", q) not in text]
    if missing:
        raise _err(f"GHCN-M readme no longer states: {missing}")
    return {k: q for k, q in README_QUOTES.items()}


def parse_inv(text: str) -> dict[str, Station]:
    out: dict[str, Station] = {}
    for i, ln in enumerate(text.splitlines(), start=1):
        if not ln.strip():
            continue
        sid, lat, lon, elev, name = ln[0:11], ln[12:20].strip(), ln[21:30].strip(), ln[31:37].strip(), ln[38:68].strip()
        if not re.match(r"^[A-Z0-9-]{11}$", sid) or cc.decimal_text(lat) is None or cc.decimal_text(lon) is None:
            raise _err(f"inventory line {i}: unexpected layout {ln[:40]!r}")
        if sid in out:
            raise _err(f"inventory: duplicate station {sid}")
        out[sid] = Station(sid, cc.decimal_text(lat) or lat, cc.decimal_text(lon) or lon, elev, name, i)
    return out


def scan_dat(lines: Iterable[str], stations: Mapping[str, Station]) -> None:
    """Count non-missing values, first/last year per station (one pass)."""
    for i, ln in enumerate(lines, start=1):
        if not ln.strip():
            continue
        sid, year, elem = ln[0:11], ln[11:15], ln[15:19]
        st = stations.get(sid)
        if st is None or elem != "TAVG" or not year.isdigit():
            raise _err(f"data line {i}: unknown station or layout ({ln[:20]!r})")
        y = int(year)
        n = sum(1 for m in range(12) if int(ln[19 + 8 * m : 24 + 8 * m]) != MISSING)
        if n:
            st.valid += n
            st.first = y if st.first is None else min(st.first, y)
            st.last = y if st.last is None else max(st.last, y)


def select_stations(stations: Mapping[str, Station]) -> dict[str, list[str]]:
    """{station id: [reasons]}: top PER_COUNTRY per FIPS code + GLOBAL_LONGEST worldwide, by valid months."""
    by_cc: dict[str, list[Station]] = defaultdict(list)
    for s in stations.values():
        if s.valid:
            by_cc[s.id[:2]].append(s)
    out: dict[str, list[str]] = defaultdict(list)
    for code, lst in by_cc.items():
        for rank, s in enumerate(sorted(lst, key=lambda s: (-s.valid, s.id))[:PER_COUNTRY], start=1):
            out[s.id].append(f"country_top{PER_COUNTRY}:{code}:rank{rank}")
    for rank, s in enumerate(sorted((s for s in stations.values() if s.valid), key=lambda s: (-s.valid, s.id))[:GLOBAL_LONGEST], start=1):  # fmt: skip
        out[s.id].append(f"global_longest{GLOBAL_LONGEST}:rank{rank}")
    return dict(out)


def read_tarball(body: bytes, *, origin: str, min_stations: int = MIN_STATIONS) -> Ghcn:
    try:
        tf = tarfile.open(fileobj=io.BytesIO(body), mode="r:gz")
    except tarfile.TarError as e:
        raise _err(f"not a gzip tar: {e}") from e
    with tf:
        members = {m.name: m for m in tf.getmembers() if m.isfile()}
        excerpt = None
        exc = [n for n in members if n.endswith(EXCERPT_MEMBER)]
        if exc:
            if origin != "test_fixture":
                raise _err("a REVA derived excerpt was served by a live source", kind="integrity")
            excerpt = json.loads(tf.extractfile(members[exc[0]]).read())  # type: ignore[union-attr]
        inv = [n for n in members if n.endswith(".qcf.inv")]
        dat = [n for n in members if n.endswith(".qcf.dat")]
        if len(inv) != 1 or len(dat) != 1:
            raise _err(f"tarball members unexpected: {sorted(members)[:6]}")
        vdir = inv[0].split("/")[-2] if "/" in inv[0] else ""
        if not re.match(r"^ghcnm\.v4\.\d+\.\d+\.\d{8}$", vdir):
            raise _err(f"version directory {vdir!r} does not follow ghcnm.v4.x.y.YYYYMMDD")
        stations = parse_inv(tf.extractfile(members[inv[0]]).read().decode("latin-1"))  # type: ignore[union-attr]
        if excerpt is None and len(stations) < min_stations:
            raise _err(f"inventory has {len(stations)} stations (< {min_stations}): truncated?", kind="incomplete")
        raw = tf.extractfile(members[dat[0]]).read().decode("latin-1")  # type: ignore[union-attr]
    lines = raw.splitlines()
    scan_dat(lines, stations)
    g = Ghcn(vdir, stations, dat[0], inv[0], excerpt)
    sel = select_stations(stations)
    for i, ln in enumerate(lines, start=1):
        if ln[0:11] in sel:
            g.dat_lines.setdefault(ln[0:11], []).append((i, ln))
    return g


def series_rows(g: Ghcn, selected: Mapping[str, Sequence[str]], sha: str) -> cc.ValueRows:
    from reva.atlas.tables import country_iso3

    rb = cc.ValueRows(ATLAS_SOURCE, DATASET)
    base = {
        "licence": RIGHTS["licence"],
        "estimate_kind": "observation",
        "product": "GHCN-M v4 QCF (adjusted, Pairwise Homogeneity Algorithm)",
        "version": g.version_dir,
        "units_note": "hundredths of a degree Celsius as published, divided by 100 (exact)",
    }
    for sid in sorted(selected):
        st = g.stations[sid]
        fl0 = cc.canon(
            base,
            station_name=st.name,
            fips_country_code=sid[:2],
            country_iso3=country_iso3(sid[:2], "fips"),
            selection=list(selected[sid]),
        )
        for line_no, ln in g.dat_lines.get(sid, []):
            y = int(ln[11:15])
            for m in range(12):
                o = 19 + 8 * m
                v = int(ln[o : o + 5])
                if v == MISSING:
                    continue
                dm, qc, ds = ln[o + 5 : o + 6], ln[o + 6 : o + 7], ln[o + 7 : o + 8]
                fl = fl0
                if dm.strip() or qc.strip() or ds.strip():
                    d = json.loads(fl0)
                    d |= {"dmflag": dm, "qcflag": qc, "dsflag": ds}
                    fl = cc.canon(d)
                rb.add(
                    indicator="TAVG",
                    provider_geo=sid,
                    period=ic.month_span(y, m + 1),
                    value=dec_str(Decimal(v) / 100),
                    unit="°C",
                    raw_sha256=sha,
                    selector={"file": g.dat_name, "line": line_no, "value_column": f"VALUE{m + 1}"},
                    flags=fl,
                )
    return rb


# --------------------------------------------------------------------------- Natural Earth ADM1
@dataclass
class Adm1:
    code: str
    iso_a2: str
    adm1_code: str
    name_en: str
    name_fr: str
    type_en: str
    wikidataid: str
    index: int


def read_ne_admin1(body: bytes, *, origin: str) -> tuple[list[Adm1], list[Any], dict[str, Any]]:
    """Natural Earth Admin-1 records + shapely geometries (same order) + report."""
    import shapefile  # type: ignore[import-untyped]  # pyshp
    from shapely.geometry import shape  # type: ignore[import-untyped]

    try:
        z = zipfile.ZipFile(io.BytesIO(body))
    except zipfile.BadZipFile as e:
        raise _err(f"Natural Earth admin-1: not a ZIP ({e})") from e
    names = z.namelist()
    excerpt = EXCERPT_MEMBER in names
    if excerpt and origin != "test_fixture":
        raise _err("a REVA derived excerpt was served by a live source", kind="integrity")

    def member(ext: str) -> io.BytesIO:
        hits = [n for n in names if n.endswith(ext) and "admin_1" in n]
        if len(hits) != 1:
            raise _err(f"Natural Earth admin-1: {ext} member missing")
        return io.BytesIO(z.read(hits[0]))

    r = shapefile.Reader(shp=member(".shp"), dbf=member(".dbf"), shx=member(".shx"), encoding="utf-8")
    fields = {f[0] for f in r.fields[1:]}
    need = {"iso_3166_2", "iso_a2", "adm1_code", "name", "name_en", "name_fr", "type_en", "wikidataid"}
    if not need <= fields:
        raise _err(f"Natural Earth admin-1 lacks fields {sorted(need - fields)}")
    if not excerpt and len(r) < MIN_NE_FEATURES:
        raise _err(f"Natural Earth admin-1 has {len(r)} features (truncated?)", kind="incomplete")
    recs: list[Adm1] = []
    geoms: list[Any] = []
    for i, sr in enumerate(r.iterShapeRecords()):
        a = sr.record.as_dict()
        recs.append(
            Adm1(
                str(a["iso_3166_2"] or ""),
                str(a["iso_a2"] or ""),
                str(a["adm1_code"] or ""),
                str(a["name_en"] or a["name"] or ""),
                str(a["name_fr"] or ""),
                str(a["type_en"] or ""),
                str(a["wikidataid"] or ""),
                i,
            )
        )
        geoms.append(shape(sr.shape.__geo_interface__))
    counts = Counter(x.code for x in recs)
    report = {
        "features": len(recs),
        "excerpt": excerpt,
        "codes_invalid": sorted({x.code for x in recs if not ISO_3166_2_RE.match(x.code)})[:50],
        "codes_ambiguous": sorted(c for c, n in counts.items() if n > 1 and ISO_3166_2_RE.match(c)),
    }
    return recs, geoms, report


def usable_codes(recs: Sequence[Adm1]) -> set[str]:
    counts = Counter(x.code for x in recs)
    return {c for c, n in counts.items() if n == 1 and ISO_3166_2_RE.match(c)}


def locate(stations: Mapping[str, Station], recs: Sequence[Adm1], geoms: Sequence[Any]) -> dict[str, Adm1]:
    """Station id -> the unique usable Natural Earth subdivision whose polygon covers its coordinates."""
    from shapely import STRtree  # type: ignore[import-untyped]
    from shapely.geometry import Point

    ok = usable_codes(recs)
    tree = STRtree(list(geoms))
    out: dict[str, Adm1] = {}
    for sid, st in stations.items():
        p = Point(float(st.lon), float(st.lat))
        hits = [int(i) for i in tree.query(p, predicate="covered_by")]
        if len(hits) == 1 and recs[hits[0]].code in ok:  # ambiguous / placeholder codes and overlaps: not used
            out[sid] = recs[hits[0]]
    return out


# --------------------------------------------------------------------------- KG
@dataclass
class Fetched:
    key: str
    body: bytes
    sha: str
    url: str
    retrieved_at: datetime


def kg_items(
    g: Ghcn,
    selected: Mapping[str, Sequence[str]],
    located: Mapping[str, Adm1],
    recs: Sequence[Adm1],
    files: Mapping[str, Fetched],
    country_qids: Mapping[str, str],
) -> tuple[tuple[list[Any], list[Any]], tuple[list[Any], list[Any]], dict[str, Any]]:
    """((station entities, station statements), (ADM1 entities, ADM1 statements), report)."""
    from reva.atlas.tables import country_iso3
    from reva.kg.model import KgEntity, KgStatement

    tar, ne = files["tavg_qcf"], files["ne_admin1"]
    rev = g.version_dir
    ref = [cc.kg_ref(file=g.inv_name, url=tar.url, raw_sha256=tar.sha, retrieved_at=tar.retrieved_at,
                     licence=RIGHTS["licence"])]  # fmt: skip
    ents: list[Any] = []
    sts: list[Any] = []
    rep: Counter[str] = Counter()
    mismatch: list[str] = []

    def st(sid: str, pid: str, *, obj: str | None = None, value: dict[str, Any] | None = None,
           quals: dict[str, Any] | None = None, refs: list[dict[str, Any]] | None = None, raw: str = tar.sha) -> None:  # fmt: skip
        sts.append(KgStatement(subject_qid=f"ghcnm:{sid}", property_pid=pid, object_qid=obj, value=value,
                               qualifiers=quals or {}, references=refs or ref, source_key=SOURCE_KEY,
                               source_revision=rev, statement_id=f"ghcnm:{sid}:{pid.split(':')[-1]}", raw_sha256=raw))  # fmt: skip
        rep[f"statements_{pid}"] += 1

    for sid, s in sorted(g.stations.items()):
        ents.append(KgEntity(qid=f"ghcnm:{sid}", kind="weather_station", labels={"en": s.name or sid},
                             descriptions={"en": f"GHCN-Monthly v4 station {sid}"}, aliases={"en": [sid]},
                             source_revision=rev))  # fmt: skip
        q = {"inventory_line": s.line}
        st(sid, "P625", value={"type": "globe_coordinate", "lat": s.lat, "lon": s.lon, "globe": "earth"}, quals=q)
        if s.elev and cc.decimal_text(s.elev) not in (None, "-999"):
            st(sid, "P2044", value={"type": "quantity", "amount": cc.decimal_text(s.elev), "unit": "metre"}, quals=q)
        fips = sid[:2]
        st(sid, "ghcnm:fips_country_code", value={"type": "string", "value": fips},
           quals={**q, "basis": README_QUOTES["fips"]})  # fmt: skip
        iso3 = country_iso3(fips, "fips")
        cq = country_qids.get(iso3 or "")
        adm = located.get(sid)
        adm_iso3 = country_iso3(adm.code[:2], "iso2") if adm else None
        if cq:
            quals = {**q, "basis": "GHCN station id FIPS prefix -> ISO3 (fips scheme, GeoNames countryInfo)",
                     "fips": fips, "country_iso3": iso3}  # fmt: skip
            if adm is not None and adm_iso3 and adm_iso3 != iso3:
                quals["coordinates_fall_in"] = f"{adm.code} (Natural Earth admin-1 polygon)"
            st(sid, "P17", obj=cq, quals=quals)
        else:
            rep["country_unmapped_or_no_qid"] += 1
        if adm is not None:
            if iso3 and adm_iso3 and adm_iso3 != iso3:
                rep["adm1_country_mismatch"] += 1
                mismatch.append(f"{sid}:{fips}->{iso3} in {adm.code}")
            else:
                st(sid, "ghcnm:in_adm1", obj=f"iso3166_2:{adm.code}",
                   quals={**q, "derived": "point_in_polygon", "method": "shapely STRtree point covered_by polygon",
                          "ne_adm1_code": adm.adm1_code, "ne_feature_index": adm.index,
                          "ne_raw_sha256": ne.sha},
                   refs=[*ref, cc.kg_ref(file="ne_10m_admin_1_states_provinces.shp", url=ne.url, raw_sha256=ne.sha,
                                         retrieved_at=ne.retrieved_at, licence=NE_RIGHTS["licence"])])  # fmt: skip
        else:
            rep["no_adm1"] += 1
        if s.valid:
            for pid, v in (("ghcnm:valid_months", s.valid), ("ghcnm:first_year", s.first), ("ghcnm:last_year", s.last)):
                st(sid, pid, value={"type": "quantity", "amount": str(v), "unit": None},
                   quals={"derived": "count/min/max over the QCF .dat file (values != -9999)", "file": g.dat_name})  # fmt: skip
        if sid in selected:
            st(sid, "ghcnm:tavg_series", value={"type": "string", "value": f"{ATLAS_SOURCE}.{DATASET}.TAVG @ provider_geo={sid}"},
               quals={"selection": list(selected[sid])})  # fmt: skip
    # ADM1 entities (Natural Earth), only usable codes
    ok = usable_codes(recs)
    a_ents: list[Any] = []
    a_sts: list[Any] = []
    nref = [cc.kg_ref(file="ne_10m_admin_1_states_provinces.dbf", url=ne.url, raw_sha256=ne.sha,
                      retrieved_at=ne.retrieved_at, licence=NE_RIGHTS["licence"])]  # fmt: skip
    for a in recs:
        if a.code not in ok:
            continue
        qid = f"iso3166_2:{a.code}"
        labels = {"en": a.name_en or a.code} | ({"fr": a.name_fr} if a.name_fr else {})
        a_ents.append(KgEntity(qid=qid, kind="region_adm1", labels=labels, aliases={"en": [a.code]},
                               descriptions={"en": f"ISO 3166-2 subdivision {a.code} (Natural Earth admin-1 polygon)"},
                               source_revision=ne.sha[:16]))  # fmt: skip

        def ast(pid: str, *, obj: str | None = None, value: dict[str, Any] | None = None,
                quals: dict[str, Any] | None = None, a: Adm1 = a, qid: str = qid) -> None:  # fmt: skip
            a_sts.append(KgStatement(subject_qid=qid, property_pid=pid, object_qid=obj, value=value,
                                     qualifiers={"ne_feature_index": a.index, **(quals or {})}, references=nref,
                                     source_key=NE_SOURCE_KEY, source_revision=ne.sha[:16],
                                     statement_id=f"ne_admin1:{a.code}:{pid.split(':')[-1]}", raw_sha256=ne.sha))  # fmt: skip

        iso3 = country_iso3(a.code[:2], "iso2")
        cq = country_qids.get(iso3 or "")
        if cq:
            pq: dict[str, Any] = {
                "basis": "ISO 3166-2 code prefix is the ISO 3166-1 alpha-2 country code",
                "country_iso3": iso3,
            }
            if a.iso_a2 != a.code[:2]:
                pq["ne_iso_a2"] = a.iso_a2  # Natural Earth's de facto view differs (e.g. UA-43 drawn under RU)
            ast("P17", obj=cq, quals=pq)
        ast("naturalearth:adm1_code", value={"type": "string", "value": a.adm1_code})
        if a.type_en:
            ast("naturalearth:type_en", value={"type": "string", "value": a.type_en})
        if re.match(r"^Q[1-9][0-9]*$", a.wikidataid):
            ast("naturalearth:wikidataid", value={"type": "external_id", "value": a.wikidataid})
    report = {**dict(rep), "adm1_country_mismatch_sample": mismatch[:20], "adm1_entities": len(a_ents)}
    return (ents, sts), (a_ents, a_sts), report


# --------------------------------------------------------------------------- ingest
async def atlas_ingest(run: Any) -> dict[str, Any]:
    from reva.adapters.connectors.atlas_geo_common import load_country_qids

    files: dict[str, Fetched] = {}
    for key, url in URLS.items():
        media = ("application/gzip", "application/x-gzip", "application/octet-stream") if key == "tavg_qcf" else (
            ("application/zip", "application/octet-stream") if key == "ne_admin1" else ("text/plain",))  # fmt: skip
        got = await ic.fetch_archived(run, url, name=key, key=key, media_types=media, max_bytes=MAX_BYTES)
        files[key] = Fetched(key, got.body, got.sha256, url, got.retrieved_at)
    quotes = check_readme(files["readme"].body)
    g = read_tarball(files["tavg_qcf"].body, origin=run.origin)
    selected = select_stations(g.stations)
    rb = series_rows(g, selected, files["tavg_qcf"].sha)
    frame = rb.frame()
    out = run.register(
        dataset=DATASET,
        frame=frame,
        raw_sha256s=[files["tavg_qcf"].sha],
        geo_scheme="iso3",
        rights={**RIGHTS, "source_key": SOURCE_KEY},
        coverage_note=f"GHCN-M {g.version_dir} QCF TAVG of {len(selected)} selected stations (top {PER_COUNTRY} per "
        f"GHCN country code by valid months + {GLOBAL_LONGEST} longest records); provider_geo = station id (not a "
        "country, iso3 null by design); -9999 never a row" + ("; DERIVED EXCERPT (test fixture)" if g.excerpt else ""),
    )
    recs, geoms, ne_report = read_ne_admin1(files["ne_admin1"].body, origin=run.origin)
    located = locate(g.stations, recs, geoms)
    kg: dict[str, Any] = {"loaded": False}
    if run.engine is not None:
        (ents, sts), (a_ents, a_sts), kg_report = kg_items(g, selected, located, recs, files, load_country_qids())
        cc.register_props(run.engine, {**KG_PREDICATES, **NE_PREDICATES}, WIKIDATA_PIDS)
        params = {"connector": NAME, "parser": PARSER_VERSION, "ghcnm_version": g.version_dir}
        ne_load = cc.load_kg(run.engine, run.store, entities=a_ents, statements=a_sts, source_key=NE_SOURCE_KEY,
                             raw_sha256s=[files["ne_admin1"].sha], retrieved_at=files["ne_admin1"].retrieved_at,
                             origin=run.origin, parameters=params)  # fmt: skip
        st_load = cc.load_kg(run.engine, run.store, entities=ents, statements=sts, source_key=SOURCE_KEY,
                             raw_sha256s=[files["tavg_qcf"].sha, files["ne_admin1"].sha],
                             retrieved_at=files["tavg_qcf"].retrieved_at, origin=run.origin, parameters=params)  # fmt: skip
        kg = {"loaded": True, "stations": len(ents), "station_statements": len(sts), "adm1_entities": len(a_ents),
              "adm1_statements": len(a_sts), "report": kg_report, "load_stations": st_load, "load_adm1": ne_load}  # fmt: skip
    return {
        "ok": True,
        "version": g.version_dir,
        "stations": len(g.stations),
        "stations_with_values": sum(1 for s in g.stations.values() if s.valid),
        "selected": len(selected),
        "located_adm1": len(located),
        "readme_quotes": quotes,
        "natural_earth": ne_report,
        "tables": [
            {
                "table": out["dataset_key"].removeprefix("atlas."),
                "rows": out["row_count"],
                "created": out["created"],
                "artifact_sha256": out["artifact_sha256"],
            }
        ],
        "kg": kg,
    }
