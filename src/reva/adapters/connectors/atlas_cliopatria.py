"""Cliopatria polity borders -> KG polity entities and the territorial overlay with present-day countries
(V3, agent KGPOLITY).

Source
------
Cliopatria (Seshat Global History Databank), "a comprehensive open-source geospatial dataset of worldwide polities
from 3400BCE to 2024CE" (README), one GeoJSON FeatureCollection distributed as ``cliopatria.geojson.zip`` in
https://github.com/Seshat-Global-History-Databank/cliopatria (releases mirrored on Zenodo record 13363121).
Licence: CC BY 4.0 (``LICENSE.md``; fetched and checked on every run -- any other text blocks the run). Paper:
Bennett et al. (2025), Scientific Data, doi:10.1038/s41597-025-04516-9. The release is pinned (:data:`TAG`);
bump the tag together with :data:`VERSION`.

Each row (feature) has ``Name`` (the entity key), ``FromYear``/``ToYear`` (integers, "negative for BCE", both
inclusive), ``Area`` (km², EPSG:6933), ``Type`` (``POLITY`` or ``RELATION``: alliances, personal unions,
allegiances, groupings), ``Wikipedia`` (page phrase), ``Wikidata`` ("associated Wikidata id for the entity in
those years"), ``SeshatID``, ``Components`` / ``MemberOf`` (``;``-separated entity names) and a (Multi)Polygon
in CRS84. Provider caveat (README): "these maps reflect only one version of the territory held by past polities.
Border uncertainties, as well as differing opinions on the names, territorial changes, and durations of polities,
are common challenges".

Years. The README says years are "negative for BCE, positive for CE"; the file also uses ``0`` as the year
between ``-1`` and ``1`` (e.g. Roman Empire rows ``-14..0`` then ``1..5``). KG times use astronomical numbering
(KGCORE): provider ``y < 0`` (``|y|`` BCE) -> ``y + 1``; provider ``0`` -> ``0`` (1 BCE, flagged
``provider_year_zero``); ``y > 0`` -> ``y``. The provider's integers are kept verbatim in every statement
(qualifier ``provider_years``).

What is loaded (``source_key`` ``cliopatria.github``, one ``load_batch``; dataset predicates declared in
``registry/kg/predicates_polity.yaml`` and registered here like ``atlas_cow`` registers ``cow:code_denotes``)
---------------------------------------------------------------------------------------------------------
* entities ``cliopatria:polity:<slug>`` (kind ``polity`` / ``polity_relation``; names in parentheses, the
  provider's groupings, get the slug prefix ``grp_``), labels = the provider's name;
* ``cliopatria:reported_area`` -- one statement per row: the provider's Area (km²) over the row's years, with
  the row ordinal, geometry digest, SeshatID, Wikipedia phrase and Wikidata id as qualifiers;
* ``cliopatria:denotes`` -- polity -> the Wikidata QID the provider records, per run of rows with that QID (no
  QID is ever guessed; rows without one are counted and listed);
* ``cliopatria:member_of`` -- component -> relation (from ``MemberOf`` and ``Components``, merged runs);
* ``cliopatria:seshat_polity_id`` -- the provider's SeshatID per run of rows;
* ``geo:covered_territory_of`` -- **derived**: :mod:`reva.kg.overlay` intersects each row polygon with the Natural
  Earth 1:10m Admin-0 map units (fetched and archived in the same run, A-GEO's entity rules) and keeps runs of
  rows covering at least 1 % of a present-day country's area. Object = the country's Wikidata QID when an
  ISO3 -> QID mapping is available at load time (``registry/entities/country_qids.json`` from WDHARVEST, else
  unambiguous P298 statements already in the KG), else the REVA node ``iso3:<KEY>`` (as LINKS names unmapped
  countries), or ``naturalearth:<ADM0_A3>`` for a Natural Earth unit outside the crosswalk (Taiwan,
  Somaliland...). Statement ids do not depend on that choice, so a later load with QIDs is a new revision of
  the same statements.

A full run also retracts (``removed_at_source``) current statements of this source and origin that the new
build no longer contains (a new release or a changed overlay threshold); a ``--dataset`` partial run cannot
happen (one dataset). No atlas table is registered (atlas periods cannot represent BCE years).
"""

from __future__ import annotations

import hashlib
import inspect
import io
import json
import re
import unicodedata
import zipfile
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from reva.atlas.sources import AtlasSourceSpec
from reva.kernel.errors import SourceResponseError
from reva.kg import overlay as ov

