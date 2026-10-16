"""NOAA NCEI Global Historical Hazards: significant earthquakes, tsunamis (sources and runups) and significant
volcanic eruptions as dated, located events with their published impacts (agent A-HAZARDS).

Source and access
-----------------
NCEI/WDS Global Historical Hazard Database, read through the HazEL hazard-service JSON API documented by its own
OpenAPI file (``https://www.ngdc.noaa.gov/hazel/hazard-service/docs/api.json``, OpenAPI 3.1, checked
2026-10-16). Lists are paged (``itemsPerPage`` <= 200, refused above), so a full run is ~240 GETs:

* ``/earthquakes`` -- NCEI/WDS Global Significant Earthquake Database, 2150 BC to present (doi:10.7289/V5TD9V7K);
* ``/tsunamis/events`` and ``/tsunamis/runups`` -- NCEI/WDS Global Historical Tsunami Database, 2100 BC to present
  (doi:10.7289/V5PN93H7): tsunami *sources* and the *runup* locations where effects were observed;
* ``/volcanoes`` -- NCEI/WDS Global Significant Volcanic Eruptions Database, 4360 BC to present (doi:10.7289/V5JW8BSH);
* ``/descriptors/...`` -- NCEI's own code lists (tsunami cause and validity codes, effect description scales,
  volcanic death agents, warning statuses, runup measurement types). Codes are decoded only from these archived
  responses, never from a table typed here.

Rights: a US Government work (NOAA), not subject to copyright in the US (17 U.S.C. 105); the NCEI ISO metadata of
the three databases say "Access to this dataset is unrestricted" and ask to cite the DOI ("Cite as: National
Geophysical Data Center / World Data Service (NGDC/WDS): ... NOAA National Centers for Environmental
Information. doi:...").

Years: NCEI writes B.C. years as negative numbers without a year 0 (HazEL search form: "Year, example: 200 B.C. =
-200"; no record has year 0). REVA uses astronomical years (year 0 = 1 BC), so NCEI year ``y < 0`` becomes
``y + 1`` (2150 BC = NCEI -2150 = astronomical -2149). The calendar of pre-1582 dates is not stated by NCEI;
dates are kept as published (labelled gregorian, with the NCEI fields verbatim in the qualifiers).

Outputs
-------
* Atlas table ``ncei_hazards.country_year`` -- **COMPUTED** by REVA: per NCEI country and event year, counts of
  records and sums of the published impact numbers, separately for each database (never added across hazards,
  because an earthquake's "total" effects already include its tsunami). Records without a published number are
  not counted in a sum (no imputation); every cell says how many records it covers and how many reported the
  value. Countries are NCEI's ``country`` field (present-day country of the location), mapped to the crosswalk
  through ``registry/atlas/geo_aliases/a_hazards.yaml``; oceans, seas and ambiguous names stay unmapped (kept,
  ``provider_geo`` = the NCEI name upper-cased).
* KG (source key ``noaa.ncei_hazards``): events ``ncei_hazards:earthquake:<id>``, ``ncei_hazards:tsunami:<id>``,
  ``ncei_hazards:eruption:<id>`` and volcanoes ``ncei_hazards:volcano:<NCEI location id>`` with their date
  (``P585``, astronomical, day/month/year precision), coordinates (``P625``), NCEI location and country, magnitudes,
  VEI, tsunami cause/validity codes, impacts as published (``ncei:deaths`` ... with NCEI's description codes),
  NCEI's association links (context, not causal) and the runup countries of each tsunami.
* Causal claims (CAUSCORE ``reva.causal.load_dataset_claims``, tier T1, relation ``coded_trigger``): earthquake ->
  tsunami and eruption -> tsunami, **only** where the NCEI tsunami record identifies the source event by its id
  (``earthquakeEventId`` / ``volcanoEventId``) *and* its cause code names that hazard (earthquake: codes 1-5;
  volcano: 4-7). Other associations stay context. Records NCEI marks "Erroneous entry" (validity -1) give no claim.
  Deaths and damage are attributes of the events, never separate claims.
* ``atlas.noaa_hazards_wikidata`` (budget key ``wikidata.wdqs``): one WDQS query for Wikidata ``P1886`` (Global
  Volcanism Program ID). NCEI's "Volcano Number" is the GVP volcano number (HazEL help: "Smithsonian Global
  Volcanism Program (GVP) provides permanent unique identifiers (Volcano Numbers)"), so a Wikidata item carrying
  the same number is the same volcano: ``ncei_hazards:volcano:<id> --ncei:same_as--> Q...`` under source key
  ``wikidata.wdqs`` (community-edited), with the shared identifier as evidence. No Wikidata property holds NCEI
  hazard *event* ids (property search 2026-10-16), so events get no ``same_as``; never by name or date.
"""

from __future__ import annotations

import calendar
import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlencode

from reva.kernel.errors import SourceResponseError

NAME = "atlas.noaa_hazards"
WD_NAME = "atlas.noaa_hazards_wikidata"
VERSION = "1.0.0"
SOURCE_KEY = "noaa.ncei_hazards"
WD_SOURCE_KEY = "wikidata.wdqs"
ATLAS_SOURCE = "ncei_hazards"
DATASET = "country_year"
WD_DATASET = "volcano_wikidata"
PARSER_VERSION = "atlas_noaa_hazards/1"
HOST = "www.ngdc.noaa.gov"
WD_HOST = "query.wikidata.org"
API = f"https://{HOST}/hazel/hazard-service/api/v1"
GEO_SCHEME = "ncei_hazards"
QID_NS = "ncei_hazards"
ITEMS_PER_PAGE = 200  # the API refuses more ("must be less than or equal to 200")
MAX_PAGES = 400
MAX_PAGE_BYTES = 2_000_000
MAX_WD_BYTES = 5_000_000
DAY = 86_400
MEDIA = ("application/json",)

#: endpoint key -> (path, year-window parameter names, minimum record count of a full live run)
ENDPOINTS: dict[str, tuple[str, tuple[str, str], int]] = {
    "earthquakes": ("earthquakes", ("minYear", "maxYear"), 6000),
    "tsunamis": ("tsunamis/events", ("minYear", "maxYear"), 2800),
    "runups": ("tsunamis/runups", ("sourceMinYear", "sourceMaxYear"), 30000),
    "volcanoes": ("volcanoes", ("minYear", "maxYear"), 800),
}
#: NCEI code lists (decoded from these archived responses only)
DESCRIPTORS: dict[str, str] = {
    "tsunami_causes": "tsunami/causes",
    "tsunami_validity": "tsunami/validity",
    "effect_people": "effect/people",
    "effect_houses": "effect/houses",
    "effect_damage": "effect/damage",
    "volcano_agents": "volcano/agents",
    "tsunami_warning_statuses": "tsunami/warning-statuses",
    "runup_measurement_types": "tsunami/runup/measurement-types",
}

DOIS = {
    "earthquakes": "10.7289/V5TD9V7K",
    "tsunamis": "10.7289/V5PN93H7",
    "runups": "10.7289/V5PN93H7",
    "volcanoes": "10.7289/V5JW8BSH",
}
DB_TITLES = {
    "earthquakes": "NCEI/WDS Global Significant Earthquake Database",
    "tsunamis": "NCEI/WDS Global Historical Tsunami Database",
    "runups": "NCEI/WDS Global Historical Tsunami Database (runups)",
    "volcanoes": "NCEI/WDS Global Significant Volcanic Eruptions Database",
}
RIGHTS: dict[str, str] = {
    "licence": "US Government work (NOAA): public domain in the US (17 U.S.C. 105); NCEI: 'Access to this dataset "
    "is unrestricted'",
    "licence_url": "https://www.ncei.noaa.gov/access/metadata/landing-page/bin/iso?id=gov.noaa.ngdc.mgg.hazards:G012153",
    "attribution": "National Geophysical Data Center / World Data Service (NGDC/WDS): NCEI/WDS Global Significant "
    "Earthquake Database (doi:10.7289/V5TD9V7K), Global Historical Tsunami Database (doi:10.7289/V5PN93H7), Global "
    "Significant Volcanic Eruptions Database (doi:10.7289/V5JW8BSH). NOAA National Centers for Environmental "
    "Information.",
}

#: Effects published by NCEI on earthquake, tsunami and eruption records: field -> (predicate, unit, descriptor
#: list decoding the ``<field>AmountOrder`` code). ``<field>Total`` = "from the <hazard> and secondary effects".
EFFECTS: dict[str, tuple[str, str, str]] = {
    "deaths": ("deaths", "persons", "effect_people"),
    "missing": ("missing", "persons", "effect_people"),
    "injuries": ("injuries", "persons", "effect_people"),
    "damageMillionsDollars": ("damage_million_usd", "million US$ (value at the time of the event)", "effect_damage"),
    "housesDestroyed": ("houses_destroyed", "houses", "effect_houses"),
    "housesDamaged": ("houses_damaged", "houses", "effect_houses"),
}
#: The amount-order field of an effect (``damageMillionsDollars`` -> ``damageAmountOrder``).
AMOUNT_ORDER = {
    "deaths": "deathsAmountOrder",
    "missing": "missingAmountOrder",
    "injuries": "injuriesAmountOrder",
    "damageMillionsDollars": "damageAmountOrder",
    "housesDestroyed": "housesDestroyedAmountOrder",
    "housesDamaged": "housesDamagedAmountOrder",
}
MAGNITUDES = {
    "eqMagnitude": "preferred magnitude (NCEI eqMagnitude)",
    "eqMagMw": "moment magnitude Mw",
    "eqMagMs": "surface-wave magnitude Ms",
    "eqMagMb": "body-wave magnitude mb",
    "eqMagMl": "local magnitude ML",
    "eqMagMfa": "felt-area magnitude Mfa",
    "eqMagUnk": "magnitude of unknown type",
}
#: NCEI tsunami cause codes under which an identified source event is the coded trigger (descriptor tsunami/causes:
#: 1 Earthquake, 2 Questionable Earthquake, 3 Earthquake and Landslide, 4 Volcano and Earthquake, 5 Volcano,
#: Earthquake and Landslide, 6 Volcano, 7 Volcano and Landslide). Checked against the archived descriptor at run time.
EQ_CAUSE_CODES = frozenset({1, 2, 3, 4, 5})
VOLCANO_CAUSE_CODES = frozenset({4, 5, 6, 7})
_CAUSE_WORDS = {"earthquake": "earthquake", "volcano": "volcano"}

