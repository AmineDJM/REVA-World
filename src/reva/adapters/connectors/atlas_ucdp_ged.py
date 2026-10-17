"""UCDP Georeferenced Event Dataset (GED 26.1 + Candidate 2026) and UCDP Non-State / One-Sided 26.1 (agent V6CONFLICT).

Source and licence
------------------
Uppsala Conflict Data Program, ``https://ucdp.uu.se/downloads/`` (no account; the token-only API is not used):

* ``ged/ged261-csv.zip`` -- UCDP GED 26.1 (``GEDEvent_v26_1.csv``): one row per event of organised violence,
  1989-01-01 .. 2025-12-31, all three types of violence, with dates, coordinates, the UCDP conflict / dyad / side ids
  and fatality estimates (best / low / high). Codebook ``ged/ged261.pdf`` (archived with every run; its sha256 is
  reported, the quoted definitions below are from it).
* ``candidateged/GEDEvent_v26_*.csv`` -- **UCDP Candidate** events 2026 (preliminary monthly ``26.0.<m>`` and
  quarterly ``26.01.26.<mm>`` releases), discovered from the downloads page (archived). Codebook
  ``candidateged/ucdp-candidate-codebook1.5.pdf``: "Not all events in UCDP Candidate will make it into the final UCDP
  GED and new events will be added"; the Candidate also "relaxes this criterion further and include all events that
  meet the definition of an event regardless of the annual threshold". Candidate rows are kept in separate tables
  (``cand_*``), flagged ``candidate: true`` with ``code_status`` verbatim, never mixed with the final release.
* ``nsos/ucdp-nonstate-261-csv.zip`` and ``nsos/ucdp-onesided-261-csv.zip`` -- the UCDP Non-State Conflict and
  One-Sided Violence datasets 26.1 (conflict-year / actor-year rows, 1989-2025) -> KG conflict-level entities.
* ``actor/ucdp-actor-261-csv.zip`` -- UCDP Actor Dataset 26.1 (labels of non-state actors, as KGGEOPOL).
* Natural Earth 1:10m Admin-1 polygons (``naciscdn.org``, public domain) -- the same file V4CLIMATE used (ADM1 entities
  ``iso3166_2:<code>``), for REVA's point-in-polygon placement of events.
* countrycode 1.9.0 (CRAN) for Gleditsch-Ward codes -> crosswalk countries (KGGEOPOL's ``StateCodeCrosswalk``).

All UCDP data: **CC BY 4.0** ("All datasets are free of charge and licensed under CC BY 4.0", downloads page).

Outputs
-------
Atlas source ``ucdp_ged`` (tables documented in ``docs/dev/v6_conflict.md`` section 0):

* ``events`` / ``cand_events`` -- one row per event: indicator ``deaths_best|event=<id>``, value = ``best``,
  provider_geo ``gw:<country_id>`` (iso3 through the crosswalk at the event's ``date_start``), period
  ``[date_start, date_end + 1 day)``, flags with the other GED fields (tov, low, high, ids, coordinates, precisions,
  PRIO-GRID cell, REVA's ADM1 placement).
* ``country_month``, ``adm1_month``, ``priogrid_month`` (+ ``cand_*``) -- **computed** sums per calendar month of
  ``date_start`` (method in flags): ``events|tov=<all|sb|ns|os>``, ``deaths_best|tov=..``, ``deaths_low|tov=..``,
  ``deaths_high|tov=..`` and (country / ADM1) ``deaths_civilians|tov=..``. Only cells with >= 1 event exist: the
  atlas never writes a zero (an absent cell means GED records no event, under GED's inclusion rules).

ADM1 placement is a REVA computation: the event coordinates (only ``where_prec`` 1-4: exact point, <= 25 km, ADM2 or
ADM1 representative point) are tested against the Natural Earth admin-1 polygons (shapely STRtree, ``covered_by``);
the subdivision is identified only by the ISO 3166-2 code Natural Earth publishes for the polygon (placeholder and
ambiguous codes are not used, as in ``atlas_ghcnm``), and only when the code's country (ISO 3166-1 prefix) equals the
country GED gives the event. GED's own ``adm_1`` *names* are never used to place anything.

KG (source key ``ucdp.ged``): conflict-level entities only -- ``ucdp:nonstate:<conflict_id>`` (kind
``non_state_conflict``) and ``ucdp:onesided:<conflict_id>`` (kind ``one_sided_violence``) from the Non-State /
One-Sided datasets, with sides / perpetrator, location, start / episode-end dates and yearly fatalities as UCDP
publishes them; GED-derived (REVA-computed) yearly event counts and fatalities, first / last event dates and
``ucdpged:events_in_adm1`` on every GED conflict id (state-based conflicts on KGGEOPOL's ``ucdp:conflict:<id>``).
UCDP ids are "unique across the dataset ... irrespective of type of violence" (GED codebook), so the link from an
event to its conflict is the published id only. Individual events are never KG entities.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from reva.adapters.connectors.atlas_geopol_common import (
    AtlasRows,
    KgEntity,
    KgStatement,
    StateCodeCrosswalk,
    StateEntityResolver,
    code_correspondence,
    fetch_countrycode,
    int_or_none,
    iso_date_or_none,
    kg_available,
    load_qid_for_iso3,
    merge_years,
    read_csv_member,
)
from reva.kernel.errors import SourceResponseError

NAME = "atlas.ucdp_ged"
CONNECTOR_NAME = NAME
VERSION = "1.0.0"
SOURCE_KEY = "ucdp.ged"
ATLAS_SOURCE = "ucdp_ged"
PARSER_VERSION = "atlas-ucdp-ged/1"
GED_VERSION = "26.1"
#: final GED coverage end: Candidate events dated on or before it are not used (the final release covers them)
GED_COVERAGE_END = date(2025, 12, 31)
CANDIDATE_YEAR = 26  # Candidate releases of this UCDP year (2026) follow the final 26.1 release
KG_REVISION = "ucdp-26.1+v6conflict.1"  # provider release + REVA's representation
HOST = "ucdp.uu.se"
NE_HOST = "naciscdn.org"
BASE = f"https://{HOST}/downloads/"
URLS: dict[str, str] = {
    "downloads": BASE,
    "ged": BASE + "ged/ged261-csv.zip",
    "ged_codebook": BASE + "ged/ged261.pdf",
    "cand_codebook": BASE + "candidateged/ucdp-candidate-codebook1.5.pdf",
    "nonstate": BASE + "nsos/ucdp-nonstate-261-csv.zip",
    "onesided": BASE + "nsos/ucdp-onesided-261-csv.zip",
    "actor": BASE + "actor/ucdp-actor-261-csv.zip",
    "ne_admin1": f"https://{NE_HOST}/naturalearth/10m/cultural/ne_10m_admin_1_states_provinces.zip",
}
GED_MEMBER = "GEDEvent_v26_1.csv"
MAX_BYTES = 60_000_000
MIN_GED_EVENTS = 400_000  # truncation guard (417,968 in 26.1)
MAX_CANDIDATE_FILES = 16
CAND_RE = re.compile(r"https://ucdp\.uu\.se/downloads/candidateged/(GEDEvent_v(\d{2})_(\d{1,2})_(\d{1,2})(?:_(\d{2}))?\.csv)")
DATASETS = (
    "events",
    "country_month",
    "adm1_month",
    "priogrid_month",
    "cand_events",
    "cand_country_month",
    "cand_adm1_month",
    "cand_priogrid_month",
)

RIGHTS: dict[str, str] = {
    "licence": "CC BY 4.0",
    "licence_url": "https://creativecommons.org/licenses/by/4.0/",
    "attribution": "Uppsala Conflict Data Program (UCDP), Department of Peace and Conflict Research, Uppsala "
    "University: UCDP Georeferenced Event Dataset (GED) Global version 26.1, UCDP Candidate Events Dataset, UCDP "
    "Non-State Conflict Dataset 26.1, UCDP One-Sided Violence Dataset 26.1",
    "citations": "Sundberg & Melander 2013 (JPR 50(4)); Hegre, Croicu, Eck & Hogbladh 2020 (JPR 57(3), Candidate); "
    "Davies et al. 2025 (JPR); Sundberg, Eck & Kreutz 2012 (JPR 49(2), non-state); Eck & Hultman 2007 (JPR 44(2), "
    "one-sided)",
}
NE_LICENCE = "Public domain (Natural Earth)"

TOV = {"1": "sb", "2": "ns", "3": "os"}
TOV_LABEL = {"sb": "state-based conflict", "ns": "non-state conflict", "os": "one-sided violence"}
#: GED where_prec values whose coordinates locate the event inside one first-order subdivision (codebook 26.1:
#: 1 exact location, 2 within 25 km of a known point, 3 ADM2 representative point, 4 ADM1 representative point).
#: 5 (linear feature / fuzzy area), 6 (country only) and 7 (international waters/airspace) are never placed.
ADM1_WHERE_PREC = frozenset({"1", "2", "3", "4"})

#: Verbatim passages of the GED 26.1 codebook (ged261.pdf) that the tables rely on.
DEFINITIONS: dict[str, str] = {
    "best": "The best (most likely) estimate of total fatalities resulting from an event. It is always the sum of "
    "deaths_a, deaths_b, deaths_civilians and deaths_unknown.",
    "low": "The lowest reliable estimate of total fatalities",
    "high": "The highest reliable estimate of total fatalities",
    "event": "In order for an event to exist, at least one dead needs to be registered in the high, best or low "
    "estimate.",
    "conflict_new_id": "This identifier is unique across the dataset (i.e. a non-state conflict cannot have the same "
    "identifier as a state-based conflict or a one-sided instance), irrespective of type of violence",
    "where_prec": "1: exact location of the event known and coded. 2: event occurred within at maximum 25 km radius "
    "around a known point. 3: only the second order administrative division where an event happened is known. 4: "
    "only the first order administrative division where an event happened is known. 5: ... a linear feature ... or "
    "a fuzzy polygon without defined borders ... 6: only the country where the event took place in is known. 7: "
    "event in international waters or airspace.",
    "inclusion": "The event dataset has a dyad and actor focus, tracing the events of all UCDP conflict dyads for both "
    "active years (years that have crossed the 25 battle related deaths threshold) and non-active years (the "
    "remainder).",
}
CM_METHOD = (
    "REVA sum over GED events by calendar month of date_start (events whose date_end falls in a later month are "
    "counted in the start month; their number is in flags.spanning); tov = type_of_violence (sb 1, ns 2, os 3)"
)
ADM1_METHOD = (
    "REVA point-in-polygon of GED coordinates (where_prec 1-4) against Natural Earth 1:10m admin-1 polygons, "
    "subdivision identified by the polygon's published iso_3166_2 code, only when its ISO 3166-1 prefix is the "
    "event's country; " + CM_METHOD
)

PREDICATES: dict[str, tuple[str, str, str, str]] = {
    "ucdpged:events": (
        "quantity",
        "ucdpged_events",
        "number of UCDP GED events in the year (computed by REVA)",
        "nombre d'événements UCDP GED dans l'année (calcul REVA)",
    ),
    "ucdpged:fatalities": (
        "quantity",
        "ucdpged_fatalities",
        "fatalities in UCDP GED events in the year, best (low/high bounds), computed by REVA",
        "morts dans les événements UCDP GED de l'année, estimation centrale (bornes basse/haute), calcul REVA",
    ),
    "ucdpged:first_event_date": (
        "time",
        "ucdpged_first_event_date",
        "earliest date_start of the UCDP GED events of the conflict",
        "plus ancienne date de début des événements UCDP GED du conflit",
    ),
    "ucdpged:last_event_date": (
        "time",
        "ucdpged_last_event_date",
        "latest date_end of the UCDP GED events of the conflict",
        "plus récente date de fin des événements UCDP GED du conflit",
    ),
    "ucdpged:events_in_adm1": (
        "item",
        "ucdpged_events_in_adm1",
        "had UCDP GED events located in first-level subdivision (REVA point in polygon)",
        "a eu des événements UCDP GED situés dans la subdivision (point dans polygone, REVA)",
    ),
    "ucdpnsos:fatalities": (
        "quantity",
        "ucdpnsos_fatalities",
        "fatalities in the year, best estimate with low/high bounds (UCDP Non-State / One-Sided dataset)",
        "morts dans l'année, estimation centrale et bornes (UCDP Non-State / One-Sided)",
    ),
    "ucdpnsos:perpetrator": (
        "item",
        "ucdpnsos_perpetrator",
        "perpetrator of one-sided violence against civilians (UCDP)",
        "auteur de violences unilatérales contre des civils (UCDP)",
    ),
    "ucdpnsos:org_level": (
        "string",
        "ucdpnsos_org_level",
        "organisational level of the warring sides (UCDP Non-State)",
        "niveau d'organisation des parties (UCDP Non-State)",
    ),
    "ucdpnsos:dyad_id": ("external_id", "ucdpnsos_dyad_id", "UCDP dyad id", "identifiant de dyade UCDP"),
}
#: KGGEOPOL predicates reused with their registered meaning (atlas_geopol_common.GEOPOL_PROPERTIES)
REUSED_GEOPOL = ("ucdp:nonstate_side", "ucdp:secondary_party", "ucdp:location", "ucdp:start_date", "ucdp:episode_end")
ORG_LEVEL = {
    "1": "formally organized groups",
    "2": "informally organized groups: supporters and affiliates of political parties and candidates",
    "3": "informally organized groups: groups sharing a common identification along ethnic, clan, religious, "
    "national or tribal lines",
}
LOCATION_NOTE = (
    "countries where the conflict/episode took place in the year (UCDP: listed if even one death occurred there; "
    "UCDP warns it should never be used for spatial analysis -- use GED)"
)
#: UCDP start/end precision codes (Non-State codebook: 1 day ... 5 year-level) -> Wikibase precision
_PREC = {"1": 11, "2": 11, "3": 10, "4": 10, "5": 9}

GED_COLUMNS = (
    "id", "relid", "year", "active_year", "code_status", "type_of_violence", "conflict_new_id", "conflict_name",
    "dyad_new_id", "dyad_name", "side_a_new_id", "side_a", "side_b_new_id", "side_b", "number_of_sources",
    "where_prec", "latitude", "longitude", "priogrid_gid", "country", "country_id", "region", "event_clarity",
    "date_prec", "date_start", "date_end", "deaths_a", "deaths_b", "deaths_civilians", "deaths_unknown", "best",
    "high", "low", "gwnoa", "gwnob",
)  # fmt: skip
INT_COLS = ("id", "type_of_violence", "conflict_new_id", "dyad_new_id", "deaths_a", "deaths_b", "deaths_civilians",
            "deaths_unknown", "best", "high", "low")  # fmt: skip


class UcdpGedError(SourceResponseError):
    pass


def _err(msg: str, kind: str = "schema_drift") -> UcdpGedError:
    return UcdpGedError(msg, retryable=False, kind=kind)


def code_digest() -> str:
    from reva.adapters.connectors import atlas_infra_common as ic

    return ic.code_digest(sys.modules[__name__])


def atlas_source() -> Any:
    from reva.atlas.sources import AtlasSourceSpec

    return AtlasSourceSpec(
        name=NAME,
        version=VERSION,
        source_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="Uppsala Conflict Data Program (Uppsala University): GED 26.1, Candidate, Non-State, One-Sided; "
        "Natural Earth admin-1 polygons for REVA's ADM1 placement",
        licence=f"{RIGHTS['licence']}; Natural Earth: public domain",
        allowed_hosts=(HOST, NE_HOST, "cran.r-project.org"),
        datasets=DATASETS,
        dataset_ids=(f"ucdp-ged-{GED_VERSION}", "ucdp-candidate-2026", f"ucdp-nonstate-{GED_VERSION}",
                     f"ucdp-onesided-{GED_VERSION}", "naturalearth-admin1-10m"),  # fmt: skip
        cadence_seconds=30 * 86400,
        planned_requests=9 + 4,
        max_response_bytes=MAX_BYTES,
        min_interval_seconds=2.0,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(),
        field_mappings={
            "geo": "gw:<country_id> (UCDP GW codes) -> crosswalk ISO3; iso3166_2:<code> (REVA point-in-polygon); "
            "priogrid:<gid> (GED's own cell)",
            "time": "event: [date_start, date_end]; aggregates: calendar month of date_start",
            "kg": "ucdp:nonstate:<id>, ucdp:onesided:<id>; GED statistics on ucdp:conflict|nonstate|onesided:<id>",
        },
        notes="UCDP API needs a token; official downloads are used. Candidate releases discovered on the downloads "
        "page. Natural Earth admin-1 = the file V4CLIMATE placed GHCN stations with.",
    )


# --------------------------------------------------------------------------- candidate discovery
@dataclass(frozen=True)
class CandFile:
    name: str
    url: str
    kind: str  # monthly | quarterly
    start_month: int
    end_month: int

    @property
    def order(self) -> tuple[int, int]:
        """Release order: later coverage end first wins; a quarterly re-release wins over a monthly of the same end."""
        return (self.end_month, 1 if self.kind == "quarterly" else 0)


def discover_candidates(html: bytes) -> list[CandFile]:
    """Candidate CSV links of the downloads page for the year after the final release (2026).

    Names: ``GEDEvent_v26_0_<m>.csv`` (monthly, month m) and ``GEDEvent_v26_01_26_<mm>.csv`` (quarterly, Jan..mm).
    Only the latest quarterly file and the monthly files after its last month are used (a quarterly release re-codes
    its whole period)."""
    text = html.decode("utf-8", errors="replace")
    found: dict[str, CandFile] = {}
    for m in CAND_RE.finditer(text):
        name, yy, a, b, c = m.group(1), int(m.group(2)), m.group(3), m.group(4), m.group(5)
        if yy != CANDIDATE_YEAR:
            continue
        if c is None and a == "0":
            cf = CandFile(name, m.group(0), "monthly", int(b), int(b))
        elif c is not None and int(b) == CANDIDATE_YEAR:
            cf = CandFile(name, m.group(0), "quarterly", int(a), int(c))
        else:
            continue
        if 1 <= cf.start_month <= cf.end_month <= 12:
            found[name] = cf
    quarterly = [f for f in found.values() if f.kind == "quarterly"]
    q_end = max((f.end_month for f in quarterly), default=0)
    keep = [max(quarterly, key=lambda f: f.end_month)] if quarterly else []
    keep += [f for f in found.values() if f.kind == "monthly" and f.end_month > q_end]
    keep.sort(key=lambda f: f.order)
    if len(keep) > MAX_CANDIDATE_FILES:
        raise _err(f"{len(keep)} candidate files listed (> {MAX_CANDIDATE_FILES})", kind="incomplete")
    return keep


# --------------------------------------------------------------------------- GED parsing
def read_ged(body: bytes, *, member: str | None, min_rows: int = 0) -> Any:
    """GED / Candidate CSV (zipped or plain) -> polars frame of strings (all columns kept as published)."""
    import polars as pl

    from reva.adapters.http.archive import read_zip_members

    if member is not None:
        members = read_zip_members(body, (member.lower(),), max_total=400 * 1024 * 1024)
        if len(members) != 1:
            raise _err(f"GED zip: expected one {member} member, found {sorted(members)}")
        (data,) = members.values()
    else:
        data = body
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise _err("GED CSV is not UTF-8") from e
    df = pl.read_csv(io.BytesIO(data), infer_schema=False, encoding="utf8")
    missing = [c for c in GED_COLUMNS if c not in df.columns]
    if missing:
        raise _err(f"GED CSV lacks columns {missing}")
    if df.height < min_rows:
        raise _err(f"GED: {df.height} events < expected {min_rows} (truncated?)", kind="incomplete")
    df = df.select(list(GED_COLUMNS)).with_row_index("record", offset=1)
    bad = df.filter(pl.any_horizontal([pl.col(c).is_null() | (pl.col(c).str.strip_chars() == "") for c in INT_COLS]))
    if bad.height:
        raise _err(f"GED: {bad.height} events without id / type / ids / death counts (e.g. id {bad['id'][0]})")
    df = df.with_columns(
        [pl.col(c).str.strip_chars().cast(pl.Int64, strict=True) for c in INT_COLS]
        + [
            pl.col("date_start").str.slice(0, 10).str.to_date("%Y-%m-%d", strict=True).alias("d0"),
            pl.col("date_end").str.slice(0, 10).str.to_date("%Y-%m-%d", strict=True).alias("d1"),
        ]
    )
    if df.filter(pl.col("d1") < pl.col("d0")).height:
        raise _err("GED: date_end before date_start")
    if df["id"].n_unique() != df.height:
        raise _err("GED: duplicate event ids within one file")
    return df


def check_events(df: Any) -> dict[str, int]:
    """Codebook consistency counts (reported, never corrected)."""
    import polars as pl

    s = df.select(
        (pl.col("best") != pl.col("deaths_a") + pl.col("deaths_b") + pl.col("deaths_civilians")
         + pl.col("deaths_unknown")).sum().alias("best_not_sum_of_parts"),  # fmt: skip
        ((pl.col("low") > pl.col("best")) | (pl.col("best") > pl.col("high"))).sum().alias("low_best_high_unordered"),
        ((pl.col("best") == 0) & (pl.col("high") == 0) & (pl.col("low") == 0)).sum().alias("all_estimates_zero"),
        (pl.col("best") == 0).sum().alias("best_zero"),
        (pl.col("d0").dt.strftime("%Y-%m") != pl.col("d1").dt.strftime("%Y-%m")).sum().alias("spanning_months"),
    ).row(0, named=True)
    return {k: int(v) for k, v in s.items()}


class Resolver:
    """GW code + date -> crosswalk resolution, cached (418 k events, ~10^4 distinct code-days)."""

    def __init__(self, crosswalk: StateCodeCrosswalk) -> None:
        self.cw = crosswalk
        self._cache: dict[tuple[int, date], Any] = {}

    def __call__(self, gw: int, when: date) -> Any:
        k = (gw, when)
        r = self._cache.get(k)
        if r is None:
            r = self._cache[k] = self.cw.resolve("ucdp", gw, when)
        return r


# --------------------------------------------------------------------------- ADM1 placement
@dataclass
class Adm1Index:
    codes: list[str]
    usable: set[str]
    tree: Any
    geoms: list[Any]
    sha256: str
    report: dict[str, Any]


def adm1_index(body: bytes, *, origin: str) -> Adm1Index:
    from shapely import STRtree  # type: ignore[import-untyped]

    from reva.adapters.connectors.atlas_ghcnm import read_ne_admin1, usable_codes

    recs, geoms, rep = read_ne_admin1(body, origin=origin)
    return Adm1Index([r.code for r in recs], usable_codes(recs), STRtree(list(geoms)), geoms,
                     hashlib.sha256(body).hexdigest(), rep)  # fmt: skip


def place_adm1(df: Any, idx: Adm1Index, iso3_of: Sequence[str | None]) -> tuple[list[str | None], list[str]]:
    """Per event: (ISO 3166-2 code or None, status). Vectorised STRtree query over the placeable events."""
    import numpy as np
    import shapely  # type: ignore[import-untyped]

    from reva.atlas.tables import country_iso3

    n = df.height
    codes: list[str | None] = [None] * n
    status = ["not_placed:where_prec_" + str(w) for w in df["where_prec"].to_list()]
    wp = df["where_prec"].to_list()
    cand = [i for i in range(n) if wp[i] in ADM1_WHERE_PREC]
    if not cand:
        return codes, status
    lat = df["latitude"].to_list()
    lon = df["longitude"].to_list()
    pts = shapely.points(np.array([float(lon[i]) for i in cand]), np.array([float(lat[i]) for i in cand]))
    inp, hit = idx.tree.query(pts, predicate="covered_by")
    hits: dict[int, list[int]] = defaultdict(list)
    for a, b in zip(inp.tolist(), hit.tolist(), strict=True):
        hits[a].append(b)
    prefix_iso3: dict[str, str | None] = {}
    for j, i in enumerate(cand):
        h = hits.get(j, [])
        if not h:
            status[i] = "not_placed:outside_polygons"
            continue
        if len(h) != 1 or idx.codes[h[0]] not in idx.usable:
            status[i] = "not_placed:ambiguous_or_placeholder_code"
            continue
        code = idx.codes[h[0]]
        ev_iso3 = iso3_of[i]
        if ev_iso3 is None:
            status[i] = "not_placed:historical_or_unmapped_state"
            continue
        if code[:2] not in prefix_iso3:
            prefix_iso3[code[:2]] = country_iso3(code[:2], "iso2")
        if prefix_iso3[code[:2]] != ev_iso3:
            status[i] = "not_placed:country_mismatch"
            continue
        codes[i] = code
        status[i] = "placed"
    return codes, status


# --------------------------------------------------------------------------- atlas rows
def _month(d: date) -> tuple[date, date]:
    return date(d.year, d.month, 1), (date(d.year + 1, 1, 1) if d.month == 12 else date(d.year, d.month + 1, 1))


def _j(obj: Mapping[str, Any]) -> str:
    return json.dumps({k: v for k, v in obj.items() if v not in (None, "")}, separators=(",", ":"), ensure_ascii=False)


@dataclass
class Prepared:
    df: Any
    iso3: list[str | None]
    qid: list[str | None]
    reason: list[str]
    adm1: list[str | None]
    adm1_status: list[str]
    sha: str
    file: str
    candidate: bool
    release: list[str] | None = None  # candidate file name per event
    release_sha: list[str] | None = None  # candidate raw sha256 per event
    checks: dict[str, int] = field(default_factory=dict)

    def sha_of(self, i: int) -> str:
        return self.release_sha[i] if self.release_sha else self.sha


def prepare(df: Any, *, resolver: Resolver, idx: Adm1Index, sha: str, file: str, candidate: bool,
            release: list[str] | None = None, release_sha: list[str] | None = None) -> Prepared:  # fmt: skip
    gws = df["country_id"].to_list()
    d0s = df["d0"].to_list()
    iso3: list[str | None] = []
    qid: list[str | None] = []
    reason: list[str] = []
    for gw, d0 in zip(gws, d0s, strict=True):
        code = int_or_none(gw)
        if code is None:
            raise _err(f"GED event without country_id ({file})")
        r = resolver(code, d0)
        iso3.append(r.iso3)
        qid.append(r.qid)
        reason.append(r.reason)
    adm1, st = place_adm1(df, idx, iso3)
    return Prepared(df, iso3, qid, reason, adm1, st, sha, file, candidate, release, release_sha, check_events(df))


def event_rows(p: Prepared, dataset: str) -> AtlasRows:
    rows = AtlasRows()
    cols = p.df.select(
        "record", "id", "relid", "type_of_violence", "low", "high", "best", "deaths_a", "deaths_b", "deaths_civilians",
        "deaths_unknown", "conflict_new_id", "dyad_new_id", "side_a_new_id", "side_b_new_id", "gwnoa", "gwnob",
        "latitude", "longitude", "where_prec", "date_prec", "event_clarity", "active_year", "priogrid_gid",
        "number_of_sources", "country_id", "code_status", "d0", "d1",
    ).iter_rows()  # fmt: skip
    for i, r in enumerate(cols):
        (rec, eid, relid, tov, low, high, best, da, db, dc, du, cid, did, sa, sb, gwa, gwb, lat, lon, wp, dp, ec, ay,
         pg, nsrc, gw, cs, d0, d1) = r  # fmt: skip
        flags: dict[str, Any] = {
            "tov": tov, "low": low, "high": high, "deaths_a": da, "deaths_b": db, "deaths_civilians": dc,
            "deaths_unknown": du, "conflict_new_id": cid, "dyad_new_id": did, "side_a_new_id": sa,
            "side_b_new_id": sb, "gwnoa": gwa, "gwnob": gwb, "lat": lat, "lon": lon, "where_prec": wp,
            "date_prec": dp, "event_clarity": ec, "active_year": ay, "priogrid_gid": pg, "number_of_sources": nsrc,
            "relid": relid, "adm1": p.adm1[i], "adm1_status": p.adm1_status[i],
        }  # fmt: skip
        if p.reason[i] != "panel":
            flags["code_resolution"] = p.reason[i]
        if p.candidate:
            flags["candidate"] = True
            flags["code_status"] = cs
            flags["release"] = p.release[i] if p.release else None
        sel: dict[str, Any] = {"file": p.release[i] if p.release else p.file, "id": eid}
        if not p.release:
            sel["record"] = rec
        rows.add(
            {
                "source": ATLAS_SOURCE, "dataset": dataset, "indicator": f"deaths_best|event={eid}",
                "concept_id": None, "iso3": p.iso3[i], "provider_geo": f"gw:{gw}", "entity_qid": p.qid[i],
                "period_start": d0, "period_end": d1 + timedelta(days=1), "value": str(best), "value_text": None,
                "unit": "deaths", "flags": _j(flags), "raw_sha256": p.sha_of(i),
                "selector": _j(sel),
            }
        )  # fmt: skip
    return rows


def _agg_rows(
    p: Prepared,
    dataset: str,
    keys: Sequence[tuple[str, str | None, str | None]],
    *,
    with_civilians: bool,
    method: str,
    extra_flags: Sequence[Mapping[str, Any]] | None = None,
) -> AtlasRows:
    """keys[i] = (provider_geo, iso3, entity_qid) of event i (provider_geo '' = event not in this table)."""
    acc: dict[tuple[str, date, str], list[int]] = {}
    geo_of: dict[str, tuple[str | None, str | None]] = {}
    ef: dict[str, Mapping[str, Any]] = {}
    shas: dict[tuple[str, date], set[str]] = defaultdict(set)
    d0 = p.df["d0"].to_list()
    d1 = p.df["d1"].to_list()
    tov = p.df["type_of_violence"].to_list()
    best = p.df["best"].to_list()
    low = p.df["low"].to_list()
    high = p.df["high"].to_list()
    civ = p.df["deaths_civilians"].to_list()
    eids = p.df["id"].to_list()
    first_ids: dict[tuple[str, date], list[int]] = defaultdict(list)
    for i, (geo, iso3, qid) in enumerate(keys):
        if not geo:
            continue
        m0 = date(d0[i].year, d0[i].month, 1)
        geo_of.setdefault(geo, (iso3, qid))
        if extra_flags is not None:
            ef.setdefault(geo, extra_flags[i])
        span = 1 if (d1[i].year, d1[i].month) != (d0[i].year, d0[i].month) else 0
        for t in ("all", TOV[str(tov[i])]):
            a = acc.setdefault((geo, m0, t), [0, 0, 0, 0, 0, 0])
            a[0] += 1
            a[1] += best[i]
            a[2] += low[i]
            a[3] += high[i]
            a[4] += civ[i]
            a[5] += span
        shas[(geo, m0)].add(p.sha_of(i))
        if len(first_ids[(geo, m0)]) < 20:
            first_ids[(geo, m0)].append(eids[i])
    rows = AtlasRows()
    inds = ["events", "deaths_best", "deaths_low", "deaths_high"] + (["deaths_civilians"] if with_civilians else [])
    for (geo, m0, t), a in sorted(acc.items()):
        start, end = _month(m0)
        iso3, qid = geo_of[geo]
        flags: dict[str, Any] = {"computed": method, "events": a[0], "spanning": a[5] or None, **dict(ef.get(geo, {}))}
        if p.candidate:
            flags["candidate"] = True
        sh = sorted(shas[(geo, m0)])
        for k, ind in enumerate(inds):
            val = a[0] if k == 0 else (a[1], a[2], a[3], a[4])[k - 1]
            rows.add(
                {
                    "source": ATLAS_SOURCE, "dataset": dataset, "indicator": f"{ind}|tov={t}", "concept_id": None,
                    "iso3": iso3, "provider_geo": geo, "entity_qid": qid, "period_start": start, "period_end": end,
                    "value": str(val), "value_text": None, "unit": "count" if k == 0 else "deaths",
                    "flags": _j(flags), "raw_sha256": sh[0],
                    "selector": _j({"file": p.file, "event_ids_sample": first_ids[(geo, m0)], "events": a[0],
                                    **({"raw_sha256s": sh} if len(sh) > 1 else {})}),
                }
            )  # fmt: skip
    return rows


def country_month_rows(p: Prepared, dataset: str) -> AtlasRows:
    gws = p.df["country_id"].to_list()
    d0 = p.df["d0"].to_list()
    # one resolution per (code, month): the crosswalk at the 15th (documented); conflicting resolutions withheld
    keys = []
    cache: dict[tuple[str, int, int], tuple[str | None, str | None]] = {}
    for i, gw in enumerate(gws):
        k = (gw, d0[i].year, d0[i].month)
        if k not in cache:
            cache[k] = (p.iso3[i], p.qid[i])
        keys.append((f"gw:{gw}", cache[k][0], cache[k][1]))
    rows = _agg_rows(p, dataset, keys, with_civilians=True, method=CM_METHOD)
    return _withhold_clashes(rows)


def _withhold_clashes(rows: AtlasRows) -> AtlasRows:
    """Two provider codes resolving to one country in the same month: iso3 withheld on both (flagged)."""
    c = rows.cols
    seen: dict[tuple[str, str, date], set[str]] = defaultdict(set)
    for ind, iso3, geo, ps in zip(c["indicator"], c["iso3"], c["provider_geo"], c["period_start"], strict=True):
        if iso3:
            seen[(ind, iso3, ps)].add(geo)
    bad = {k for k, v in seen.items() if len(v) > 1}
    if bad:
        for i in range(len(rows)):
            k = (c["indicator"][i], c["iso3"][i], c["period_start"][i])
            if k in bad:
                f = json.loads(c["flags"][i])
                f["iso3_withheld"] = f"{c['iso3'][i]}: several provider codes map to it in this month"
                c["flags"][i] = _j(f)
                c["iso3"][i] = None
    return rows


def adm1_month_rows(p: Prepared, dataset: str) -> AtlasRows:
    keys = [(f"iso3166_2:{a}" if a else "", None, None) for a in p.adm1]
    extra = [{"country_iso3": iso3} for iso3 in p.iso3]
    return _agg_rows(p, dataset, keys, with_civilians=True, method=ADM1_METHOD, extra_flags=extra)


def priogrid_month_rows(p: Prepared, dataset: str) -> AtlasRows:
    keys = [(f"priogrid:{g}" if (g or "").strip() else "", None, None) for g in p.df["priogrid_gid"].to_list()]
    return _agg_rows(p, dataset, keys, with_civilians=False,
                     method="GED's own priogrid_gid; " + CM_METHOD)  # fmt: skip


# --------------------------------------------------------------------------- candidate merge
def merge_candidates(frames: Sequence[tuple[CandFile, Any, str]]) -> tuple[Any, list[str], list[str], dict[str, int]]:
    """Concatenate Candidate files in release order; an event id in a later release replaces the earlier version.
    Events dated on or before the final GED coverage end are dropped (counted)."""
    import polars as pl

    parts = []
    for k, (cf, df, sha) in enumerate(sorted(frames, key=lambda x: x[0].order)):
        parts.append(df.with_columns(pl.lit(cf.name).alias("_release"), pl.lit(sha).alias("_sha"), pl.lit(k).alias("_k")))
    if not parts:
        return None, [], [], {}
    allf = pl.concat(parts, how="vertical")
    n_all = allf.height
    allf = allf.sort(["id", "_k"]).unique(subset=["id"], keep="last", maintain_order=True)
    superseded = n_all - allf.height
    before = allf.filter(pl.col("d0") <= GED_COVERAGE_END).height
    allf = allf.filter(pl.col("d0") > GED_COVERAGE_END).sort(["d0", "id"])
    rep = {"rows_in_files": n_all, "superseded_by_later_release": superseded, "dated_in_final_coverage": before,
           "events": allf.height}  # fmt: skip
    return allf, allf["_release"].to_list(), allf["_sha"].to_list(), rep


# --------------------------------------------------------------------------- KG
def _q(amount: int, unit: str = "1", low: int | None = None, high: int | None = None) -> dict[str, Any]:
    v: dict[str, Any] = {"type": "quantity", "amount": str(amount), "unit": unit}
    if low is not None:
        v["lowerBound"] = str(low)
    if high is not None:
        v["upperBound"] = str(high)
    return v


SUBJECT_PREFIX = {1: "ucdp:conflict:", 2: "ucdp:nonstate:", 3: "ucdp:onesided:"}


def ged_kg_statements(p: Prepared, ne_sha: str) -> list[KgStatement]:
    """REVA-computed GED statistics per UCDP conflict id (final release only)."""
    df = p.df
    tov = df["type_of_violence"].to_list()
    cid = df["conflict_new_id"].to_list()
    yr = df["d0"].to_list()
    d1 = df["d1"].to_list()
    best = df["best"].to_list()
    low = df["low"].to_list()
    high = df["high"].to_list()
    eids = df["id"].to_list()
    cy: dict[tuple[int, int, int], list[int]] = {}
    span: dict[tuple[int, int], list[Any]] = {}
    adm: dict[tuple[int, int, str], list[Any]] = {}
    for i in range(df.height):
        k = (tov[i], cid[i])
        a = cy.setdefault((tov[i], cid[i], yr[i].year), [0, 0, 0, 0])
        a[0] += 1
        a[1] += best[i]
        a[2] += low[i]
        a[3] += high[i]
        s = span.setdefault(k, [yr[i], d1[i], eids[i], eids[i]])
        if yr[i] < s[0]:
            s[0], s[2] = yr[i], eids[i]
        if d1[i] > s[1]:
            s[1], s[3] = d1[i], eids[i]
        if p.adm1[i]:
            b = adm.setdefault((tov[i], cid[i], p.adm1[i]), [0, 0, set()])
            b[0] += 1
            b[1] += best[i]
            b[2].add(yr[i].year)
    out: list[KgStatement] = []
    sel = {"file": p.file}
    deriv = {"derived": f"REVA aggregation of UCDP GED {GED_VERSION} events by conflict_new_id"}
    for (t, c, y), a in sorted(cy.items()):
        subj = f"{SUBJECT_PREFIX[t]}{c}"
        q = {**deriv, "type_of_violence": TOV_LABEL[TOV[str(t)]], "definition_best": DEFINITIONS["best"]}
        out.append(KgStatement(key=f"ged:{c}:events:{y}", subject=subj, property="ucdpged:events", value=_q(a[0]),
                               point_in_time=f"{y}-01-01", pit_precision=9, qualifiers=q, raw_sha256=p.sha,
                               selector={**sel, "conflict_new_id": c, "year": y}))  # fmt: skip
        out.append(KgStatement(key=f"ged:{c}:fatalities:{y}", subject=subj, property="ucdpged:fatalities",
                               value=_q(a[1], "deaths", a[2], a[3]), point_in_time=f"{y}-01-01", pit_precision=9,
                               qualifiers={**q, "estimate": "sum of best (bounds: sums of low and high)"},
                               raw_sha256=p.sha, selector={**sel, "conflict_new_id": c, "year": y}))  # fmt: skip
    for (t, c), s in sorted(span.items()):
        subj = f"{SUBJECT_PREFIX[t]}{c}"
        out.append(KgStatement(key=f"ged:{c}:first_event", subject=subj, property="ucdpged:first_event_date",
                               value={"type": "time", "time": s[0].isoformat(), "precision": 11},
                               qualifiers={**deriv, "event_id": s[2], "basis": "min date_start"}, raw_sha256=p.sha,
                               selector={**sel, "id": s[2], "column": "date_start"}))  # fmt: skip
        out.append(KgStatement(key=f"ged:{c}:last_event", subject=subj, property="ucdpged:last_event_date",
                               value={"type": "time", "time": s[1].isoformat(), "precision": 11},
                               qualifiers={**deriv, "event_id": s[3], "basis": "max date_end"}, raw_sha256=p.sha,
                               selector={**sel, "id": s[3], "column": "date_end"}))  # fmt: skip
    for (t, c, code), b in sorted(adm.items()):
        years = sorted(b[2])
        out.append(
            KgStatement(
                key=f"ged:{c}:adm1:{code}", subject=f"{SUBJECT_PREFIX[t]}{c}", property="ucdpged:events_in_adm1",
                object_qid=f"iso3166_2:{code}", start=f"{years[0]}-01-01", start_precision=9,
                end=f"{years[-1]}-12-31", end_precision=9,
                qualifiers={"derived": "point_in_polygon", "method": ADM1_METHOD.split(";")[0], "events": b[0],
                            "deaths_best": b[1], "years": years, "natural_earth_sha256": ne_sha},
                raw_sha256=p.sha, selector={**sel, "conflict_new_id": c, "adm1": code},
            )
        )  # fmt: skip
    return out


def _years_runs(subj: str, pred: str, obj: str, yl: list[tuple[int, int]], key: str, file: str, sha: str,
                quals: Mapping[str, Any]) -> list[KgStatement]:  # fmt: skip
    out = []
    years = [y for y, _ in yl]
    for a, b in merge_years(years):
        run = [y for y in sorted(set(years)) if a <= y <= b]
        out.append(KgStatement(key=f"{key}:{a}", subject=subj, property=pred, object_qid=obj, start=f"{a}-01-01",
                               start_precision=9, end=f"{b}-12-31", end_precision=9,
                               qualifiers={**quals, "years": run}, raw_sha256=sha,
                               selector={"file": file, "rows": [ln for y, ln in yl if a <= y <= b][:50]}))  # fmt: skip
    return out


def nsos_kg(
    ns: Sequence[Mapping[str, str]],
    os_: Sequence[Mapping[str, str]],
    actors: Sequence[Mapping[str, str]],
    *,
    shas: Mapping[str, str],
    resolver: StateEntityResolver,
) -> tuple[list[KgEntity], list[KgStatement], dict[str, int]]:
    """Conflict-level entities of the Non-State and One-Sided datasets with their UCDP-coded statements."""
    actor_name = {a["ActorId"]: a["NameData"] for a in actors}
    ents: list[KgEntity] = []
    sts: list[KgStatement] = []
    used_actors: set[str] = set()
    ns_file, os_file = "NonState_v26_1.csv", "OneSided_v26_1.csv"
    for kind, rows, file, sha in (("ns", ns, ns_file, shas["nonstate"]), ("os", os_, os_file, shas["onesided"])):
        by: dict[str, list[tuple[int, Mapping[str, str]]]] = defaultdict(list)
        for i, r in enumerate(rows):
            if (r.get("version") or "") != GED_VERSION:
                raise _err(f"{file} row {i + 2}: version {r.get('version')!r} != {GED_VERSION}")
            by[r["conflict_id"]].append((i + 2, r))
        for cid, items in sorted(by.items(), key=lambda x: int(x[0])):
            items.sort(key=lambda x: int(x[1]["year"]))
            first, last = items[0][1], items[-1][1]
            years = [int(r["year"]) for _, r in items]
            prefix = "ucdp:nonstate:" if kind == "ns" else "ucdp:onesided:"
            subj = f"{prefix}{cid}"
            if kind == "ns":
                label = f"{last['side_a_name']} - {last['side_b_name']} (UCDP non-state conflict {cid})"
                ek, desc = "non_state_conflict", "UCDP Non-State Conflict Dataset"
            else:
                label = f"{last['actor_name']} - civilians (UCDP one-sided violence {cid})"
                ek, desc = "one_sided_violence", "UCDP One-Sided Violence Dataset"
            ents.append(KgEntity(qid=subj, kind=ek, labels={"en": label},
                                 descriptions={"en": f"{desc} {GED_VERSION}, conflict id {cid}, active years "
                                               f"{min(years)}-{max(years)}"}))  # fmt: skip
            rel: dict[tuple[str, str, str], list[tuple[int, int]]] = defaultdict(list)
            for line, r in items:
                y = int(r["year"])
                when = date(y, 7, 1)
                for gw in _codes(r.get("gwno_location")):
                    rel[("ucdp:location", resolver.entity("ucdp", gw, when), "")].append((y, line))
                if kind == "ns":
                    for side in ("a", "b"):
                        aid = (r.get(f"side_{side}_id") or "").strip()
                        if aid:
                            used_actors.add(aid)
                            rel[("ucdp:nonstate_side", f"ucdp:actor:{aid}", side)].append((y, line))
                        for gw in _codes(r.get(f"gwno_{side}_2nd")):
                            rel[("ucdp:secondary_party", resolver.entity("ucdp", gw, when), side)].append((y, line))
                else:
                    aid = r["actor_id"].strip()
                    gwa = _codes(r.get("gwnoa"))
                    if r.get("is_government_actor") == "1" and len(gwa) == 1:
                        obj = resolver.entity("ucdp", gwa[0], when)
                    else:
                        obj = f"ucdp:actor:{aid}"
                        used_actors.add(aid)
                    rel[("ucdpnsos:perpetrator", obj, aid)].append((y, line))
                q = {k: r[k] for k in ("low_fatality_estimate", "high_fatality_estimate") if (r.get(k) or "").strip()}
                sts.append(KgStatement(
                    key=f"{kind}:{cid}:fatalities:{y}", subject=subj, property="ucdpnsos:fatalities",
                    value=_q(int(r["best_fatality_estimate"]), "deaths", int_or_none(q.get("low_fatality_estimate")),
                             int_or_none(q.get("high_fatality_estimate"))),
                    point_in_time=f"{y}-01-01", pit_precision=9,
                    qualifiers={"estimate": "best_fatality_estimate (bounds: low/high_fatality_estimate)",
                                "definition": "automatic aggregation (summing) of the GED figures of the "
                                "conflict/dyad-year (UCDP codebook)"},
                    raw_sha256=sha, selector={"file": file, "row": line, "column": "best_fatality_estimate"}))  # fmt: skip
                if kind == "ns" and r.get("ep_end") == "1" and iso_date_or_none(r.get("ep_end_date")):
                    sts.append(KgStatement(
                        key=f"ns:{cid}:episode_end:{y}", subject=subj, property="ucdp:episode_end",
                        value={"type": "time", "time": r["ep_end_date"][:10],
                               "precision": _PREC.get(r.get("ep_end_prec") or "", 9)},
                        qualifiers={"definition": "the last date of recorded combat (conflict inactive the "
                                    "following year)", "ucdp_ep_end_prec": r.get("ep_end_prec")},
                        raw_sha256=sha, selector={"file": file, "row": line, "column": "ep_end_date"}))  # fmt: skip
            for (pred, obj, side), yl in sorted(rel.items()):
                quals: dict[str, Any] = {}
                if pred in ("ucdp:nonstate_side", "ucdp:secondary_party"):
                    quals["side"] = side
                if pred == "ucdpnsos:perpetrator":
                    quals["ucdp_actor_id"] = side
                if pred == "ucdp:location":
                    quals["definition"] = LOCATION_NOTE
                sts += _years_runs(subj, pred, obj, yl, f"{kind}:{cid}:{pred.split(':')[1]}:{side}:{obj}", file,
                                   sha, quals)  # fmt: skip
            sts.append(KgStatement(key=f"{kind}:{cid}:dyad_id", subject=subj, property="ucdpnsos:dyad_id",
                                   value={"type": "external_id", "value": last["dyad_id"]}, raw_sha256=sha,
                                   selector={"file": file, "row": items[-1][0], "column": "dyad_id"}))  # fmt: skip
            if kind == "ns":
                lvl = ORG_LEVEL.get(last.get("org", ""))
                if lvl:
                    sts.append(KgStatement(key=f"ns:{cid}:org", subject=subj, property="ucdpnsos:org_level",
                                           value={"type": "string", "value": f"{last['org']}: {lvl}"},
                                           raw_sha256=sha, selector={"file": file, "row": items[-1][0],
                                                                     "column": "org"}))  # fmt: skip
                sd = iso_date_or_none(first.get("start_date"))
                if sd:
                    sts.append(KgStatement(
                        key=f"ns:{cid}:start_date", subject=subj, property="ucdp:start_date",
                        value={"type": "time", "time": sd.isoformat(), "precision": _PREC.get(first.get("start_prec")
                                                                                              or "", 9)},
                        qualifiers={"definition": "date of the first fatal event of the conflict (start_date)",
                                    "ucdp_start_prec": first.get("start_prec")},
                        raw_sha256=sha, selector={"file": file, "row": items[0][0], "column": "start_date"}))  # fmt: skip
    for aid in sorted(used_actors, key=lambda s: int(s) if s.isdigit() else 0):
        name = actor_name.get(aid)
        ents.append(KgEntity(qid=f"ucdp:actor:{aid}", kind="armed_group",
                             labels={"en": (name or "").strip() or f"UCDP actor {aid}"},
                             descriptions={"en": f"UCDP actor {aid} (UCDP Actor Dataset {GED_VERSION})"}))  # fmt: skip
    rep = {"nonstate_conflicts": len({r["conflict_id"] for r in ns}),
           "onesided_instances": len({r["conflict_id"] for r in os_}), "actors": len(used_actors),
           "actors_without_name": sum(1 for a in used_actors if a not in actor_name)}  # fmt: skip
    return ents, sts, rep


def _codes(field_: str | None) -> list[int]:
    out = []
    for part in (field_ or "").split(","):
        v = int_or_none(part)
        if v is not None:
            out.append(v)
    return out


def register_predicates(engine: Any) -> None:
    from reva.adapters.connectors import atlas_climate_common as cc

    cc.register_props(engine, PREDICATES, ())


def predicates_file() -> Path:
    return Path(__file__).resolve().parents[4] / "registry" / "kg" / "predicates_conflict.yaml"


# --------------------------------------------------------------------------- ingest
@dataclass
class Got:
    key: str
    url: str
    body: bytes
    sha: str
    retrieved_at: datetime
    raw: Any


async def _get(run: Any, key: str, url: str, media: tuple[str, ...]) -> Got:
    raw = await run.get(url, key=key, media_types=media, max_bytes=MAX_BYTES)
    return Got(key, url, raw.body, hashlib.sha256(raw.body).hexdigest(), raw.retrieved_at, raw)


ZIP = ("application/zip", "application/x-zip-compressed", "application/octet-stream")
CSV = ("text/csv", "application/octet-stream", "text/plain")
PDF = ("application/pdf", "application/octet-stream")


async def atlas_ingest(run: Any) -> dict[str, Any]:
    """Fetch the UCDP files + Natural Earth admin-1 + countrycode, archive them after parsing, register the
    atlas tables and load the conflict-level KG records."""
    crosswalk, cc_sha = await fetch_countrycode(run)
    got: dict[str, Got] = {}
    for key, url in URLS.items():
        media = ("text/html",) if key == "downloads" else (PDF if key.endswith("codebook") else ZIP)
        got[key] = await _get(run, key, url, media)
    cands = discover_candidates(got["downloads"].body)
    cand_got = [await _get(run, f"cand:{c.name}", c.url, CSV) for c in cands]

    strict = run.origin == "live"
    idx = adm1_index(got["ne_admin1"].body, origin=run.origin)
    resolver = Resolver(crosswalk)
    ged = read_ged(got["ged"].body, member=GED_MEMBER, min_rows=MIN_GED_EVENTS if strict else 1)
    final = prepare(ged, resolver=resolver, idx=idx, sha=got["ged"].sha, file=GED_MEMBER, candidate=False)
    cframes = [(c, read_ged(g.body, member=None), g.sha) for c, g in zip(cands, cand_got, strict=True)]
    cdf, crel, cshas, crep = merge_candidates(cframes)
    cand = None
    if cdf is not None and cdf.height:
        cand = prepare(cdf.drop(["_release", "_sha", "_k"]), resolver=resolver, idx=idx, sha=cshas[0],
                       file="candidate", candidate=True, release=crel, release_sha=cshas)  # fmt: skip
    ns = read_csv_member(got["nonstate"].body, "NonState_v26_1.csv")
    os_ = read_csv_member(got["onesided"].body, "OneSided_v26_1.csv")
    actors = read_csv_member(got["actor"].body, "Actor_v26_1.csv", encoding="cp1252")
    if strict and (len(ns) < 1500 or len(os_) < 1200):
        raise _err(f"Non-State {len(ns)} / One-Sided {len(os_)} rows (truncated?)", kind="incomplete")

    for g in [*got.values(), *cand_got]:  # parsed first: nothing archived on schema drift
        run.archive(g.raw)

    shas_all = {k: g.sha for k, g in got.items()}
    tables: list[dict[str, Any]] = []

    def reg(dataset: str, rows: AtlasRows, raws: list[str], note: str) -> None:
        if not run.wants(dataset) or len(rows) == 0:
            return
        out = run.register(dataset=dataset, frame=rows.frame(), raw_sha256s=sorted(set(raws)),
                           rights={**RIGHTS, "source_key": SOURCE_KEY}, coverage_note=note)  # fmt: skip
        tables.append({"table": out["dataset_key"].removeprefix("atlas."), "rows": out["row_count"],
                       "created": out["created"], "artifact_sha256": out["artifact_sha256"]})  # fmt: skip

    fin_note = f"UCDP GED {GED_VERSION} (final), 1989-2025"
    ne_note = f"; ADM1 = REVA point-in-polygon, Natural Earth admin-1 sha256 {idx.sha256}"
    reg("events", event_rows(final, "events"), [final.sha], fin_note + "; one row per event, value = best" + ne_note)
    reg("country_month", country_month_rows(final, "country_month"), [final.sha], fin_note + "; COMPUTED: " + CM_METHOD)
    reg("adm1_month", adm1_month_rows(final, "adm1_month"), [final.sha, idx.sha256],
        fin_note + "; COMPUTED: " + ADM1_METHOD + ne_note)
    reg("priogrid_month", priogrid_month_rows(final, "priogrid_month"), [final.sha], fin_note + "; COMPUTED by cell")
    if cand is not None:
        cn = "UCDP Candidate 2026 (PRELIMINARY; releases " + ", ".join(c.name for c in cands) + ")"
        reg("cand_events", event_rows(cand, "cand_events"), cshas, cn)
        reg("cand_country_month", country_month_rows(cand, "cand_country_month"), cshas, cn + "; COMPUTED: " + CM_METHOD)
        reg("cand_adm1_month", adm1_month_rows(cand, "cand_adm1_month"), [*cshas, idx.sha256],
            cn + "; COMPUTED: " + ADM1_METHOD + ne_note)
        reg("cand_priogrid_month", priogrid_month_rows(cand, "cand_priogrid_month"), cshas, cn + "; COMPUTED by cell")

    adm_status = Counter(final.adm1_status)
    res: dict[str, Any] = {
        "ok": True,
        "ged_events": final.df.height,
        "ged_checks": final.checks,
        "adm1_placement": dict(adm_status.most_common()),
        "candidate_files": [c.name for c in cands],
        "candidate": crep,
        "candidate_checks": cand.checks if cand else None,
        "candidate_adm1_placement": dict(Counter(cand.adm1_status).most_common()) if cand else None,
        "natural_earth": {"sha256": idx.sha256, **{k: v for k, v in idx.report.items() if k != "codes_invalid"}},
        "codebooks": {k: shas_all[k] for k in ("ged_codebook", "cand_codebook")},
        "tables": tables,
        "unmapped_state_codes": sorted({f"gw:{g}" for g, i in zip(final.df["country_id"].to_list(), final.iso3,
                                                                    strict=True) if i is None}),  # fmt: skip
    }
    kg: dict[str, Any] = {"loaded": False}
    if run.dataset is None and run.engine is not None and kg_available(run.engine):
        from reva.adapters.connectors.atlas_geopol_common import load_kg

        sres = StateEntityResolver(crosswalk, load_qid_for_iso3())
        ents, sts, nsrep = nsos_kg(ns, os_, actors, shas=shas_all, resolver=sres)
        sts += ged_kg_statements(final, idx.sha256)
        ents += sres.code_entities()
        sts += code_correspondence(sres, raw_sha256=cc_sha)
        register_predicates(run.engine)
        load = load_kg(run.engine, run.store, ents, sts, source_key=SOURCE_KEY, source_revision=KG_REVISION,
                       raw_sha256s=sorted({*shas_all.values(), cc_sha}),
                       retrieved_at=max(g.retrieved_at for g in got.values()), origin=run.origin)  # fmt: skip
        kg = {"loaded": True, "entities": len(ents), "statements": len(sts), "report": nsrep, "load": load}
    res["kg"] = kg
    return res