NAME = "atlas.cliopatria"
VERSION = "1.0.0"
SOURCE_KEY = "cliopatria.github"
ATLAS_SOURCE = "cliopatria"
DATASET = "polities"
PARSER_VERSION = "atlas_cliopatria/1"
REPO = "Seshat-Global-History-Databank/cliopatria"
TAG = "v0.2.1"
COMMIT = "5f433377139a6eeaeacbdc894b70f8805b72c8b5"
RAW_HOST = "raw.githubusercontent.com"
NE_HOST = "naciscdn.org"
URL_ZIP = f"https://{RAW_HOST}/{REPO}/{TAG}/cliopatria.geojson.zip"
URL_LICENCE = f"https://{RAW_HOST}/{REPO}/{TAG}/LICENSE.md"
URL_NE = f"https://{NE_HOST}/naturalearth/10m/cultural/ne_10m_admin_0_map_units.zip"
MAX_ZIP_BYTES = 80_000_000
MAX_MEMBER_BYTES = 600_000_000
MAX_NE_BYTES = 12_000_000
MIN_ROWS_LIVE = 10_000  # truncation guard (v0.2.1 has 13,797 rows); fixture mode is trimmed
MIN_SHARE = 0.01
MAX_ROWS_IN_QUALIFIER = 150
LICENCE_MARKER = "Creative Commons Attribution 4.0 International License"
PREDICATES_FILE = Path(__file__).resolve().parents[4] / "registry" / "kg" / "predicates_polity.yaml"
CROSSWALK_FILE = Path(__file__).resolve().parents[4] / "registry" / "entities" / "country_crosswalk.json"

RIGHTS = {
    "licence": "CC BY 4.0",
    "licence_url": "http://creativecommons.org/licenses/by/4.0/",
    "attribution": (
        f"Cliopatria (Seshat Global History Databank) {TAG}, Bennett et al. (2025) Scientific Data, "
        "doi:10.1038/s41597-025-04516-9; overlay with Natural Earth 1:10m Admin-0 (public domain) computed by REVA"
    ),
}
CAVEAT = (
    "Derived by REVA: overlay of a Cliopatria polygon (approximate borders; 'only one version of the territory held "
    "by past polities', Cliopatria README; years are the provider's sampling steps) with Natural Earth 1:10m "
    "Admin-0 (default de facto view of present-day borders). A share of present-day area inside the drawn polygon; "
    "not a statement about sovereignty, control, population or causes."
)
P_OVERLAY = "geo:covered_territory_of"
P_DENOTES = "cliopatria:denotes"
P_AREA = "cliopatria:reported_area"
P_MEMBER = "cliopatria:member_of"
P_SESHAT = "cliopatria:seshat_polity_id"
QID_RE = re.compile(r"^Q[1-9][0-9]{0,11}$")
_MEMBER_RE = re.compile(r"^cliopatria_polities_only_v(\d)(\d)(\d)\.geojson$|^cliopatria\.geojson$")


class CliopatriaError(SourceResponseError):
    pass


def _err(msg: str, kind: str = "schema_drift") -> CliopatriaError:
    return CliopatriaError(msg, retryable=False, kind=kind)


# ---------------------------------------------------------------------------------------------- spec
def code_digest() -> str:
    h = hashlib.sha256()
    for m in (inspect.getmodule(code_digest), ov):
        h.update(inspect.getsource(m).encode())  # type: ignore[arg-type]
    h.update(PREDICATES_FILE.read_bytes())
    return "sha256:" + h.hexdigest()


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=NAME,
        version=VERSION,
        source_key=SOURCE_KEY,
        budget_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="Seshat Global History Databank (Cliopatria); Natural Earth (Admin-0 map units)",
        licence="CC BY 4.0 (Cliopatria); public domain (Natural Earth)",
        allowed_hosts=(RAW_HOST, NE_HOST),
        datasets=(DATASET,),
        dataset_ids=(f"cliopatria:{TAG}", "ne_10m_admin_0_map_units"),
        cadence_seconds=30 * 86400,
        planned_requests=3,
        max_response_bytes=MAX_ZIP_BYTES,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(),
        field_mappings={
            "entity": "Name",
            "years": "FromYear..ToYear (inclusive; negative = BCE; KG astronomical)",
            "wikidata": "Wikidata (provider-declared)",
            "overlay": f"{P_OVERLAY} (derived, reva.kg.overlay, min share {MIN_SHARE})",
        },
        notes=(
            "KG only (no atlas table: BCE). Polity entities + provider-declared Wikidata ids + derived overlay with "
            "present-day countries (Natural Earth 1:10m map units, A-GEO entity rules). " + CAVEAT
        )[:4000],
    )