CODEBOOK_URL = "https://doi.org/10.7289/V5PN93H7"
CODEBOOK_NAME = "NCEI/WDS Global Historical Tsunami Database: tsunami source event record (HazEL hazard-service v1)"
#: Verbatim NCEI help text (HazEL application, help "Tsunami Cause Code"); the code labels come from the archived
#: ``descriptors/tsunami/causes`` response.
CAUSE_CODE_HELP = "Tsunami Cause Code. Valid values: 0 to 11. The source of the tsunami:"


class NoaaHazardsError(SourceResponseError):
    pass


def _err(msg: str, kind: str = "schema_drift") -> NoaaHazardsError:
    return NoaaHazardsError(msg, retryable=False, kind=kind)


# ============================================================================================== parsing
@dataclass(frozen=True)
class Page:
    endpoint: str
    page: int
    total_pages: int
    total_items: int
    items: tuple[dict[str, Any], ...]
    sha256: str
    url: str


def parse_json(body: bytes) -> Any:
    """JSON with decimal literals kept verbatim as strings (``6.6`` -> ``"6.6"``; integers stay ``int``)."""
    try:
        return json.loads(body.decode("utf-8"), parse_float=str)
    except (UnicodeDecodeError, ValueError) as e:
        raise _err(f"not JSON: {e}") from e


def parse_page(endpoint: str, body: bytes, url: str = "") -> Page:
    doc = parse_json(body)
    if not isinstance(doc, dict) or not isinstance(doc.get("items"), list):
        errs = doc.get("formErrors") if isinstance(doc, dict) else None
        raise _err(f"{endpoint}: no 'items' list in the response (formErrors={errs})")
    for k in ("page", "totalPages", "totalItems"):
        if not isinstance(doc.get(k), int):
            raise _err(f"{endpoint}: paging field {k!r} missing or not an integer")
    items = doc["items"]
    for it in items:
        if not isinstance(it, dict) or not isinstance(it.get("id"), int):
            raise _err(f"{endpoint}: record without an integer id")
        if endpoint != "volcanolocs" and not isinstance(it.get("year"), int):
            raise _err(f"{endpoint}: record {it.get('id')} without an integer year")
    return Page(
        endpoint=endpoint,
        page=doc["page"],
        total_pages=doc["totalPages"],
        total_items=doc["totalItems"],
        items=tuple(items),
        sha256=hashlib.sha256(body).hexdigest(),
        url=url,
    )


def parse_descriptor(body: bytes) -> dict[str, str]:
    doc = parse_json(body)
    if not isinstance(doc, dict) or not isinstance(doc.get("items"), list):
        raise _err("descriptor: no 'items' list")
    out = {}
    for it in doc["items"]:
        if not isinstance(it, dict) or "id" not in it or not isinstance(it.get("description"), str):
            raise _err("descriptor: item without id/description")
        out[str(it["id"])] = it["description"]
    return out


def page_url(endpoint: str, page: int, window: tuple[int, int] | None = None) -> str:
    path, (lo_name, hi_name), _ = ENDPOINTS[endpoint]
    params: dict[str, Any] = {}
    if window is not None:
        params[lo_name], params[hi_name] = window
    params["page"] = page
    return f"{API}/{path}?{urlencode(params)}"


def descriptor_url(key: str) -> str:
    return f"{API}/descriptors/{DESCRIPTORS[key]}"


def dec(v: Any) -> str | None:
    """A published number as a plain decimal string (``1E+3`` -> ``1000``), or None when absent/not numeric."""
    if v is None or isinstance(v, bool):
        return None
    try:
        d = Decimal(str(v))
    except InvalidOperation:
        return None
    if not d.is_finite():
        return None
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".") or "0"
    return "0" if s in ("-0", "") else s


def astronomical_year(ncei_year: int) -> int:
    """NCEI year (B.C. negative, no year 0) -> astronomical year (year 0 = 1 BC)."""
    if ncei_year == 0:
        raise _err("NCEI year 0 does not exist in its B.C./A.D. numbering")
    return ncei_year + 1 if ncei_year < 0 else ncei_year


def _is_leap(y: int) -> bool:  # proleptic Gregorian, astronomical year
    return y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)


def _days_in_month(y: int, m: int) -> int:
    if m == 2:
        return 29 if _is_leap(y) else 28
    return calendar.monthrange(2001, m)[1]


def event_time(rec: Mapping[str, Any]) -> tuple[str, int, dict[str, Any]]:
    """(ISO time in astronomical years, Wikibase precision, NCEI fields kept verbatim)."""
    y = astronomical_year(int(rec["year"]))
    m, d = rec.get("month"), rec.get("day")
    quals: dict[str, Any] = {"ncei_year": rec["year"]}
    for k in ("month", "day", "hour", "minute", "second"):
        if rec.get(k) is not None:
            quals[f"ncei_{k}"] = rec[k]
    ys = f"-{abs(y):04d}" if y < 0 else f"{y:04d}"
    if isinstance(m, int) and 1 <= m <= 12:
        if isinstance(d, int) and 1 <= d <= _days_in_month(y, m):
            return f"{ys}-{m:02d}-{d:02d}", 11, quals
        if d is not None:
            quals["date_note"] = "NCEI day not valid for the month in the proleptic Gregorian calendar; month precision"
        return f"{ys}-{m:02d}-01", 10, quals
    if m is not None:
        quals["date_note"] = "NCEI month outside 1-12; year precision"
    return f"{ys}-01-01", 9, quals


def date_text(rec: Mapping[str, Any]) -> str:
    y = int(rec["year"])
    ytxt = f"{-y} BC" if y < 0 else str(y)
    m, d = rec.get("month"), rec.get("day")
    if isinstance(m, int) and 1 <= m <= 12:
        if isinstance(d, int) and 1 <= d <= 31 and y > 0:
            return f"{y:04d}-{m:02d}-{d:02d}"
        return f"{ytxt}-{m:02d}" if y > 0 else f"{ytxt} (month {m})"
    return ytxt


def country_key(name: Any) -> str | None:
    """NCEI country name normalised as the alias scheme stores it (upper case, single spaces)."""
    if not isinstance(name, str) or not name.strip():
        return None
    return " ".join(name.split()).upper()


# ============================================================================================== fetched data
@dataclass
class Rec:
    """One NCEI record with its location in the archived raw page."""

    endpoint: str
    data: dict[str, Any]
    sha256: str
    page: int
    index: int

    @property
    def id(self) -> int:
        return int(self.data["id"])

    def ref(self, *fields: str) -> dict[str, Any]:
        return {
            "raw_sha256": self.sha256,
            "selector": {
                "endpoint": ENDPOINTS[self.endpoint][0],
                "page": self.page,
                "index": self.index,
                "id": self.id,
                **({"fields": list(fields)} if fields else {}),
            },
            "stated_in": f"{DB_TITLES[self.endpoint]} (doi:{DOIS[self.endpoint]})",
        }


@dataclass
class HazardData:
    records: dict[str, list[Rec]] = field(default_factory=dict)
    descriptors: dict[str, dict[str, str]] = field(default_factory=dict)
    descriptor_shas: dict[str, str] = field(default_factory=dict)
    page_shas: dict[str, list[str]] = field(default_factory=dict)
    window: tuple[int, int] | None = None
    retrieved_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def by_id(self, endpoint: str) -> dict[int, Rec]:
        return {r.id: r for r in self.records.get(endpoint, [])}

    def all_shas(self) -> list[str]:
        out = set(self.descriptor_shas.values())
        for v in self.page_shas.values():
            out.update(v)
        return sorted(out)

    def revision(self) -> str:
        h = hashlib.sha256("\n".join(self.all_shas()).encode()).hexdigest()[:16]
        return f"hazel-v1:{h}"


def assemble(
    pages: Mapping[str, Sequence[Page]],
    descriptors: Mapping[str, bytes],
    *,
    strict: bool,
    window: tuple[int, int] | None = None,
    retrieved_at: datetime | None = None,
) -> HazardData:
    """Check paging completeness and build :class:`HazardData` (no I/O)."""
    data = HazardData(window=window)
    if retrieved_at is not None:
        data.retrieved_at = retrieved_at
    for key, body in descriptors.items():
        data.descriptors[key] = parse_descriptor(body)
        data.descriptor_shas[key] = hashlib.sha256(body).hexdigest()
    for ep, plist in pages.items():
        plist = sorted(plist, key=lambda p: p.page)
        if not plist:
            raise _err(f"{ep}: no page fetched", kind="incomplete")
        total_pages, total_items = plist[0].total_pages, plist[0].total_items
        if [p.page for p in plist] != list(range(1, max(total_pages, 1) + 1)):
            raise _err(f"{ep}: pages {[p.page for p in plist]} != 1..{total_pages}", kind="incomplete")
        recs = [Rec(ep, dict(it), p.sha256, p.page, i) for p in plist for i, it in enumerate(p.items)]
        ids = Counter(r.id for r in recs)
        dups = [i for i, n in ids.items() if n > 1]
        if dups:
            raise _err(
                f"{ep}: duplicate record ids across pages {dups[:5]} (data changed during paging?)", kind="incomplete"
            )
        if len(recs) != total_items:
            raise _err(f"{ep}: {len(recs)} records != totalItems {total_items}", kind="incomplete")
        if strict and window is None and len(recs) < ENDPOINTS[ep][2]:
            raise _err(f"{ep}: only {len(recs)} records (< {ENDPOINTS[ep][2]}): truncated?", kind="incomplete")
        data.records[ep] = recs
        data.page_shas[ep] = [p.sha256 for p in plist]
    causes = data.descriptors.get("tsunami_causes", {})
    for code, word in ((1, "earthquake"), (6, "volcano")):
        if word not in causes.get(str(code), "").lower():
            raise _err(f"NCEI tsunami cause code {code} is no longer '{word}': {causes.get(str(code))!r}")
    return data