# ---------------------------------------------------------------------------------------------- parsing
def check_licence(body: bytes) -> str:
    text = body.decode("utf-8", "replace")
    if LICENCE_MARKER not in text:
        raise _err("Cliopatria LICENSE.md no longer states CC BY 4.0: run blocked", kind="licence_changed")
    return "CC BY 4.0"


def astro_year(y: int) -> int:
    """Provider year (negative = BCE, 0 used between -1 and 1) -> astronomical year."""
    return y + 1 if y < 0 else y


def kg_year(y: int) -> Any:
    from reva.kg.model import KgTime

    a = astro_year(y)
    return KgTime(f"{'-' if a < 0 else '+'}{abs(a):04d}-01-01T00:00:00Z", 9)


def year_label(y: int) -> str:
    return f"{-y} BCE" if y < 0 else ("1 BCE (provider year 0)" if y == 0 else f"{y} CE")


def _slug(name: str) -> str:
    grp = name.startswith("(") and name.endswith(")")
    core = name[1:-1] if grp else name
    s = unicodedata.normalize("NFKD", core).encode("ascii", "ignore").decode("ascii").lower()
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")[:90] or "x"
    return ("grp_" if grp else "") + s


def entity_ids(names: Iterable[str]) -> dict[str, str]:
    """Provider name -> ``cliopatria:polity:<slug>``; names whose slugs collide all get ``-<sha6(name)>``."""
    names = sorted(set(names))
    by: dict[str, list[str]] = defaultdict(list)
    for n in names:
        by[_slug(n)].append(n)
    out = {}
    for s, ns in by.items():
        for n in ns:
            suffix = "" if len(ns) == 1 else "-" + hashlib.sha256(n.encode()).hexdigest()[:6]
            out[n] = f"cliopatria:polity:{s}{suffix}"
    return out


def _split(v: Any) -> tuple[str, ...]:
    return tuple(x.strip() for x in str(v or "").split(";") if x.strip())


def _area_text(v: Any) -> str | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or v in (float("inf"), float("-inf")):
        return None
    return format(Decimal(repr(float(v))), "f")


@dataclass(frozen=True)
class Row:
    row: int
    name: str
    type: str
    from_year: int
    to_year: int
    area: str | None
    wikipedia: str
    wikidata: str
    seshat_id: str
    components: tuple[str, ...]
    member_of: tuple[str, ...]
    geometry_sha256: str
    geometry_type: str
    reva_area_km2: float

    def ref(self) -> ov.RowRef:
        return ov.RowRef(self.row, self.name, self.from_year, self.to_year, self.geometry_sha256)


@dataclass
class Scan:
    member: str
    release: str
    rows: list[Row] = field(default_factory=list)
    hits: dict[int, list[ov.Hit]] = field(default_factory=dict)
    counters: Counter[str] = field(default_factory=Counter)
    problems: list[dict[str, Any]] = field(default_factory=list)


def open_member(body: bytes) -> tuple[zipfile.ZipFile, zipfile.ZipInfo, str]:
    try:
        zf = zipfile.ZipFile(io.BytesIO(body))
    except zipfile.BadZipFile as e:
        raise _err("cliopatria.geojson.zip is not a zip archive") from e
    infos = [i for i in zf.infolist() if i.filename.lower().endswith(".geojson")]
    if len(infos) != 1:
        raise _err(f"expected one .geojson member, got {[i.filename for i in zf.infolist()]}")
    info = infos[0]
    m = _MEMBER_RE.match(info.filename)
    if not m:
        raise _err(f"unexpected member name {info.filename!r}")
    if info.file_size > MAX_MEMBER_BYTES:
        raise _err("geojson member larger than the bound", kind="too_large")
    release = f"v{m.group(1)}.{m.group(2)}.{m.group(3)}" if m.group(1) else "unversioned"
    return zf, info, release