async def fetch(run: Any, window: tuple[int, int] | None = None) -> tuple[HazardData, list[Any]]:
    """Fetch every list page and descriptor through the run's metered client (nothing archived yet)."""
    raws: list[Any] = []
    pages: dict[str, list[Page]] = {}
    for ep in ENDPOINTS:
        plist: list[Page] = []
        p, total = 1, 1
        while p <= total:
            url = page_url(ep, p, window)
            raw = await run.get(
                url,
                key=f"{ep}:page:{p}",
                media_types=MEDIA,
                max_bytes=MAX_PAGE_BYTES,
                params={"endpoint": ep, "page": p, **({"window": list(window)} if window else {})},
            )
            pg = parse_page(ep, raw.body, url)
            if pg.page != p:
                raise _err(f"{ep}: asked page {p}, got page {pg.page}")
            total = pg.total_pages
            if total > MAX_PAGES:
                raise _err(f"{ep}: {total} pages > {MAX_PAGES}", kind="unexpected_size")
            raws.append(raw)
            plist.append(pg)
            p += 1
        pages[ep] = plist
    desc: dict[str, bytes] = {}
    for key in DESCRIPTORS:
        raw = await run.get(
            descriptor_url(key),
            key=f"descriptor:{key}",
            media_types=MEDIA,
            max_bytes=MAX_PAGE_BYTES,
            params={"descriptor": key},
        )
        raws.append(raw)
        desc[key] = raw.body
    retrieved = max(r.retrieved_at for r in raws)
    data = assemble(pages, desc, strict=run.origin == "live", window=window, retrieved_at=retrieved)
    return data, raws


# ============================================================================================== atlas table
#: indicator -> (endpoint, kind, field or None, unit, label)
INDICATORS: dict[str, tuple[str, str, str | None, str]] = {
    "eq_events": ("earthquakes", "count", None, "events"),
    "eq_deaths": ("earthquakes", "sum", "deaths", "persons"),
    "eq_deaths_total": ("earthquakes", "sum", "deathsTotal", "persons"),
    "eq_injuries": ("earthquakes", "sum", "injuries", "persons"),
    "eq_damage_musd": ("earthquakes", "sum", "damageMillionsDollars", "million US$ (value at the time of the event)"),
    "eq_houses_destroyed": ("earthquakes", "sum", "housesDestroyed", "houses"),
    "ts_events": ("tsunamis", "count", None, "events"),
    "ts_events_valid34": ("tsunamis", "count_valid34", None, "events"),
    "ts_deaths": ("tsunamis", "sum", "deaths", "persons"),
    "ts_deaths_total": ("tsunamis", "sum", "deathsTotal", "persons"),
    "ts_runups": ("runups", "count", None, "runup records"),
    "ts_runup_deaths": ("runups", "sum", "deaths", "persons"),
    "vo_eruptions": ("volcanoes", "count", None, "eruptions"),
    "vo_deaths": ("volcanoes", "sum", "deaths", "persons"),
    "vo_deaths_total": ("volcanoes", "sum", "deathsTotal", "persons"),
    "vo_injuries": ("volcanoes", "sum", "injuries", "persons"),
    "vo_damage_musd": ("volcanoes", "sum", "damageMillionsDollars", "million US$ (value at the time of the event)"),
    "vo_houses_destroyed": ("volcanoes", "sum", "housesDestroyed", "houses"),
}
_GEO_BASIS = {
    "earthquakes": "NCEI earthquake record 'country' (present-day country of the epicentral location)",
    "tsunamis": "NCEI tsunami source record 'country' (country of the source location)",
    "runups": "NCEI runup record 'country' (country where the runup/effect was observed)",
    "volcanoes": "NCEI eruption record 'country' (country of the volcano)",
}
_YEAR_BASIS = {
    "earthquakes": "event year",
    "tsunamis": "tsunami source event year",
    "runups": "year of the tsunami source event (runup records carry the source date)",
    "volcanoes": "eruption event year",
}


def _resolve_geo(name_key: str | None) -> str | None:
    from reva.atlas.tables import country_iso3

    if name_key is None:
        return None
    return country_iso3(name_key, GEO_SCHEME)


def country_year_rows(data: HazardData, *, resolve: Any = None) -> list[dict[str, Any]]:
    """COMPUTED per (NCEI country, astronomical year) counts and sums; mapped names are aggregated per crosswalk
    entity (``provider_geo`` = its key, the NCEI names listed in the flags), unmapped ones keep their NCEI name."""
    resolve = resolve or _resolve_geo
    rows: list[dict[str, Any]] = []
    for ep in ENDPOINTS:
        recs = data.records.get(ep, [])
        groups: dict[tuple[str, str | None, int], list[Rec]] = defaultdict(list)
        names: dict[tuple[str, str | None, int], set[str]] = defaultdict(set)
        for r in recs:
            ck = country_key(r.data.get("country"))
            if ck is None:
                ck = "(NO COUNTRY)"
                iso = None
            else:
                iso = resolve(ck)
            geo = iso or ck
            y = astronomical_year(int(r.data["year"]))
            groups[(geo, iso, y)].append(r)
            names[(geo, iso, y)].add(str(r.data.get("country") or ""))
        for ind, (iep, kind, fld, unit) in INDICATORS.items():
            if iep != ep:
                continue
            for (geo, iso, y), grp in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][2])):
                if ep == "tsunamis" and kind != "count_valid34":
                    grp_used = [r for r in grp if r.data.get("eventValidity") != -1]
                elif kind == "count_valid34":
                    grp_used = [r for r in grp if r.data.get("eventValidity") in (3, 4)]
                else:
                    grp_used = grp
                if not grp_used:
                    continue
                flags: dict[str, Any] = {
                    "computed_from": f"{DB_TITLES[ep]} (doi:{DOIS[ep]}), HazEL hazard-service v1",
                    "geo_basis": _GEO_BASIS[ep],
                    "year_basis": _YEAR_BASIS[ep] + "; NCEI B.C. years converted to astronomical years",
                    "ncei_country_names": sorted(names[(geo, iso, y)]),
                    "records": len(grp_used),
                }
                if ep == "tsunamis":
                    flags["validity_filter"] = (
                        "eventValidity 3 or 4 (probable or definite tsunami)"
                        if kind == "count_valid34"
                        else "all records except eventValidity -1 (erroneous entry)"
                    )
                if kind.startswith("count"):
                    value = str(len(grp_used))
                    flags["aggregation"] = "count of NCEI records"
                    used = grp_used
                else:
                    vals = [(r, dec(r.data.get(fld))) for r in grp_used]  # type: ignore[arg-type]
                    used = [r for r, v in vals if v is not None]
                    if not used:
                        continue  # nothing published: the cell is omitted, never a filled zero
                    value = dec(sum(Decimal(v) for _, v in vals if v is not None))  # type: ignore[assignment]
                    flags["aggregation"] = f"sum of NCEI '{fld}' over records that publish it"
                    flags["records_with_value"] = len(used)
                    flags["records_without_value"] = len(grp_used) - len(used)
                    if fld and fld.endswith("Total"):
                        flags["definition"] = "NCEI total effects: the hazard and its secondary effects (e.g. tsunami)"
                pages = sorted({r.sha256 for r in used})
                if len(pages) > 1:
                    flags["raw_pages"] = pages
                ids = sorted(r.id for r in used)
                rows.append(
                    {
                        "source": ATLAS_SOURCE,
                        "dataset": DATASET,
                        "indicator": ind,
                        "concept_id": f"hazard_events.{ind}",
                        "iso3": iso,
                        "provider_geo": geo,
                        "entity_qid": None,
                        "year": y,
                        "value": value,
                        "value_text": None,
                        "unit": unit,
                        "flags": json.dumps(flags, sort_keys=True, ensure_ascii=False),
                        "raw_sha256": used[0].sha256,
                        "selector": json.dumps(
                            {
                                "endpoint": ENDPOINTS[ep][0],
                                "record_ids": ids[:500],
                                **({"field": fld} if fld else {}),
                                **({"record_ids_truncated": len(ids)} if len(ids) > 500 else {}),
                            },
                            sort_keys=True,
                        ),
                    }
                )
    return rows


def atlas_frame(rows: Sequence[Mapping[str, Any]]) -> Any:
    from reva.atlas.tables import empty_atlas_frame, frame_with_year_periods

    if not rows:
        return empty_atlas_frame()
    return frame_with_year_periods(rows, year_key="year")


# ============================================================================================== KG
#: dataset predicates: pid -> (value kind, reva predicate, label en, label fr)
PREDICATES: dict[str, tuple[str, str, str, str]] = {
    "ncei:location_name": ("string", "ncei_location_name", "location name (NCEI)", "nom du lieu (NCEI)"),
    "ncei:country_name": (
        "string",
        "ncei_country_name",
        "country as published by NCEI",
        "pays tel que publié par NCEI",
    ),
    "ncei:country": (
        "item",
        "ncei_country",
        "located in present-day country (NCEI country field)",
        "situé dans le pays actuel (champ pays NCEI)",
    ),
    "ncei:region_code": ("string", "ncei_region_code", "NCEI region code", "code de région NCEI"),
    "ncei:area": ("string", "ncei_area", "NCEI area (state/province code)", "zone NCEI (code d'État/province)"),
    "ncei:magnitude": ("quantity", "ncei_magnitude", "earthquake magnitude (NCEI)", "magnitude du séisme (NCEI)"),
    "ncei:focal_depth_km": (
        "quantity",
        "ncei_focal_depth_km",
        "focal depth in km (NCEI)",
        "profondeur du foyer en km (NCEI)",
    ),
    "ncei:mmi_intensity": (
        "quantity",
        "ncei_mmi_intensity",
        "maximum Modified Mercalli intensity (NCEI)",
        "intensité Mercalli modifiée maximale (NCEI)",
    ),
    "ncei:tsunami_cause": (
        "string",
        "ncei_tsunami_cause",
        "tsunami cause code (NCEI)",
        "code de cause du tsunami (NCEI)",
    ),
    "ncei:tsunami_validity": (
        "string",
        "ncei_tsunami_validity",
        "tsunami event validity code (NCEI)",
        "code de validité du tsunami (NCEI)",
    ),
    "ncei:max_water_height_m": (
        "quantity",
        "ncei_max_water_height_m",
        "maximum water height in m (NCEI)",
        "hauteur d'eau maximale en m (NCEI)",
    ),
    "ncei:runup_count": (
        "quantity",
        "ncei_runup_count",
        "number of runup records (NCEI)",
        "nombre de relevés de runup (NCEI)",
    ),
    "ncei:deposit_count": (
        "quantity",
        "ncei_deposit_count",
        "number of tsunami deposit records (NCEI)",
        "nombre de dépôts de tsunami (NCEI)",
    ),
    "ncei:tsunami_intensity": (
        "quantity",
        "ncei_tsunami_intensity",
        "tsunami intensity, Soloviev-Imamura (NCEI)",
        "intensité du tsunami, Soloviev-Imamura (NCEI)",
    ),
    "ncei:tsunami_magnitude": (
        "quantity",
        "ncei_tsunami_magnitude",
        "tsunami magnitude (NCEI)",
        "magnitude du tsunami (NCEI)",
    ),
    "ncei:warning_status": (
        "string",
        "ncei_warning_status",
        "tsunami warning status code (NCEI)",
        "code de statut d'alerte au tsunami (NCEI)",
    ),
    "ncei:oceanic_tsunami": (
        "string",
        "ncei_oceanic_tsunami",
        "oceanic tsunami flag (NCEI)",
        "indicateur de tsunami océanique (NCEI)",
    ),
    "ncei:vei": ("quantity", "ncei_vei", "volcanic explosivity index (NCEI)", "indice d'explosivité volcanique (NCEI)"),
    "ncei:eruption_agent": (
        "string",
        "ncei_eruption_agent",
        "agent of eruption deaths code (NCEI)",
        "code de l'agent des décès de l'éruption (NCEI)",
    ),
    "ncei:volcano": ("item", "ncei_volcano", "volcano (NCEI)", "volcan (NCEI)"),
    "ncei:gvp_volcano_number": (
        "external_id",
        "ncei_gvp_volcano_number",
        "Global Volcanism Program volcano number (as published by NCEI)",
        "numéro de volcan du Global Volcanism Program (publié par NCEI)",
    ),
    "ncei:previous_volcano_number": (
        "string",
        "ncei_previous_volcano_number",
        "previous (CAVW) volcano number (NCEI)",
        "ancien numéro de volcan CAVW (NCEI)",
    ),
    "ncei:volcano_location": (
        "string",
        "ncei_volcano_location",
        "volcano location/region (NCEI)",
        "localisation/région du volcan (NCEI)",
    ),
    "ncei:elevation_m": ("quantity", "ncei_elevation_m", "elevation in m (NCEI)", "altitude en m (NCEI)"),
    "ncei:morphology": ("string", "ncei_morphology", "volcano type (NCEI)", "type de volcan (NCEI)"),
    "ncei:volcano_status": ("string", "ncei_volcano_status", "volcano status (NCEI)", "statut du volcan (NCEI)"),
    "ncei:last_eruption_code": (
        "string",
        "ncei_last_eruption_code",
        "last known eruption code (NCEI)",
        "code de dernière éruption connue (NCEI)",
    ),
    "ncei:record_flags": (
        "string",
        "ncei_record_flags",
        "NCEI record flags (verbatim JSON)",
        "indicateurs de l'enregistrement NCEI (JSON verbatim)",
    ),
    "ncei:associated_tsunami": (
        "item",
        "ncei_associated_tsunami",
        "associated tsunami (NCEI link; context)",
        "tsunami associé (lien NCEI ; contexte)",
    ),
    "ncei:associated_earthquake": (
        "item",
        "ncei_associated_earthquake",
        "associated earthquake (NCEI link; context)",
        "séisme associé (lien NCEI ; contexte)",
    ),
    "ncei:associated_eruption": (
        "item",
        "ncei_associated_eruption",
        "associated volcanic eruption (NCEI link; context)",
        "éruption volcanique associée (lien NCEI ; contexte)",
    ),
    "ncei:runup_country": (
        "item",
        "ncei_runup_country",
        "runups observed in country (NCEI)",
        "runups observés dans le pays (NCEI)",
    ),
    "ncei:runup_country_name": (
        "string",
        "ncei_runup_country_name",
        "runups observed in (NCEI country name)",
        "runups observés dans (nom de pays NCEI)",
    ),
    "ncei:same_as": ("item", "ncei_same_as", "same as (shared identifier)", "identique à (identifiant partagé)"),
}
for _eff, (_pred, _unit, _desc) in EFFECTS.items():
    PREDICATES[f"ncei:{_pred}"] = (
        "quantity",
        f"ncei_{_pred}",
        f"{_pred.replace('_', ' ')} (NCEI)",
        f"{_pred.replace('_', ' ')} (NCEI)",
    )
    PREDICATES[f"ncei:{_pred}_description"] = (
        "string",
        f"ncei_{_pred}_description",
        f"{_pred.replace('_', ' ')} description code (NCEI)",
        f"code de description {_pred.replace('_', ' ')} (NCEI)",
    )
#: Wikidata properties used with their WDHARVEST definitions (registry/kg/properties.yaml).
WIKIDATA_PIDS = ("P585", "P625")


def kg_properties() -> list[Any]:
    from reva.kg.model import KgProperty

    return [
        KgProperty(pid=p, value_kind=k, reva_predicate=rp, label_en=le, label_fr=lf)
        for p, (k, rp, le, lf) in PREDICATES.items()
    ]


def ensure_kg_properties(engine: Any) -> dict[str, Any]:
    from reva.adapters.connectors.wikidata import load_allowlist
    from reva.kg.harvest import kg_properties as wd_props
    from reva.kg.model import register_properties

    wd = [p for p in wd_props(load_allowlist()) if p.pid in WIKIDATA_PIDS]
    if len(wd) != len(WIKIDATA_PIDS):
        raise ValueError(f"{WIKIDATA_PIDS} must all be in registry/kg/properties.yaml")
    return register_properties(engine, [*wd, *kg_properties()]).__dict__


def eq_qid(i: int) -> str:
    return f"{QID_NS}:earthquake:{i}"


def ts_qid(i: int) -> str:
    return f"{QID_NS}:tsunami:{i}"


def eruption_qid(i: int) -> str:
    return f"{QID_NS}:eruption:{i}"


def volcano_qid(location_id: int) -> str:
    return f"{QID_NS}:volcano:{location_id}"