def scan(body: bytes, overlayer: ov.Overlayer | None, *, expect_release: str | None = TAG) -> Scan:
    """Stream the features (bounded memory), keep the row attributes, and overlay each geometry."""
    zf, info, release = open_member(body)
    if expect_release is not None and release != expect_release:
        raise _err(f"member {info.filename} is release {release}, expected {expect_release}")
    out = Scan(member=info.filename, release=release)
    with zf.open(info) as fh:
        for i, ft in enumerate(ov.iter_geojson_features(fh)):
            p = ft.get("properties") or {}
            g = ft.get("geometry")
            name = p.get("Name")
            fy, ty = p.get("FromYear"), p.get("ToYear")
            if not isinstance(name, str) or not name.strip():
                out.counters["row_without_name"] += 1
                out.problems.append({"row": i, "problem": "no Name"})
                continue
            if type(fy) is not int or type(ty) is not int or ty < fy:
                out.counters["row_invalid_years"] += 1
                out.problems.append({"row": i, "name": name, "problem": f"years {fy!r}..{ty!r}"})
                continue
            if not isinstance(g, dict) or g.get("type") not in ("Polygon", "MultiPolygon"):
                out.counters["row_without_polygon"] += 1
                out.problems.append({"row": i, "name": name, "problem": "no polygon geometry"})
                continue
            gsha = ov.canonical_json_sha256(g)
            wd = str(p.get("Wikidata") or "").strip()
            if wd and not QID_RE.match(wd):
                out.counters["row_invalid_wikidata"] += 1
                out.problems.append({"row": i, "name": name, "problem": f"Wikidata {wd[:40]!r} is not a QID"})
                wd = ""
            area_km2 = 0.0
            if overlayer is not None:
                area_km2, hits = overlayer.overlay(g, gsha)
                out.hits[i] = hits
            area = _area_text(p.get("Area"))
            if area is None:
                out.counters["row_without_area"] += 1
            out.rows.append(
                Row(
                    row=i,
                    name=name.strip(),
                    type=str(p.get("Type") or "POLITY"),
                    from_year=fy,
                    to_year=ty,
                    area=area,
                    wikipedia=str(p.get("Wikipedia") or "").strip(),
                    wikidata=wd,
                    seshat_id=str(p.get("SeshatID") or "").strip(),
                    components=_split(p.get("Components")),
                    member_of=_split(p.get("MemberOf")),
                    geometry_sha256=gsha,
                    geometry_type=str(g["type"]),
                    reva_area_km2=area_km2,
                )
            )
    out.counters["rows"] = len(out.rows)
    if overlayer is not None:
        out.counters.update({f"overlay_{k}": v for k, v in overlayer.counters.items()})
    return out


# ---------------------------------------------------------------------------------------------- runs of rows
def _runs(items: Iterable[tuple[int, int, Any]]) -> list[list[tuple[int, int, Any]]]:
    """Group ``(from, to, payload)`` items into runs of time-contiguous items (sorted by years)."""
    seq = sorted(items, key=lambda x: (x[0], x[1], json.dumps(x[2], sort_keys=True, default=str)))
    runs: list[list[tuple[int, int, Any]]] = []
    for it in seq:
        if runs and it[0] <= max(r[1] for r in runs[-1]) + 1:
            runs[-1].append(it)
        else:
            runs.append([it])
    return runs


def _years_q(fy: int, ty: int) -> dict[str, Any]:
    q: dict[str, Any] = {"provider_years": [fy, ty]}
    if 0 in (fy, ty):
        q["provider_year_zero"] = "provider year 0 read as 1 BCE (astronomical 0)"
    return q


# ---------------------------------------------------------------------------------------------- KG build
def load_predicates(path: Path = PREDICATES_FILE) -> list[Any]:
    from reva.kg.model import KgProperty

    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    keys = ("pid", "value_kind", "reva_predicate", "label_en", "label_fr", "inverse_name", "inverse_label_en",
            "inverse_label_fr")  # fmt: skip
    return [KgProperty(**{k: p[k] for k in keys if k in p}) for p in doc["predicates"]]


def crosswalk_labels(path: Path = CROSSWALK_FILE) -> dict[str, str]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    return {e["key"]: e.get("label") or e["key"] for e in doc["entities"]}


def country_node(t: ov.Territory, qids: Mapping[str, str]) -> tuple[str, str]:
    """(KG node, how): the QID, ``iso3:<KEY>`` or ``naturalearth:<ADM0_A3>``."""
    if t.iso3 is None:
        return t.key, "naturalearth_unit_outside_crosswalk"
    q = qids.get(t.iso3)
    return (q, "qid") if q else (f"iso3:{t.iso3}", "iso3_node_qid_unresolved")


@dataclass
class KgBuild:
    entities: list[Any]
    statements: list[Any]
    spans: list[ov.Span]
    report: dict[str, Any]