class _KgBuilder:
    def __init__(self, data: HazardData, country_qids: Mapping[str, str], revision: str, resolve: Any) -> None:
        from reva.kg.model import KgEntity, KgStatement, KgTime

        self.E, self.S, self.T = KgEntity, KgStatement, KgTime
        self.data = data
        self.qids = country_qids
        self.rev = revision
        self.resolve = resolve
        self.entities: list[Any] = []
        self.statements: list[Any] = []
        self.report: Counter[str] = Counter()

    def st(
        self,
        key: str,
        subject: str,
        pid: str,
        rec: Rec,
        fields: Sequence[str],
        *,
        obj: str | None = None,
        value: Mapping[str, Any] | None = None,
        quals: Mapping[str, Any] | None = None,
    ) -> None:
        self.statements.append(
            self.S(
                subject_qid=subject,
                property_pid=pid,
                object_qid=obj,
                value=dict(value) if value else None,
                qualifiers=dict(quals or {}),
                references=(rec.ref(*fields),),
                source_key=SOURCE_KEY,
                source_revision=self.rev,
                statement_id=key,
                raw_sha256=rec.sha256,
            )
        )

    def string(
        self,
        key: str,
        subject: str,
        pid: str,
        rec: Rec,
        fld: str,
        v: Any = None,
        quals: Mapping[str, Any] | None = None,
    ) -> None:
        v = rec.data.get(fld) if v is None else v
        if v is None or (isinstance(v, str) and not v.strip()):
            return
        self.st(key, subject, pid, rec, [fld], value={"type": "string", "value": str(v)}, quals=quals)

    def quantity(
        self, key: str, subject: str, pid: str, rec: Rec, fld: str, unit: str, quals: Mapping[str, Any] | None = None
    ) -> None:
        a = dec(rec.data.get(fld))
        if a is None:
            return
        self.st(
            key,
            subject,
            pid,
            rec,
            [fld],
            value={"type": "quantity", "amount": a, "unit": unit},
            quals={"ncei_field": fld, **(quals or {})},
        )

    def common(self, prefix: str, subject: str, rec: Rec) -> None:
        iso, prec, q = event_time(rec.data)
        self.st(
            f"{prefix}:date",
            subject,
            "P585",
            rec,
            ["year", "month", "day", "hour", "minute", "second"],
            value={"type": "time", "time": iso, "precision": prec},
            quals={
                **q,
                "year_convention": "NCEI B.C. years are negative without a year 0; stored as "
                "astronomical years (year 0 = 1 BC)",
                "time_zone": "UTC (NCEI)",
            },
        )
        lat, lon = dec(rec.data.get("latitude")), dec(rec.data.get("longitude"))
        if lat is not None and lon is not None:
            self.st(
                f"{prefix}:coordinates",
                subject,
                "P625",
                rec,
                ["latitude", "longitude"],
                value={"type": "globe_coordinate", "lat": lat, "lon": lon, "globe": "earth"},
            )
        self.string(f"{prefix}:location_name", subject, "ncei:location_name", rec, "locationName")
        self.country(prefix, subject, rec, "ncei:country", "ncei:country_name")
        if rec.data.get("regionCode") is not None:
            self.string(
                f"{prefix}:region_code", subject, "ncei:region_code", rec, "regionCode", str(rec.data["regionCode"])
            )
        self.string(f"{prefix}:area", subject, "ncei:area", rec, "area")

    def country(self, prefix: str, subject: str, rec: Rec, item_pid: str, name_pid: str) -> None:
        name = rec.data.get("country")
        if not isinstance(name, str) or not name.strip():
            return
        self.string(f"{prefix}:country_name", subject, name_pid, rec, "country", name)
        iso = self.resolve(country_key(name))
        qid = self.qids.get(iso) if iso else None
        if qid:
            self.st(
                f"{prefix}:country",
                subject,
                item_pid,
                rec,
                ["country"],
                obj=qid,
                quals={
                    "ncei_country": name,
                    "crosswalk_key": iso,
                    "basis": "NCEI country name -> crosswalk entity (geo alias scheme ncei_hazards)",
                },
            )
        else:
            self.report["country_unresolved_" + ("unmapped_name" if not iso else "no_qid")] += 1

    def effects(self, prefix: str, subject: str, rec: Rec, hazard: str) -> None:
        for fld, (pred, unit, desc_key) in EFFECTS.items():
            scales = self.data.descriptors.get(desc_key, {})
            for suffix, scope in (("", f"from the {hazard}"), ("Total", f"from the {hazard} and secondary effects")):
                f, fo = fld + suffix, AMOUNT_ORDER[fld] + suffix
                q = {"scope": scope}
                self.quantity(f"{prefix}:{pred}{suffix.lower()}", subject, f"ncei:{pred}", rec, f, unit, q)
                code = rec.data.get(fo)
                if code is not None:
                    self.string(
                        f"{prefix}:{pred}{suffix.lower()}_description",
                        subject,
                        f"ncei:{pred}_description",
                        rec,
                        fo,
                        str(code),
                        {
                            **q,
                            "ncei_field": fo,
                            "description": scales.get(str(code)),
                            "scale": f"NCEI descriptor {DESCRIPTORS[desc_key]}",
                        },
                    )

    def flags(self, prefix: str, subject: str, rec: Rec, names: Sequence[str]) -> None:
        f = {k: rec.data[k] for k in names if k in rec.data}
        if f:
            self.string(f"{prefix}:flags", subject, "ncei:record_flags", rec, names[0], json.dumps(f, sort_keys=True))

    def link(self, prefix: str, subject: str, rec: Rec, fld: str, pid: str, target: str | None, exists: bool) -> None:
        if rec.data.get(fld) is None:
            return
        if not exists or target is None:
            self.report[f"link_target_not_fetched:{fld}"] += 1
            return
        self.st(
            f"{prefix}:{fld}",
            subject,
            pid,
            rec,
            [fld],
            obj=target,
            quals={"ncei_field": fld, "nature": "NCEI association between records; context, not a causal claim"},
        )

    # ---- entity kinds
    def earthquakes(self) -> None:
        tsu, vol = self.data.by_id("tsunamis"), self.data.by_id("volcanoes")
        for r in self.data.records.get("earthquakes", []):
            d, q, p = r.data, eq_qid(r.id), f"eq:{r.id}"
            loc = d.get("locationName") or d.get("country") or "?"
            self.entities.append(
                self.E(
                    qid=q,
                    kind="earthquake",
                    labels={
                        "en": f"Earthquake {date_text(d)}, {loc} (NCEI {r.id})",
                        "fr": f"Séisme {date_text(d)}, {loc} (NCEI {r.id})",
                    },
                    descriptions={
                        "en": f"Significant earthquake, NCEI/WDS Global Significant Earthquake Database id {r.id}",
                        "fr": f"Séisme significatif, base NCEI/WDS Global Significant Earthquake Database, id {r.id}",
                    },
                    source_revision=self.rev,
                )
            )
            self.common(p, q, r)
            for fld, label in MAGNITUDES.items():
                self.quantity(f"{p}:{fld}", q, "ncei:magnitude", r, fld, "1", {"scale": label})
            self.quantity(f"{p}:depth", q, "ncei:focal_depth_km", r, "eqDepth", "km")
            self.quantity(
                f"{p}:intensity", q, "ncei:mmi_intensity", r, "intensity", "1", {"scale": "Modified Mercalli Intensity"}
            )
            self.effects(p, q, r, "earthquake")
            self.flags(p, q, r, ("publish",))
            t = d.get("tsunamiEventId")
            self.link(p, q, r, "tsunamiEventId", "ncei:associated_tsunami", ts_qid(t) if t else None, t in tsu)
            v = d.get("volcanoEventId")
            self.link(p, q, r, "volcanoEventId", "ncei:associated_eruption", eruption_qid(v) if v else None, v in vol)

    def tsunamis(self) -> None:
        eqs, vol = self.data.by_id("earthquakes"), self.data.by_id("volcanoes")
        causes, validity = (
            self.data.descriptors.get("tsunami_causes", {}),
            self.data.descriptors.get("tsunami_validity", {}),
        )
        warn = self.data.descriptors.get("tsunami_warning_statuses", {})
        runups: dict[int, dict[str, list[Rec]]] = defaultdict(lambda: defaultdict(list))
        for ru in self.data.records.get("runups", []):
            t = ru.data.get("tsunamiEventId")
            ck = country_key(ru.data.get("country"))
            if t is not None and ck:
                runups[int(t)][ck].append(ru)
        for r in self.data.records.get("tsunamis", []):
            d, q, p = r.data, ts_qid(r.id), f"ts:{r.id}"
            loc = d.get("locationName") or d.get("country") or "?"
            self.entities.append(
                self.E(
                    qid=q,
                    kind="tsunami",
                    labels={
                        "en": f"Tsunami {date_text(d)}, {loc} (NCEI {r.id})",
                        "fr": f"Tsunami {date_text(d)}, {loc} (NCEI {r.id})",
                    },
                    descriptions={
                        "en": f"Tsunami source event, NCEI/WDS Global Historical Tsunami Database id {r.id}",
                        "fr": f"Événement source de tsunami, base NCEI/WDS Global Historical Tsunami Database, "
                        f"id {r.id}",
                    },
                    source_revision=self.rev,
                )
            )
            self.common(p, q, r)
            if d.get("causeCode") is not None:
                self.string(
                    f"{p}:cause",
                    q,
                    "ncei:tsunami_cause",
                    r,
                    "causeCode",
                    str(d["causeCode"]),
                    {"description": causes.get(str(d["causeCode"])), "scale": "NCEI descriptor tsunami/causes"},
                )
            if d.get("eventValidity") is not None:
                self.string(
                    f"{p}:validity",
                    q,
                    "ncei:tsunami_validity",
                    r,
                    "eventValidity",
                    str(d["eventValidity"]),
                    {"description": validity.get(str(d["eventValidity"])), "scale": "NCEI descriptor tsunami/validity"},
                )
            if d.get("warningStatusId") is not None:
                self.string(
                    f"{p}:warning",
                    q,
                    "ncei:warning_status",
                    r,
                    "warningStatusId",
                    str(d["warningStatusId"]),
                    {
                        "description": warn.get(str(d["warningStatusId"])),
                        "scale": "NCEI descriptor tsunami/warning-statuses",
                    },
                )
            self.quantity(f"{p}:max_water_height", q, "ncei:max_water_height_m", r, "maxWaterHeight", "m")
            self.quantity(f"{p}:runups", q, "ncei:runup_count", r, "numRunups", "1")
            self.quantity(f"{p}:deposits", q, "ncei:deposit_count", r, "numDeposits", "1")
            self.quantity(
                f"{p}:ts_intensity",
                q,
                "ncei:tsunami_intensity",
                r,
                "tsIntensity",
                "1",
                {"scale": "Soloviev-Imamura tsunami intensity"},
            )
            self.quantity(
                f"{p}:ts_mag_iida", q, "ncei:tsunami_magnitude", r, "tsMtIi", "1", {"scale": "Iida tsunami magnitude"}
            )
            self.quantity(
                f"{p}:ts_mag_abe", q, "ncei:tsunami_magnitude", r, "tsMtAbe", "1", {"scale": "Abe tsunami magnitude"}
            )
            if d.get("oceanicTsunami") is not None:
                self.string(
                    f"{p}:oceanic",
                    q,
                    "ncei:oceanic_tsunami",
                    r,
                    "oceanicTsunami",
                    "true" if d["oceanicTsunami"] else "false",
                )
            self.effects(p, q, r, "tsunami")
            self.flags(p, q, r, ("publish",))
            e = d.get("earthquakeEventId")
            self.link(p, q, r, "earthquakeEventId", "ncei:associated_earthquake", eq_qid(e) if e else None, e in eqs)
            v = d.get("volcanoEventId")
            self.link(p, q, r, "volcanoEventId", "ncei:associated_eruption", eruption_qid(v) if v else None, v in vol)
            for ck, rus in sorted(runups.get(r.id, {}).items()):
                deaths = [dec(x.data.get("deaths")) for x in rus]
                dv = [Decimal(x) for x in deaths if x is not None]
                quals = {
                    "runup_records": len(rus),
                    "ncei_country": sorted({str(x.data.get("country")) for x in rus}),
                    **(
                        {"runup_deaths_published_sum": dec(sum(dv)), "runup_records_with_deaths": len(dv)} if dv else {}
                    ),
                }
                first = min(rus, key=lambda x: x.id)
                iso = self.resolve(ck)
                qid = self.qids.get(iso) if iso else None
                ref = {
                    **first.ref("country", "tsunamiEventId"),
                    "selector": {**first.ref()["selector"], "record_ids": sorted(x.id for x in rus)[:200]},
                }
                k = ck.lower().replace(" ", "_")[:60]
                if qid:
                    self.statements.append(
                        self.S(
                            subject_qid=q,
                            property_pid="ncei:runup_country",
                            object_qid=qid,
                            qualifiers={**quals, "crosswalk_key": iso},
                            references=(ref,),
                            source_key=SOURCE_KEY,
                            source_revision=self.rev,
                            statement_id=f"{p}:runup_country:{iso}:{k}",
                            raw_sha256=first.sha256,
                        )
                    )
                else:
                    self.statements.append(
                        self.S(
                            subject_qid=q,
                            property_pid="ncei:runup_country_name",
                            value={"type": "string", "value": ck},
                            qualifiers=quals,
                            references=(ref,),
                            source_key=SOURCE_KEY,
                            source_revision=self.rev,
                            statement_id=f"{p}:runup_country_name:{k}",
                            raw_sha256=first.sha256,
                        )
                    )

    def volcanoes(self) -> None:
        eqs, tsu = self.data.by_id("earthquakes"), self.data.by_id("tsunamis")
        agents = self.data.descriptors.get("volcano_agents", {})
        seen: dict[int, Rec] = {}
        for r in self.data.records.get("volcanoes", []):
            d, q, p = r.data, eruption_qid(r.id), f"vo:{r.id}"
            name = d.get("name") or "?"
            self.entities.append(
                self.E(
                    qid=q,
                    kind="volcanic_eruption",
                    labels={
                        "en": f"Eruption of {name}, {date_text(d)} (NCEI {r.id})",
                        "fr": f"Éruption du {name}, {date_text(d)} (NCEI {r.id})",
                    },
                    descriptions={
                        "en": f"Significant volcanic eruption, NCEI/WDS Global Significant Volcanic Eruptions "
                        f"Database id {r.id}",
                        "fr": f"Éruption volcanique significative, base NCEI/WDS Global Significant Volcanic "
                        f"Eruptions Database, id {r.id}",
                    },
                    source_revision=self.rev,
                )
            )
            self.common(p, q, r)
            self.quantity(f"{p}:vei", q, "ncei:vei", r, "vei", "1", {"scale": "Volcanic Explosivity Index"})
            agent = d.get("agent")
            if isinstance(agent, str) and agent.strip():
                self.string(
                    f"{p}:agent",
                    q,
                    "ncei:eruption_agent",
                    r,
                    "agent",
                    agent,
                    {
                        "descriptions": {c: agents.get(c) for c in agent if c.strip()},
                        "scale": "NCEI descriptor volcano/agents (one letter per agent)",
                    },
                )
            self.effects(p, q, r, "eruption")
            self.flags(p, q, r, ("publish", "significant", "eruption"))
            loc = d.get("volcanoLocationId")
            if isinstance(loc, int):
                self.st(f"{p}:volcano", q, "ncei:volcano", r, ["volcanoLocationId"], obj=volcano_qid(loc))
                if loc not in seen or r.id > seen[loc].id:
                    seen[loc] = r
            e = d.get("earthquakeEventId")
            self.link(p, q, r, "earthquakeEventId", "ncei:associated_earthquake", eq_qid(e) if e else None, e in eqs)
            t = d.get("tsunamiEventId")
            self.link(p, q, r, "tsunamiEventId", "ncei:associated_tsunami", ts_qid(t) if t else None, t in tsu)
        for loc, r in sorted(seen.items()):
            d, q, p = r.data, volcano_qid(loc), f"volcano:{loc}"
            self.entities.append(
                self.E(
                    qid=q,
                    kind="volcano",
                    labels={
                        "en": f"{d.get('name') or 'Volcano'} (volcano, NCEI {loc})",
                        "fr": f"{d.get('name') or 'Volcan'} (volcan, NCEI {loc})",
                    },
                    descriptions={
                        "en": f"Volcano of the NCEI Significant Volcanic Eruptions Database, NCEI location id {loc}",
                        "fr": f"Volcan de la base NCEI des éruptions volcaniques significatives, id de lieu NCEI {loc}",
                    },
                    source_revision=self.rev,
                )
            )
            q_from = {"from_eruption_record": r.id}
            if d.get("volcanoLocationNewNum") is not None:
                self.st(
                    f"{p}:gvp_number",
                    q,
                    "ncei:gvp_volcano_number",
                    r,
                    ["volcanoLocationNewNum"],
                    value={"type": "external_id", "value": str(d["volcanoLocationNewNum"])},
                    quals={
                        **q_from,
                        "definition": "NCEI 'Volcano Number': Smithsonian Global Volcanism Program "
                        "(GVP) permanent unique identifier (Volcanoes of the World)",
                    },
                )
            self.string(
                f"{p}:previous_number", q, "ncei:previous_volcano_number", r, "volcanoLocationNum", quals=q_from
            )
            lat, lon = dec(d.get("latitude")), dec(d.get("longitude"))
            if lat is not None and lon is not None:
                self.st(
                    f"{p}:coordinates",
                    q,
                    "P625",
                    r,
                    ["latitude", "longitude"],
                    value={"type": "globe_coordinate", "lat": lat, "lon": lon, "globe": "earth"},
                    quals=q_from,
                )
            self.string(f"{p}:location", q, "ncei:volcano_location", r, "location", quals=q_from)
            self.country(p, q, r, "ncei:country", "ncei:country_name")
            self.quantity(f"{p}:elevation", q, "ncei:elevation_m", r, "elevation", "m", q_from)
            self.string(f"{p}:morphology", q, "ncei:morphology", r, "morphology", quals=q_from)
            self.string(f"{p}:status", q, "ncei:volcano_status", r, "status", quals=q_from)
            self.string(f"{p}:last_eruption", q, "ncei:last_eruption_code", r, "timeErupt", quals=q_from)