def kg_records(
    sc: Scan,
    layer: ov.CountryLayer | None,
    *,
    raws: Mapping[str, str],
    country_qids: Mapping[str, str] | None = None,
    min_share: float = MIN_SHARE,
    ne_url: str = URL_NE,
) -> KgBuild:
    """Entities and statements (``reva.kg.model`` dataclasses) for :func:`reva.kg.model.load_batch`.

    ``raws``: ``{"cliopatria": sha, "naturalearth": sha}`` (archived raw artifacts)."""
    from reva.kg.model import KgEntity, KgStatement

    qids = dict(country_qids or {})
    rev = f"{sc.release}@{COMMIT[:12]}" if sc.release == TAG else sc.release
    c_raw = raws["cliopatria"]
    ids = entity_ids(r.name for r in sc.rows)
    by_name: dict[str, list[Row]] = defaultdict(list)
    for r in sc.rows:
        by_name[r.name].append(r)
    # every relation / component name must be an entity of the file
    known = set(by_name)
    refs = {n for r in sc.rows for n in (*r.components, *r.member_of)}
    unknown_refs = sorted(refs - known)
    ents: list[Any] = []
    sts: list[Any] = []
    report: dict[str, Any] = {"release": sc.release, "source_revision": rev, "unknown_relation_refs": unknown_refs}
    for name in sorted(by_name):
        rows = by_name[name]
        lo, hi = min(r.from_year for r in rows), max(r.to_year for r in rows)
        types = sorted({r.type for r in rows})
        kind = "polity_relation" if types == ["RELATION"] else "polity"
        wp = sorted({r.wikipedia for r in rows if r.wikipedia and r.wikipedia != name})
        ents.append(
            KgEntity(
                qid=ids[name],
                kind=kind,
                labels={"en": name},
                descriptions={
                    "en": f"Cliopatria {'relation' if kind == 'polity_relation' else 'polity'} (Seshat), mapped "
                    f"{year_label(lo)} - {year_label(hi)} in {len(rows)} row(s)",
                    "fr": f"entité Cliopatria (Seshat), cartographiée de {lo} à {hi} (années du fournisseur, "
                    f"négatives = av. J.-C.), {len(rows)} ligne(s)",
                },
                aliases={"en": wp[:20]} if wp else {},
                source_revision=rev,
            )
        )
        eid = ids[name]
        # reported area, one per row
        for r in rows:
            if r.area is None:
                continue
            q = {
                "row": r.row,
                **_years_q(r.from_year, r.to_year),
                "geometry_sha256": r.geometry_sha256,
                "geometry_type": r.geometry_type,
                "type": r.type,
                "area_basis": "Cliopatria Area field: km2 in EPSG:6933 (provider)",
            }
            if r.reva_area_km2 > 0:
                q["reva_ellipsoidal_area_km2"] = ov.km2_text(r.reva_area_km2)
            for k, v in (("seshat_id", r.seshat_id), ("wikipedia", r.wikipedia), ("wikidata", r.wikidata)):
                if v:
                    q[k] = v
            sts.append(
                KgStatement(
                    subject_qid=eid,
                    property_pid=P_AREA,
                    value={"type": "quantity", "amount": r.area, "unit": "km2"},
                    start=kg_year(r.from_year),
                    end=kg_year(r.to_year),
                    qualifiers=q,
                    statement_id=f"area:{eid.split(':', 2)[2]}:{r.from_year}:{r.to_year}",
                    source_revision=rev,
                    raw_sha256=c_raw,
                )
            )
        # provider-declared Wikidata id and SeshatID, per run of rows carrying the same value
        for prop, attr in ((P_DENOTES, "wikidata"), (P_SESHAT, "seshat_id")):
            vals: dict[str, list[tuple[int, int, int]]] = defaultdict(list)
            for r in rows:
                v = getattr(r, attr)
                if v:
                    vals[v].append((r.from_year, r.to_year, r.row))
            for v, items in sorted(vals.items()):
                for run in _runs(items):
                    fy, ty = run[0][0], max(x[1] for x in run)
                    q = {**_years_q(fy, ty), "rows": [x[2] for x in run][:MAX_ROWS_IN_QUALIFIER], "row_count": len(run)}
                    if prop == P_DENOTES:
                        q["mapping"] = "provider_declared (Cliopatria Wikidata field); not checked by REVA"
                        wps = sorted({r.wikipedia for r in rows if r.wikidata == v and r.wikipedia})
                        if wps:
                            q["wikipedia"] = wps[:5]
                    sts.append(
                        KgStatement(
                            subject_qid=eid,
                            property_pid=prop,
                            object_qid=v if prop == P_DENOTES else None,
                            value=None if prop == P_DENOTES else {"type": "external_id", "value": v},
                            start=kg_year(fy),
                            end=kg_year(ty),
                            qualifiers=q,
                            statement_id=f"{'denotes' if prop == P_DENOTES else 'seshat'}:{eid.split(':', 2)[2]}:"
                            f"{v}:{fy}:{ty}",
                            source_revision=rev,
                            raw_sha256=c_raw,
                        )
                    )
    report["rows_without_wikidata"] = sorted(
        {(r.name, r.from_year, r.to_year) for r in sc.rows if not r.wikidata}, key=lambda x: (x[0], x[1])
    )[:500]
    report["polities_without_any_wikidata"] = sorted(n for n, rs in by_name.items() if not any(r.wikidata for r in rs))
    # membership (component -> relation) from MemberOf (member's rows) and Components (relation's rows)
    pairs: dict[tuple[str, str], list[tuple[int, int, tuple[str, int]]]] = defaultdict(list)
    for r in sc.rows:
        for m in r.member_of:
            if m in known:
                pairs[(r.name, m)].append((r.from_year, r.to_year, ("MemberOf", r.row)))
        for c in r.components:
            if c in known:
                pairs[(c, r.name)].append((r.from_year, r.to_year, ("Components", r.row)))
    for (comp, rel), pitems in sorted(pairs.items()):
        for run in _runs(pitems):
            fy, ty = run[0][0], max(x[1] for x in run)
            sts.append(
                KgStatement(
                    subject_qid=ids[comp],
                    property_pid=P_MEMBER,
                    object_qid=ids[rel],
                    start=kg_year(fy),
                    end=kg_year(ty),
                    qualifiers={
                        **_years_q(fy, ty),
                        "fields": sorted({x[2][0] for x in run}),
                        "rows": sorted({x[2][1] for x in run})[:MAX_ROWS_IN_QUALIFIER],
                    },
                    statement_id=f"member:{ids[comp].split(':', 2)[2]}:{ids[rel].split(':', 2)[2]}:{fy}:{ty}",
                    source_revision=rev,
                    raw_sha256=c_raw,
                )
            )
    # derived overlay
    span_list: list[ov.Span] = []
    if layer is not None:
        n_raw = raws["naturalearth"]
        terr = layer.by_key()
        span_list, sstats = ov.spans((r.ref() for r in sc.rows), sc.hits, min_share=min_share)
        nodes: Counter[str] = Counter()
        node_ents: dict[str, Any] = {}
        labels = crosswalk_labels()
        orev = f"{rev}+ne{layer.ne_version or '?'}+{ov.OVERLAY_VERSION}"
        for sp in span_list:
            t = terr[sp.territory]
            node, how = country_node(t, qids)
            nodes[how] += 1
            if how != "qid" and node not in node_ents:
                node_ents[node] = KgEntity(
                    qid=node,
                    kind="country" if t.iso3 else "territory",
                    labels={"en": labels.get(t.iso3, t.label) if t.iso3 else t.label},
                    descriptions={
                        "en": (
                            f"present-day country (REVA crosswalk key {t.iso3}; Wikidata QID not resolved at load)"
                            if t.iso3
                            else f"Natural Earth 1:10m Admin-0 unit {t.key.split(':', 1)[1]} (not a crosswalk entity)"
                        )
                    },
                    source_revision=orev,
                )
            rows_q = [
                {
                    "row": rr.row,
                    "provider_years": [rr.from_year, rr.to_year],
                    "share": ov.share_text(h.share),
                    "overlap_km2": ov.km2_text(h.overlap_km2),
                    "geometry_sha256": rr.geometry_sha256,
                }
                for rr, h in sp.rows
            ]
            oq: dict[str, Any] = {
                "derivation": "computed_spatial_overlay",
                "label": "derived",
                "method": ov.OVERLAY_VERSION,
                "share_of_country_area": sp.summary(),
                "overlap_km2_max": ov.km2_text(max(h.overlap_km2 for _, h in sp.rows)),
                "country_area_km2": ov.km2_text(t.area_km2),
                "country_key": t.key,
                "country_iso3": t.iso3,
                "country_node": how,
                "country_geometry_sha256": t.geometry_sha256,
                "ne_units": list(t.units),
                "ne_version": layer.ne_version,
                "ne_raw_sha256": n_raw,
                "cliopatria_release": sc.release,
                "cliopatria_raw_sha256": c_raw,
                "min_share": ov.share_text(min_share),
                **_years_q(sp.from_year, sp.to_year),
                "rows": rows_q[:MAX_ROWS_IN_QUALIFIER],
                "row_count": len(rows_q),
                "caveat": CAVEAT,
            }
            if len(rows_q) > MAX_ROWS_IN_QUALIFIER:
                oq["rows_truncated"] = True
            sts.append(
                KgStatement(
                    subject_qid=ids[sp.polity],
                    property_pid=P_OVERLAY,
                    object_qid=node,
                    start=kg_year(sp.from_year),
                    end=kg_year(sp.to_year),
                    qualifiers=oq,
                    references=[
                        {"cliopatria": URL_ZIP, "release": sc.release, "raw_sha256": c_raw},
                        {"naturalearth": ne_url, "version": layer.ne_version, "raw_sha256": n_raw},
                    ],
                    statement_id=f"overlay:{ids[sp.polity].split(':', 2)[2]}:{t.key}:{sp.from_year}:{sp.to_year}",
                    source_revision=orev,
                    raw_sha256=c_raw,
                )
            )
        ents.extend(node_ents[k] for k in sorted(node_ents))
        report["overlay"] = {
            "spans": len(span_list),
            "below_min_share_row_hits": sstats["below_min_share"],
            "country_nodes": dict(nodes),
            "ne_version": layer.ne_version,
            "ne_unmapped_units": layer.unmapped_units,
            "min_share": min_share,
        }
    report["entities"] = len(ents)
    report["statements"] = dict(Counter(s.property_pid for s in sts))
    return KgBuild(ents, sts, span_list, report)