def kg_records(
    data: HazardData, *, country_qids: Mapping[str, str], resolve: Any = None
) -> tuple[list[Any], list[Any], dict[str, Any]]:
    """NCEI records -> ``reva.kg.model`` entities and statements (source key ``noaa.ncei_hazards``)."""
    b = _KgBuilder(data, country_qids, data.revision(), resolve or _resolve_geo)
    b.earthquakes()
    b.tsunamis()
    b.volcanoes()
    kinds = Counter(e.kind for e in b.entities)
    return b.entities, b.statements, {"entities": dict(kinds), "statements": len(b.statements), **dict(b.report)}


# ============================================================================================== causal claims
@dataclass(frozen=True)
class CodedLink:
    """One NCEI-coded trigger (neutral form of a ``reva.causal.DatasetClaim``)."""

    cause_id: str
    effect_id: str
    source_claim_id: str
    codebook_term: str
    selector: Mapping[str, Any]
    raw_sha256: str


def codebook_definitions(data: HazardData) -> dict[str, str]:
    causes = data.descriptors.get("tsunami_causes", {})
    defs = {
        f"cause_code:{c}": f'{CAUSE_CODE_HELP} {c} = "{causes[str(c)]}" (NCEI descriptor tsunami/causes)'
        for c in sorted(EQ_CAUSE_CODES | VOLCANO_CAUSE_CODES)
        if str(c) in causes
    }
    defs["source_event_link"] = (
        "The NCEI tsunami source record names its source event by the id of the NCEI Significant Earthquake "
        "Database record (field earthquakeEventId) or Significant Volcanic Eruptions Database record (field "
        "volcanoEventId); the earthquake/eruption record carries the reciprocal tsunamiEventId."
    )
    return defs


def coded_links(data: HazardData) -> tuple[list[CodedLink], dict[str, Any]]:
    """Earthquake -> tsunami and eruption -> tsunami triggers coded by NCEI (see module doc for the rules)."""
    eqs, vol = data.by_id("earthquakes"), data.by_id("volcanoes")
    out: list[CodedLink] = []
    rep: Counter[str] = Counter()
    for r in data.records.get("tsunamis", []):
        d = r.data
        code = d.get("causeCode")
        validity = d.get("eventValidity")
        for fld, target, codes, word in (
            ("earthquakeEventId", eqs, EQ_CAUSE_CODES, "earthquake"),
            ("volcanoEventId", vol, VOLCANO_CAUSE_CODES, "volcano"),
        ):
            src = d.get(fld)
            if src is None:
                continue
            if validity == -1:
                rep[f"{word}_link_skipped_erroneous_entry"] += 1
                continue
            if code not in codes:
                rep[f"{word}_link_context_only_cause_code_{code}"] += 1
                continue
            if src not in target:
                rep[f"{word}_link_target_not_fetched"] += 1
                continue
            src_rec = target[src]
            reciprocal = src_rec.data.get("tsunamiEventId") == r.id
            if not reciprocal:
                rep[f"{word}_link_not_reciprocal"] += 1
            cause = eq_qid(src) if word == "earthquake" else eruption_qid(src)
            out.append(
                CodedLink(
                    cause_id=cause,
                    effect_id=ts_qid(r.id),
                    source_claim_id=f"tsunami:{r.id}:{word}:{src}",
                    codebook_term=f"cause_code:{code}",
                    selector={
                        "endpoint": ENDPOINTS["tsunamis"][0],
                        "page": r.page,
                        "index": r.index,
                        "id": r.id,
                        "columns": {fld: src, "causeCode": code, "eventValidity": validity},
                        "reciprocal": {
                            "endpoint": ENDPOINTS[src_rec.endpoint][0],
                            "id": src,
                            "page": src_rec.page,
                            "index": src_rec.index,
                            "raw_sha256": src_rec.sha256,
                            "tsunamiEventId": src_rec.data.get("tsunamiEventId"),
                        },
                    },
                    raw_sha256=r.sha256,
                )
            )
            rep[f"{word}_to_tsunami_claims"] += 1
    return out, dict(rep)