# ---------------------------------------------------------------------------------------------- DB side
def ensure_kg_properties(engine: Any) -> dict[str, Any]:
    from reva.kg.model import register_properties

    return register_properties(engine, load_predicates()).__dict__


def resolve_country_qids(engine: Any, origin: str) -> tuple[dict[str, str], dict[str, Any]]:
    """ISO3 -> QID without guessing: ``registry/entities/country_qids.json`` (WDHARVEST) when present, else
    P298 (ISO 3166-1 alpha-3) statements of the same origin already in the KG; an ISO3 claimed by several
    items is left unresolved unless exactly one claim is preferred-rank."""
    from sqlalchemy import text

    from reva.adapters.connectors.atlas_geo_common import load_country_qids

    q = load_country_qids()
    if q:
        return q, {"basis": "registry/entities/country_qids.json", "resolved": len(q)}
    with engine.connect() as c:
        rows = c.execute(
            text("""SELECT subject_qid, upper(value->>'value') AS code, rank FROM kg.statement_current
                    WHERE property_pid = 'P298' AND origin = :o AND value IS NOT NULL"""),
            {"o": origin},
        ).all()
    claims: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for s, code, rank in rows:
        if code:
            claims[code].append((s, rank))
    out, ambiguous = {}, []
    for code, cl in claims.items():
        subj = {s for s, _ in cl}
        pref = {s for s, r in cl if r == "preferred"}
        if len(subj) == 1:
            out[code] = next(iter(subj))
        elif len(pref) == 1:
            out[code] = next(iter(pref))
        else:
            ambiguous.append(code)
    return out, {"basis": "kg P298 statements", "resolved": len(out), "ambiguous": sorted(ambiguous)}


def current_keys(engine: Any, origin: str) -> set[str]:
    from sqlalchemy import text

    with engine.connect() as c:
        return set(
            c.execute(
                text("SELECT statement_key FROM kg.statement_current WHERE source_key = :s AND origin = :o"),
                {"s": SOURCE_KEY, "o": origin},
            ).scalars()
        )


def load_kg(
    engine: Any,
    store: Any,
    build: KgBuild,
    *,
    raw_sha256s: Sequence[str],
    retrieved_at: datetime,
    origin: str,
    retract_missing: bool = True,
) -> dict[str, Any]:
    from reva.kg.model import MAX_STATEMENTS_PER_BATCH, load_batch, statement_key

    props = ensure_kg_properties(engine)
    retract: list[str] = []
    if retract_missing:
        new = {statement_key(s, SOURCE_KEY) for s in build.statements}
        retract = sorted(current_keys(engine, origin) - new)
    reports = []
    sts = build.statements
    for i in range(0, max(len(sts), 1), MAX_STATEMENTS_PER_BATCH):
        rep = load_batch(
            engine,
            store,
            entities=build.entities if i == 0 else (),
            statements=sts[i : i + MAX_STATEMENTS_PER_BATCH],
            source_key=SOURCE_KEY,
            raw_sha256s=sorted(set(raw_sha256s)),
            retrieved_at=retrieved_at,
            origin=origin,
            retract_keys=retract if i == 0 else (),
            parameters={"connector": NAME, "parser": PARSER_VERSION, "overlay": ov.OVERLAY_VERSION},
        )
        reports.append(rep.as_dict())
    keys = ("entities_inserted", "entities_unchanged", "statements_inserted", "statements_new_revision",
            "statements_unchanged", "statements_stale", "statements_conflicting", "retractions_inserted")  # fmt: skip
    total = {k: sum(r[k] for r in reports) for k in keys}
    rejected: Counter[str] = Counter()
    for r in reports:
        rejected.update(r["rejected_counts"])
    return {
        **total,
        "rejected_counts": dict(rejected),
        "rejected_sample": [x for r in reports for x in r["rejected_sample"]][:50],
        "batches": len(reports),
        "properties": {k: v for k, v in props.items() if not k.startswith("_")},
    }