def _causal_api() -> Any:
    """``reva.causal`` (CAUSCORE) exports, or None when the package is not there (yet)."""
    import importlib

    for name in ("reva.causal", "reva.causal.model"):
        try:
            mod = importlib.import_module(name)
        except ImportError:
            continue
        if all(hasattr(mod, n) for n in ("Codebook", "DatasetClaim", "load_dataset_claims")):
            return mod
    return None


def causal_available(engine: Any) -> bool:
    if _causal_api() is None:
        return False
    from sqlalchemy import text

    with engine.connect() as c:
        return bool(c.execute(text("SELECT to_regclass('kg.causal_claim') IS NOT NULL")).scalar_one())


def load_claims(
    engine: Any, store: Any, data: HazardData, links: Sequence[CodedLink], *, origin: str, complete: bool
) -> dict[str, Any]:
    """T1 ``coded_trigger`` claims through CAUSCORE (``reva.causal.load_dataset_claims``)."""
    api = _causal_api()
    Codebook, DatasetClaim, load_dataset_claims = api.Codebook, api.DatasetClaim, api.load_dataset_claims

    used = {link.raw_sha256 for link in links} | {str(link.selector["reciprocal"]["raw_sha256"]) for link in links}
    if "tsunami_causes" in data.descriptor_shas:  # the codebook terms are decoded from this response
        used.add(data.descriptor_shas["tsunami_causes"])
    shas = sorted(used) or sorted(data.page_shas.get("tsunamis", []))[:1]
    claims = [
        DatasetClaim(
            cause_id=link.cause_id,
            effect_id=link.effect_id,
            relation="coded_trigger",
            selector=dict(link.selector),
            raw_sha256=link.raw_sha256,
            codebook_term=link.codebook_term,
            source_claim_id=link.source_claim_id,
            tier="T1",
        )
        for link in links
    ]
    rep = load_dataset_claims(
        engine,
        store,
        source_key=SOURCE_KEY,
        dataset="tsunami_sources",
        dataset_revision=data.revision(),
        codebook=Codebook(
            name=CODEBOOK_NAME,
            version="hazel-hazard-service-v1",
            url=CODEBOOK_URL,
            definitions=codebook_definitions(data),
        ),
        raw_sha256s=shas,
        retrieved_at=data.retrieved_at,
        origin=origin,
        claims=claims,
        complete=complete,
        parameters={"connector": NAME, "parser": PARSER_VERSION, "rules": "docs/dev/v3_a_hazards.md#coded-triggers"},
    )
    out = rep.as_dict() if hasattr(rep, "as_dict") else dict(rep)
    grade = getattr(api, "grade_claims", None)
    if grade is None:
        try:
            from reva.causal.grader import grade_claims as grade
        except ImportError:
            grade = None
    if grade is not None:  # the interface lets dataset agents grade after their load (idempotent)
        g = grade(engine, origin=origin)
        out["grading"] = g.as_dict() if hasattr(g, "as_dict") else str(g)[:2000]
    return out


# ============================================================================================== KG loading
def current_keys(engine: Any, source_key: str, origin: str) -> set[str]:
    from sqlalchemy import text

    like = f"{source_key}:".replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    with engine.connect() as c:
        return set(
            c.execute(
                text("""SELECT statement_key FROM kg.statement_current
                    WHERE source_key = :s AND origin = :o AND statement_key LIKE :p"""),
                {"s": source_key, "o": origin, "p": like},
            ).scalars()
        )


def kg_available(engine: Any) -> bool:
    from sqlalchemy import text

    with engine.connect() as c:
        return bool(c.execute(text("SELECT to_regclass('kg.statement') IS NOT NULL")).scalar_one())