# ---------------------------------------------------------------------------------------------- coverage
def coverage(build: KgBuild, layer: ov.CountryLayer) -> dict[str, Any]:
    """Per territory: polities with a run, earliest/latest provider year, years with any covering polity."""
    per: dict[str, dict[str, Any]] = {}
    years: dict[str, set[int]] = defaultdict(set)
    pol: dict[str, set[str]] = defaultdict(set)
    for sp in build.spans:
        pol[sp.territory].add(sp.polity)
        for rr, _ in sp.rows:
            years[sp.territory].update(range(rr.from_year, rr.to_year + 1))
    labels = crosswalk_labels()
    for t in layer.territories:
        ys = years.get(t.key, set())
        per[t.key] = {
            "label": labels.get(t.iso3, t.label) if t.iso3 else t.label,
            "iso3": t.iso3,
            "area_km2": round(t.area_km2, 1),
            "polities": len(pol.get(t.key, ())),
            "first_year": min(ys) if ys else None,
            "last_year": max(ys) if ys else None,
            "years_covered": len(ys),
        }
    return per


def era_counts(sc: Scan) -> dict[str, int]:
    """Distinct polities with at least one row overlapping each era (provider years)."""
    eras = [
        ("before 1000 BCE", -10_000, -1001),
        ("1000 BCE - 1 BCE", -1000, 0),
        ("1 - 500", 1, 500),
        ("501 - 1000", 501, 1000),
        ("1001 - 1500", 1001, 1500),
        ("1501 - 1800", 1501, 1800),
        ("1801 - 1900", 1801, 1900),
        ("1901 - 2024", 1901, 2100),
    ]
    out = {}
    for label, a, b in eras:
        out[label] = len({r.name for r in sc.rows if r.from_year <= b and r.to_year >= a})
    return out


# ---------------------------------------------------------------------------------------------- run
async def atlas_ingest(run: Any) -> dict[str, Any]:
    """Licence check, Cliopatria zip, Natural Earth map units -> archive all three -> overlay -> one KG load."""
    if not run.wants(DATASET):
        return {"skipped": True}
    lic = await run.get(URL_LICENCE, key="licence", media_types=("text/plain", "text/markdown"), max_bytes=100_000)
    lic_sha = run.archive(lic)
    licence = check_licence(lic.body)
    raw = await run.get(
        URL_ZIP, key="cliopatria", media_types=("application/zip", "application/octet-stream"), max_bytes=MAX_ZIP_BYTES
    )
    c_sha = run.archive(raw)
    ne = await run.get(
        URL_NE, key="ne_map_units", media_types=("application/zip", "application/octet-stream"), max_bytes=MAX_NE_BYTES
    )
    n_sha = run.archive(ne)
    layer = ov.country_layer_from_ne(ne.body)
    overlayer = ov.Overlayer(layer)
    sc = scan(raw.body, overlayer)
    if run.origin == "live" and len(sc.rows) < MIN_ROWS_LIVE:
        raise _err(f"only {len(sc.rows)} rows: truncated download?", kind="truncated")
    qids, qinfo = resolve_country_qids(run.engine, run.origin)
    build = kg_records(sc, layer, raws={"cliopatria": c_sha, "naturalearth": n_sha}, country_qids=qids)
    kg = load_kg(
        run.engine,
        run.store,
        build,
        raw_sha256s=[lic_sha, c_sha, n_sha],
        retrieved_at=max(lic.retrieved_at, raw.retrieved_at, ne.retrieved_at),
        origin=run.origin,
    )
    cov = coverage(build, layer)
    return {
        "licence": licence,
        "release": sc.release,
        "member": sc.member,
        "raw": {
            "licence": {"url": URL_LICENCE, "sha256": lic_sha, "bytes": len(lic.body)},
            "cliopatria": {"url": URL_ZIP, "sha256": c_sha, "bytes": len(raw.body)},
            "naturalearth": {"url": URL_NE, "sha256": n_sha, "bytes": len(ne.body), "version": layer.ne_version},
        },
        "rows": len(sc.rows),
        "polities": len({r.name for r in sc.rows}),
        "eras": era_counts(sc),
        "counters": dict(sc.counters),
        "problems": sc.problems[:100],
        "overlay_errors": overlayer.errors[:50],
        "country_qids": qinfo,
        "build": build.report,
        "kg": kg,
        "coverage": {
            "territories": len(cov),
            "with_any_polity": sum(1 for v in cov.values() if v["polities"]),
            "without_polity": sorted(k for k, v in cov.items() if not v["polities"]),
            "by_territory": cov,
        },
    }