def load_kg(
    engine: Any,
    store: Any,
    entities: Sequence[Any],
    statements: Sequence[Any],
    *,
    source_key: str,
    raw_sha256s: Sequence[str],
    retrieved_at: datetime,
    origin: str,
    retract: bool,
    batch: int = 50_000,
) -> dict[str, Any]:
    """Load in bounded batches; on a complete run, current statements of the source that the new build no longer
    has are retracted (``removed_at_source``)."""
    from reva.kg.model import MAX_ENTITIES_PER_BATCH, MAX_RETRACTIONS_PER_BATCH, load_batch, statement_key

    gone: list[str] = []
    if retract:
        new = {statement_key(s, source_key) for s in statements}
        gone = sorted(current_keys(engine, source_key, origin) - new)
    totals: Counter[str] = Counter()
    rejected: Counter[str] = Counter()
    sample: list[Any] = []
    step = min(batch, 100_000)
    n = max(
        1,
        -(-len(statements) // step),
        -(-len(entities) // MAX_ENTITIES_PER_BATCH),
        -(-len(gone) // MAX_RETRACTIONS_PER_BATCH),
    )
    for i in range(n):
        sts = statements[i * step : (i + 1) * step]
        used = sorted({s.raw_sha256 for s in sts if s.raw_sha256}) or sorted(set(raw_sha256s))
        rep = load_batch(
            engine,
            store,
            entities=entities[i * MAX_ENTITIES_PER_BATCH : (i + 1) * MAX_ENTITIES_PER_BATCH],
            statements=sts,
            source_key=source_key,
            raw_sha256s=used,
            retrieved_at=retrieved_at,
            origin=origin,
            retract_keys=gone[i * MAX_RETRACTIONS_PER_BATCH : (i + 1) * MAX_RETRACTIONS_PER_BATCH],
            parameters={"connector": NAME, "parser": PARSER_VERSION},
        ).as_dict()
        for k in (
            "entities_inserted",
            "entities_unchanged",
            "statements_inserted",
            "statements_new_revision",
            "statements_unchanged",
            "statements_stale",
            "statements_conflicting",
            "retractions_inserted",
        ):
            totals[k] += int(rep.get(k) or 0)
        rejected.update(rep.get("rejected_counts") or {})
        sample.extend((rep.get("rejected") or [])[: 20 - len(sample)])
    return {
        **totals,
        "batches": n,
        "rejected": dict(rejected),
        "rejected_sample": sample[:20],
        "status": "loaded" if not rejected else "loaded_with_rejections",
    }


# ============================================================================================== atlas protocol
def atlas_source() -> list[Any]:
    from reva.atlas.sources import AtlasSourceSpec

    return [
        AtlasSourceSpec(
            name=NAME,
            version=VERSION,
            source_key=SOURCE_KEY,
            budget_key=SOURCE_KEY,
            atlas_source=ATLAS_SOURCE,
            provider="NOAA National Centers for Environmental Information (NCEI) / World Data Service, Global "
            "Historical Hazard Database (HazEL)",
            licence=f"{RIGHTS['licence']} ({RIGHTS['licence_url']})",
            allowed_hosts=(HOST,),
            datasets=(DATASET,),
            dataset_ids=tuple(f"doi:{d}" for d in sorted(set(DOIS.values()))),
            cadence_seconds=30 * DAY,
            planned_requests=260,
            max_response_bytes=MAX_PAGE_BYTES,
            min_interval_seconds=1.0,
            parser_version=PARSER_VERSION,
            field_mappings={
                "events": "earthquakes / tsunamis/events / volcanoes records -> ncei_hazards:<kind>:<id>",
                "time": "year/month/day (UTC); NCEI B.C. negative years -> astronomical years",
                "geo": "NCEI country name -> crosswalk via geo alias scheme ncei_hazards",
                "claims": "tsunami earthquakeEventId / volcanoEventId + causeCode -> T1 coded_trigger",
            },
            notes="Paged JSON API (<= 200 records per page): ~240 requests per full run. Options min_year/max_year "
            "(NCEI years) restrict the run to a window (fixtures, tests); a windowed run retracts nothing.",
        ),
        AtlasSourceSpec(
            name=WD_NAME,
            version=VERSION,
            source_key=WD_SOURCE_KEY,
            budget_key=WD_SOURCE_KEY,
            atlas_source=ATLAS_SOURCE,
            provider="Wikidata (WDQS), community-edited; P1886 Global Volcanism Program ID statements",
            licence="CC0-1.0 (https://www.wikidata.org/wiki/Wikidata:Licensing)",
            allowed_hosts=(WD_HOST,),
            datasets=(WD_DATASET,),
            dataset_ids=("wikidata:P1886",),
            cadence_seconds=30 * DAY,
            planned_requests=3,
            max_response_bytes=MAX_WD_BYTES,
            min_interval_seconds=2.0,
            parser_version=PARSER_VERSION,
            field_mappings={"statement": "?item p:P1886 ?statement (non-deprecated)", "code": "ps:P1886"},
            notes="KG only: ncei_hazards:volcano:<id> --ncei:same_as--> Q when the item's P1886 equals the NCEI "
            "Volcano Number (GVP); needs the NCEI volcanoes already loaded (same origin).",
        ),
    ]


def _window(run: Any) -> tuple[int, int] | None:
    opts = getattr(run, "options", None) or {}
    lo, hi = opts.get("min_year"), opts.get("max_year")
    if lo is None and hi is None:
        return None
    try:
        lo_i, hi_i = int(lo if lo is not None else -5000), int(hi if hi is not None else 9999)
    except (TypeError, ValueError) as e:
        raise _err("options min_year/max_year must be integers (NCEI years)", kind="bad_option") from e
    if lo_i > hi_i:
        raise _err("option min_year > max_year", kind="bad_option")
    return lo_i, hi_i


def resolve_country_qids(engine: Any, origin: str) -> dict[str, str]:
    from reva.adapters.connectors.atlas_cliopatria import resolve_country_qids as resolve

    return resolve(engine, origin)[0]


async def atlas_ingest(run: Any) -> dict[str, Any]:
    if run.spec.name == WD_NAME:
        return await ingest_wikidata(run)
    window = _window(run)
    data, raws = await fetch(run, window)
    for raw in raws:  # parsed and checked first: nothing archived on schema drift
        run.archive(raw)
    counts = {ep: len(v) for ep, v in data.records.items()}
    res: dict[str, Any] = {
        "records": counts,
        "window": list(window) if window else None,
        "revision": data.revision(),
        "archived_raw": len(raws),
    }
    if run.wants(DATASET):
        rows = country_year_rows(data)
        r = run.register(
            dataset=DATASET,
            frame=atlas_frame(rows),
            raw_sha256s=sorted({row["raw_sha256"] for row in rows}) or data.all_shas()[:1],
            geo_scheme=GEO_SCHEME,
            rights=RIGHTS,
            coverage_note=(
                "COMPUTED by REVA from NCEI/WDS Global Historical Hazard Database records (significant earthquakes "
                "2150 BC-, tsunami sources and runups 2000 BC-, significant eruptions 4360 BC-): per NCEI country and "
                "event year, record counts and sums of published impacts, separately per database; only "
                "country-years with >= 1 record; sums only over records that publish the value"
                + (f"; WINDOW NCEI years {window[0]}..{window[1]}" if window else "")
            ),
        )
        res["atlas"] = {
            k: r[k] for k in ("dataset_key", "row_count", "created", "artifact_sha256", "unmapped_provider_geo")
        }
    links, link_report = coded_links(data)
    res["coded_links"] = link_report
    if run.dataset is None and kg_available(run.engine):
        qids = resolve_country_qids(run.engine, run.origin)
        ents, sts, kg_rep = kg_records(data, country_qids=qids)
        ensure_kg_properties(run.engine)
        res["kg_build"] = kg_rep
        res["kg"] = load_kg(
            run.engine,
            run.store,
            ents,
            sts,
            source_key=SOURCE_KEY,
            raw_sha256s=data.all_shas(),
            retrieved_at=data.retrieved_at,
            origin=run.origin,
            retract=window is None,
        )
        if causal_available(run.engine):
            res["causal"] = load_claims(run.engine, run.store, data, links, origin=run.origin, complete=window is None)
        else:
            res["causal"] = {
                "status": "unavailable",
                "reason": "reva.causal / kg.causal_claim not present",
                "claims_built": len(links),
            }
    return res


# ============================================================================================== Wikidata xref
WD_QUERY = """SELECT ?item ?statement ?code ?rank WHERE {
  ?item p:P1886 ?statement .
  ?statement ps:P1886 ?code ;
             wikibase:rank ?rank .
  FILTER(?rank != wikibase:DeprecatedRank)
}"""
WD_PREFIX = "gvp_xref:"
RANKS = {"http://wikiba.se/ontology#PreferredRank": "preferred", "http://wikiba.se/ontology#NormalRank": "normal"}


def known_gvp_numbers(engine: Any, origin: str) -> dict[str, list[str]]:
    """GVP number -> NCEI volcano entities already in the KG (same origin)."""
    from sqlalchemy import text

    with engine.connect() as c:
        rows = c.execute(
            text("""SELECT subject_qid, value->>'value' FROM kg.statement_current
                    WHERE source_key = :s AND origin = :o AND property_pid = 'ncei:gvp_volcano_number'"""),
            {"s": SOURCE_KEY, "o": origin},
        ).all()
    out: dict[str, list[str]] = defaultdict(list)
    for subj, code in rows:
        if code:
            out[str(code)].append(subj)
    return dict(out)


def wikidata_statements(
    bindings: Sequence[Mapping[str, Any]],
    *,
    gvp: Mapping[str, Sequence[str]],
    raw_sha256: str,
    retrieved_at: datetime,
    query_sha256: str,
) -> tuple[list[Any], dict[str, Any]]:
    """WDQS P1886 rows -> ``ncei_hazards:volcano:<id> --ncei:same_as--> Q`` for GVP numbers NCEI publishes. A number
    carried by several Wikidata items (or by several NCEI volcanoes) is ambiguous: no statement, reported."""
    from reva.adapters.connectors.wikidata import qid_from_uri, statement_id_from_uri
    from reva.kg.model import KgStatement

    counters: Counter[str] = Counter()
    by_code: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    for b in bindings:
        qid = qid_from_uri((b.get("item") or {}).get("value"))
        sid = statement_id_from_uri((b.get("statement") or {}).get("value"))
        code = str((b.get("code") or {}).get("value") or "").strip()
        rank = RANKS.get(str((b.get("rank") or {}).get("value") or ""))
        if not qid or not sid or rank is None or not code:
            counters["row_incomplete_or_deprecated"] += 1
            continue
        by_code[code].append((qid, sid, rank))
    out: list[Any] = []
    revision = f"wdqs@{retrieved_at.strftime('%Y-%m-%dT%H:%M:%SZ')}"
    for code, subjects in sorted(gvp.items()):
        hits = by_code.get(code, [])
        items = sorted({h[0] for h in hits})
        if not items:
            counters["ncei_gvp_number_not_on_wikidata"] += 1
            continue
        if len(items) > 1:
            counters["gvp_number_on_several_items"] += 1
            continue
        if len(subjects) > 1:
            counters["gvp_number_on_several_ncei_volcanoes"] += 1
            continue
        qid, sid, rank = min(hits, key=lambda h: h[1])
        out.append(
            KgStatement(
                subject_qid=subjects[0],
                property_pid="ncei:same_as",
                object_qid=qid,
                qualifiers={
                    "evidence": "shared identifier: NCEI 'Volcano Number' (GVP volcano number) = Wikidata P1886 "
                    "(Global Volcanism Program ID)",
                    "gvp_volcano_number": code,
                    "wikidata_property": "P1886",
                    "wikidata_statement_id": sid,
                    "wikidata_rank": rank,
                    "_revision": {
                        "wikidata_revision": "unknown",
                        "reason": "WDQS rows carry no entity revision",
                        "retrieved_at": retrieved_at.isoformat(),
                    },
                },
                references=[{"stated_in": "Wikidata (P1886)", "query_sha256": query_sha256}],
                statement_id=f"{WD_PREFIX}{subjects[0].rsplit(':', 1)[1]}:{qid}",
                source_revision=revision,
                raw_sha256=raw_sha256,
            )
        )
        counters["same_as"] += 1
    return out, {
        "rows": len(bindings),
        "gvp_numbers_on_wikidata": len(by_code),
        "ncei_gvp_numbers": len(gvp),
        **dict(counters),
    }


async def ingest_wikidata(run: Any) -> dict[str, Any]:
    import tempfile
    from pathlib import Path

    from reva.adapters.connectors.wikidata import Archive, MachineLock, WikidataClient

    if not run.wants(WD_DATASET):
        return {"skipped": True}
    gvp = known_gvp_numbers(run.engine, run.origin) if kg_available(run.engine) else {}
    if not gvp:
        return {"skipped": True, "reason": "no NCEI volcano with a GVP number in the KG (run atlas.noaa_hazards first)"}
    with tempfile.TemporaryDirectory(prefix="reva-hazards-lock-") as tmp:
        lock = None
        if run.origin != "live":  # fixture replay: never wait on the machine-wide Wikidata cool-down
            lock = MachineLock(Path(tmp), min_interval=0.0)
        client = WikidataClient(run.http, archive=Archive(run.store), lock=lock, max_retries=6)
        res = await client.sparql(WD_QUERY, meta={"purpose": "gvp_xref", "connector": WD_NAME})
    sts, report = wikidata_statements(
        res.bindings, gvp=gvp, raw_sha256=res.raw_sha256, retrieved_at=res.retrieved_at, query_sha256=res.query_sha256
    )
    ensure_kg_properties(run.engine)
    from reva.kg.model import MAX_RETRACTIONS_PER_BATCH, load_batch, statement_key

    new = {statement_key(s, WD_SOURCE_KEY) for s in sts}
    gone = sorted(
        k
        for k in current_keys(run.engine, WD_SOURCE_KEY, run.origin)
        if k.startswith(f"{WD_SOURCE_KEY}:{WD_PREFIX}") and k not in new
    )[:MAX_RETRACTIONS_PER_BATCH]
    rep = load_batch(
        run.engine,
        run.store,
        entities=[],
        statements=sts,
        source_key=WD_SOURCE_KEY,
        raw_sha256s=[res.raw_sha256],
        retrieved_at=res.retrieved_at,
        origin=run.origin,
        retract_keys=gone,
        parameters={"connector": WD_NAME, "parser": PARSER_VERSION, "query_sha256": res.query_sha256},
    ).as_dict()
    return {
        "raw_sha256": res.raw_sha256,
        "query_sha256": res.query_sha256,
        "build": report,
        "kg": {
            k: rep.get(k)
            for k in ("statements_inserted", "statements_unchanged", "retractions_inserted", "rejected_counts")
        },
    }


def unmapped_names(data: HazardData, resolve: Any = None) -> dict[str, int]:
    """NCEI country names (normalised) that do not map to a crosswalk entity, with record counts."""
    resolve = resolve or _resolve_geo
    c: Counter[str] = Counter()
    for recs in data.records.values():
        for r in recs:
            ck = country_key(r.data.get("country"))
            if ck and resolve(ck) is None:
                c[ck] += 1
    return dict(sorted(c.items()))
