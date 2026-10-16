"""GeoNames cities -> atlas tables + KG links (V3, agent A-CITIES).

Source: GeoNames daily dump (``https://download.geonames.org/export/dump/``, licence CC BY 4.0, attribution
"GeoNames, www.geonames.org"):

* ``cities15000.zip`` (~3.4 MB): "all cities with a population > 15000 or capitals" (GeoNames readme), one
  row per populated place in the 19-column ``geoname`` layout;
* ``admin1CodesASCII.txt`` (~150 kB): first-order administrative divisions (code, name, ASCII name, geonameid);
* ``alternateNamesV2.zip`` (~205 MB, **only on request**: ``--dataset cities15000_names`` or
  ``--option altnames=1``): language-tagged alternate names, of which only ``fr``/``en`` names of the
  cities15000 places are kept (at most :data:`MAX_NAMES_PER_LANG` per place and language).

Atlas tables (atlas rows are per country: ``provider_geo`` is the GeoNames country code verbatim, mapped with
the ``geonames_cc`` scheme = ISO 3166-1 alpha-2 + ``XK`` -> ``XKX``; the place is folded into the indicator,
``population|geonameid=2267057``, exactly like other agents fold a counterpart country):

* ``geonames.cities15000`` — per place: name, ASCII name, feature code (PPLC capital, PPLA seat of a first-order
  division, PPLX section of a place, PPLH historical ...), admin-1 code (+ name from admin1CodesASCII), latitude,
  longitude, population, elevation, DEM elevation, timezone, alternate country codes, GeoNames modification
  date. Every value is what GeoNames states. GeoNames does not date its figures: rows carry the one-day
  retrieval snapshot as period (``flags.period_basis``), and the place's own ``modification date`` (last edit of
  the record, not the date of the population figure) is kept in the population flags. A population of ``0``
  (GeoNames: not known) and DEM ``-9999`` (no data) are omitted, never stored as numbers.
* ``geonames.cities15000_by_country`` — exact per-country derivations from that one file: number of places with
  population > 15000 (all feature codes, and without sections/historical/abandoned places), largest place and
  its population, and the PPLC place(s). Several PPLC places are all listed (never one picked); a capital
  population is given only when the country has exactly one PPLC place.
* ``geonames.admin1`` — admin-1 divisions per country (name, ASCII name, geonameid) and their count.
* ``geonames.cities15000_names`` — bounded fr/en alternate names (preferred, short, colloquial, historic
  flags as GeoNames states them).

KG link (``registry/kg/queries/places/``): a place is linked to a Wikidata item only through **Wikidata's own**
GeoNames ID statements (P1566), harvested with WDHARVEST's locked, budget-metered client
(:func:`harvest_wikidata`, CLI ``python -m reva.adapters.connectors.atlas_cities wikidata --db <throwaway>``).
A geonameid held by exactly one item is mapped; held by several items it is reported as ambiguous and not
used; nothing is ever matched by name. GeoNames-sourced statements (source ``geonames.cities``) are then
loaded on the mapped items: P17 country, P1376 capital of (PPLC only), P131 admin-1 division (when the
division's own geonameid is mapped), P1082 population (no date: GeoNames does not date it). Country QIDs
come from ``registry/entities/country_qids.json`` (WDHARVEST); without it the country-valued statements are
reported as blocked. :func:`capital_crosscheck` compares GeoNames PPLC places with Wikidata P36 (capital)
and reports every disagreement (several or disputed capitals included) without choosing one.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import math
import zipfile
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from reva.adapters.connectors.atlas_geo_common import snapshot_period
from reva.adapters.connectors.base import dec_str, module_digest
from reva.atlas.sources import AtlasSourceSpec
from reva.kernel.errors import SourceResponseError

log = logging.getLogger("reva.atlas.cities")

CONNECTOR_NAME = "atlas.geonames_cities"
CONNECTOR_VERSION = "1.0.0"
SOURCE_KEY = "geonames.cities"
PARSER_VERSION = "atlas-geonames-cities/1"
ATLAS_SOURCE = "geonames"
DS_CITIES = "cities15000"
DS_COUNTRY = "cities15000_by_country"
DS_ADMIN1 = "admin1"
DS_NAMES = "cities15000_names"
DATASETS = (DS_CITIES, DS_COUNTRY, DS_ADMIN1, DS_NAMES)
GEO_SCHEME = "geonames_cc"

HOST = "download.geonames.org"
BASE = f"https://{HOST}/export/dump"
URL_CITIES = f"{BASE}/cities15000.zip"
URL_ADMIN1 = f"{BASE}/admin1CodesASCII.txt"
URL_ALTNAMES = f"{BASE}/alternateNamesV2.zip"
CITIES_MEMBER = "cities15000.txt"
ALTNAMES_MEMBER = "alternateNamesV2.txt"
MAX_CITIES_BYTES = 20_000_000
MAX_ADMIN1_BYTES = 2_000_000
MAX_ALTNAMES_BYTES = 300_000_000
MAX_UNZIPPED = 2_000_000_000  # alternateNamesV2.txt is ~0.7 GB; streamed, never held in memory
MIN_CITIES = 20_000  # a truncated/changed file is refused, not registered
MIN_ADMIN1 = 3_000
THRESHOLD = 15_000  # readme: "all cities with a population > 15000 or capitals"
NAME_LANGS = ("fr", "en")
MAX_NAMES_PER_LANG = 5
#: feature codes left out of the "places without sections/historical" count and of "largest place"
#: (GeoNames featureCodes: PPLX section of populated place, PPLH historical, PPLQ abandoned, PPLW destroyed,
#: PPLCH historical capital)
NOT_A_CURRENT_PLACE = frozenset({"PPLX", "PPLH", "PPLQ", "PPLW", "PPLCH"})

RIGHTS = {
    "licence": "CC BY 4.0",
    "licence_url": "https://creativecommons.org/licenses/by/4.0/",
    "attribution": "GeoNames (www.geonames.org): cities15000, admin1CodesASCII, alternateNamesV2",
}

GEONAME_COLUMNS = (
    "geonameid",
    "name",
    "asciiname",
    "alternatenames",
    "latitude",
    "longitude",
    "feature class",
    "feature code",
    "country code",
    "cc2",
    "admin1 code",
    "admin2 code",
    "admin3 code",
    "admin4 code",
    "population",
    "elevation",
    "dem",
    "timezone",
    "modification date",
)
#: column -> (indicator, unit) for numeric cells
NUMERIC = {
    "latitude": ("latitude", "degrees (WGS84)"),
    "longitude": ("longitude", "degrees (WGS84)"),
    "population": ("population", "persons"),
    "elevation": ("elevation", "m"),
    "dem": ("dem_elevation", "m"),
}
#: column -> indicator for text cells
TEXT = {
    "name": "name",
    "asciiname": "asciiname",
    "feature code": "feature_code",
    "admin1 code": "admin1_code",
    "timezone": "timezone",
    "cc2": "cc2",
    "modification date": "modification_date",
}
BASE_FLAGS = {"as_declared_by": "GeoNames", "period_basis": "retrieval snapshot: GeoNames does not date its figures"}


class GeoNamesCitiesError(SourceResponseError):
    pass


def _err(msg: str) -> GeoNamesCitiesError:
    return GeoNamesCitiesError(msg, retryable=False, kind="schema_drift")


def _canon(obj: Mapping[str, Any]) -> str:
    return json.dumps(dict(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _num(s: str) -> str | None:
    s = s.strip()
    if not s:
        return None
    try:
        d = Decimal(s)
    except InvalidOperation as e:
        raise _err(f"not a number: {s[:40]!r}") from e
    if not d.is_finite():
        raise _err(f"not a finite number: {s[:40]!r}")
    return dec_str(d)


# --------------------------------------------------------------------------------------------- parsing
@dataclass(frozen=True)
class Place:
    line: int  # 1-based line in cities15000.txt
    cells: dict[str, str]

    @property
    def geonameid(self) -> str:
        return self.cells["geonameid"]

    @property
    def country(self) -> str:
        return self.cells["country code"]

    @property
    def feature_code(self) -> str:
        return self.cells["feature code"]

    @property
    def population(self) -> int:
        p = self.cells["population"].strip()
        return int(p) if p.isdigit() else 0

    @property
    def admin1_key(self) -> str | None:
        a = self.cells["admin1 code"].strip()
        return f"{self.country}.{a}" if a else None


@dataclass(frozen=True)
class Admin1:
    line: int
    code: str  # "PT.14"
    name: str
    asciiname: str
    geonameid: str

    @property
    def country(self) -> str:
        return self.code.split(".", 1)[0]


def unzip_member(body: bytes, member: str, *, limit: int) -> bytes:
    try:
        zf = zipfile.ZipFile(io.BytesIO(body))
    except zipfile.BadZipFile as e:
        raise _err("not a zip archive") from e
    with zf:
        names = zf.namelist()
        if member not in names:
            raise _err(f"zip has no {member} (members: {names[:10]})")
        info = zf.getinfo(member)
        if info.file_size > limit:
            raise _err(f"{member} is {info.file_size} bytes uncompressed (limit {limit})")
        with zf.open(member) as fh:
            data = fh.read(limit + 1)
    if len(data) > limit:
        raise _err(f"{member} exceeds {limit} bytes uncompressed")
    return data


def parse_cities(body: bytes, *, member: str = CITIES_MEMBER) -> list[Place]:
    """``cities15000.zip`` -> places (the 19-column ``geoname`` layout of the GeoNames readme)."""
    data = unzip_member(body, member, limit=MAX_CITIES_BYTES * 10)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise _err(f"{member} is not UTF-8") from e
    out: list[Place] = []
    seen: set[str] = set()
    for n, line in enumerate(text.split("\n"), start=1):
        if not line.strip():
            continue
        cells = line.rstrip("\r").split("\t")
        if len(cells) != len(GEONAME_COLUMNS):
            raise _err(f"{member} line {n}: {len(cells)} columns, expected {len(GEONAME_COLUMNS)}")
        p = Place(n, dict(zip(GEONAME_COLUMNS, cells, strict=True)))
        if not p.geonameid.isdigit():
            raise _err(f"{member} line {n}: geonameid {p.geonameid[:20]!r} is not numeric")
        if p.geonameid in seen:
            raise _err(f"{member} line {n}: duplicate geonameid {p.geonameid}")
        seen.add(p.geonameid)
        out.append(p)
    return out


def validate_cities(places: Sequence[Place], *, min_rows: int = MIN_CITIES) -> list[str]:
    problems = []
    if len(places) < min_rows:
        problems.append(f"only {len(places)} places (expected >= {min_rows})")
    for p in places:
        if len(p.country) != 2 or not p.country.isalpha():
            problems.append(f"line {p.line}: bad country code {p.country!r}")
        if p.cells["feature class"] != "P":
            problems.append(f"line {p.line}: feature class {p.cells['feature class']!r} is not P")
        try:
            lat, lon = float(p.cells["latitude"]), float(p.cells["longitude"])
        except ValueError:
            problems.append(f"line {p.line}: coordinates are not numbers")
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            problems.append(f"line {p.line}: coordinates out of range")
        if len(problems) > 50:
            break
    return problems


def parse_admin1(body: bytes) -> list[Admin1]:
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as e:
        raise _err("admin1CodesASCII.txt is not UTF-8") from e
    out: list[Admin1] = []
    seen: set[str] = set()
    for n, line in enumerate(text.split("\n"), start=1):
        if not line.strip():
            continue
        cells = line.rstrip("\r").split("\t")
        if len(cells) != 4:
            raise _err(f"admin1CodesASCII.txt line {n}: {len(cells)} columns, expected 4")
        code, name, ascii_, gid = cells
        if "." not in code or not gid.isdigit():
            raise _err(f"admin1CodesASCII.txt line {n}: unexpected code/geonameid {code[:20]!r}/{gid[:20]!r}")
        if code in seen:
            raise _err(f"admin1CodesASCII.txt line {n}: duplicate code {code}")
        seen.add(code)
        out.append(Admin1(n, code, name, ascii_, gid))
    return out


@dataclass(frozen=True)
class AltName:
    line: int
    alternate_name_id: str
    geonameid: str
    lang: str
    name: str
    preferred: bool
    short: bool
    colloquial: bool
    historic: bool
    from_: str
    to: str


def iter_altnames(
    body: bytes, geonameids: set[str], *, langs: Sequence[str] = NAME_LANGS, member: str = ALTNAMES_MEMBER
) -> Iterator[AltName]:
    """Stream ``alternateNamesV2.zip`` and yield the ``langs`` names of ``geonameids`` (the ~0.7 GB text
    member is read line by line, never held in memory)."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(body))
    except zipfile.BadZipFile as e:
        raise _err("alternateNamesV2.zip is not a zip archive") from e
    want = set(langs)
    with zf:
        if member not in zf.namelist():
            raise _err(f"zip has no {member} (members: {zf.namelist()[:10]})")
        if zf.getinfo(member).file_size > MAX_UNZIPPED:
            raise _err(f"{member} exceeds {MAX_UNZIPPED} bytes uncompressed")
        with zf.open(member) as fh:
            for n, raw in enumerate(io.TextIOWrapper(fh, encoding="utf-8", newline="\n"), start=1):
                parts = raw.rstrip("\n").rstrip("\r").split("\t")
                if len(parts) < 4:
                    if raw.strip():
                        raise _err(f"{member} line {n}: {len(parts)} columns")
                    continue
                if parts[2] not in want or parts[1] not in geonameids:
                    continue
                if len(parts) != 10:
                    raise _err(f"{member} line {n}: {len(parts)} columns, expected 10 (alternateNamesV2 layout)")
                yield AltName(
                    n,
                    parts[0],
                    parts[1],
                    parts[2],
                    parts[3],
                    parts[4] == "1",
                    parts[5] == "1",
                    parts[6] == "1",
                    parts[7] == "1",
                    parts[8],
                    parts[9],
                )


def bounded_names(names: Iterable[AltName], *, per_lang: int = MAX_NAMES_PER_LANG) -> tuple[list[AltName], int]:
    """At most ``per_lang`` names per (place, language): preferred first, then short, then the others;
    colloquial and historic names last; ties by alternateNameId. Returns (kept, dropped count)."""
    groups: dict[tuple[str, str], list[AltName]] = defaultdict(list)
    for a in names:
        groups[(a.geonameid, a.lang)].append(a)
    kept: list[AltName] = []
    dropped = 0
    for key in sorted(groups):
        g = sorted(
            groups[key],
            key=lambda a: (not a.preferred, not a.short, a.colloquial, a.historic, int(a.alternate_name_id)),
        )
        kept.extend(g[:per_lang])
        dropped += max(0, len(g) - per_lang)
    return kept, dropped


# --------------------------------------------------------------------------------------------- atlas rows
def _geo_iso3(code: str) -> str | None:
    from reva.atlas.tables import country_iso3

    return country_iso3(code, GEO_SCHEME)


def _row(
    *,
    dataset: str,
    indicator: str,
    geo: str,
    period: tuple[Any, Any],
    value: str | None,
    text: str | None,
    unit: str,
    flags: Mapping[str, Any],
    raw_sha256: str,
    selector: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "source": ATLAS_SOURCE,
        "dataset": dataset,
        "indicator": indicator,
        "concept_id": None,
        "iso3": _geo_iso3(geo),
        "provider_geo": geo,
        "entity_qid": None,
        "period_start": period[0],
        "period_end": period[1],
        "value": value,
        "value_text": text,
        "unit": unit,
        "flags": _canon(flags),
        "raw_sha256": raw_sha256,
        "selector": _canon(selector),
    }


def city_rows(
    places: Sequence[Place],
    admin1: Mapping[str, Admin1],
    *,
    raw_sha256: str,
    retrieved_at: datetime,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Atlas rows of ``geonames.cities15000`` and counts of omitted cells (by reason)."""
    period = snapshot_period(retrieved_at)
    out: list[dict[str, Any]] = []
    omitted: Counter[str] = Counter()
    for p in places:
        gid = p.geonameid
        base = {**BASE_FLAGS, "geonameid": gid, "feature_code": p.feature_code}

        def sel(col: str, p: Place = p) -> dict[str, Any]:
            return {"file": CITIES_MEMBER, "line": p.line, "column": col}

        for col, (ind, unit) in NUMERIC.items():
            v = _num(p.cells[col])
            if v is None:
                omitted[f"{ind}_empty"] += 1
                continue
            flags: dict[str, Any] = dict(base)
            if col == "population":
                if v == "0":
                    omitted["population_zero_unknown"] += 1  # GeoNames 0 = not known: never a number
                    continue
                flags["geonames_modification_date"] = p.cells["modification date"]
                flags["date_note"] = "GeoNames states no date for the figure; modification date = last record edit"
            if col == "dem":
                if v == "-9999":
                    omitted["dem_no_data"] += 1
                    continue
                flags["dem_note"] = "GeoNames 'dem': digital elevation model value (srtm3 or gtopo30), not surveyed"
            out.append(
                _row(
                    dataset=DS_CITIES,
                    indicator=f"{ind}|geonameid={gid}",
                    geo=p.country,
                    period=period,
                    value=v,
                    text=None,
                    unit=unit,
                    flags=flags,
                    raw_sha256=raw_sha256,
                    selector=sel(col),
                )
            )
        for col, ind in TEXT.items():
            raw = p.cells[col]
            t = raw.strip()
            if not t:
                continue
            flags = dict(base)
            if t != raw:
                flags["whitespace_trimmed"] = True
            if col == "admin1 code":
                a = admin1.get(f"{p.country}.{t}")
                if a is not None:
                    flags.update(admin1_name=a.name, admin1_geonameid=a.geonameid)
                else:
                    flags["admin1_not_in_admin1CodesASCII"] = True
            out.append(
                _row(
                    dataset=DS_CITIES,
                    indicator=f"{ind}|geonameid={gid}",
                    geo=p.country,
                    period=period,
                    value=None,
                    text=t[:4000],
                    unit="1",
                    flags=flags,
                    raw_sha256=raw_sha256,
                    selector=sel(col),
                )
            )
    return out, dict(omitted)


def country_aggregates(places: Sequence[Place]) -> dict[str, dict[str, Any]]:
    """Exact per-country derivations from cities15000 (no external data)."""
    by_cc: dict[str, list[Place]] = defaultdict(list)
    for p in places:
        by_cc[p.country].append(p)
    out: dict[str, dict[str, Any]] = {}
    for cc, ps in sorted(by_cc.items()):
        over = [p for p in ps if p.population > THRESHOLD]
        current = [p for p in over if p.feature_code not in NOT_A_CURRENT_PLACE]
        cand = [p for p in ps if p.feature_code not in NOT_A_CURRENT_PLACE and p.population > 0]
        top = max((p.population for p in cand), default=0)
        largest = sorted((p for p in cand if p.population == top), key=lambda p: int(p.geonameid)) if top else []
        pplc = sorted((p for p in ps if p.feature_code == "PPLC"), key=lambda p: int(p.geonameid))
        out[cc] = {
            "places": len(ps),
            "over_threshold": over,
            "over_threshold_current": current,
            "largest": largest,
            "largest_population": top,
            "pplc": pplc,
            "feature_codes": dict(Counter(p.feature_code for p in over)),
        }
    return out


def country_rows(places: Sequence[Place], *, raw_sha256: str, retrieved_at: datetime) -> list[dict[str, Any]]:
    period = snapshot_period(retrieved_at)
    out: list[dict[str, Any]] = []
    sel_all = {"file": CITIES_MEMBER, "derivation": "all rows of the country"}
    base = {**BASE_FLAGS, "derived_from": "cities15000.txt (exact count/selection, no estimate)"}
    for cc, a in country_aggregates(places).items():

        def add(
            ind: str,
            value: str | None,
            text: str | None,
            unit: str,
            flags: Mapping[str, Any],
            lines: Sequence[int] | None = None,
            cc: str = cc,
        ) -> None:
            sel = dict(sel_all) if lines is None else {"file": CITIES_MEMBER, "lines": list(lines)[:50]}
            out.append(
                _row(
                    dataset=DS_COUNTRY,
                    indicator=ind,
                    geo=cc,
                    period=period,
                    value=value,
                    text=text,
                    unit=unit,
                    flags={**base, **flags},
                    raw_sha256=raw_sha256,
                    selector=sel,
                )
            )

        add(
            "places_pop_gt_15000|scope=all_ppl",
            str(len(a["over_threshold"])),
            None,
            "places",
            {
                "definition": "places in cities15000 with GeoNames population > 15000 (any P feature code, incl. "
                "PPLX sections of a larger place)",
                "feature_codes": a["feature_codes"],
            },
        )
        add(
            "places_pop_gt_15000|scope=excl_sections_historical",
            str(len(a["over_threshold_current"])),
            None,
            "places",
            {
                "definition": "same, without PPLX (section of a place), PPLH, PPLQ, PPLW, PPLCH",
                "excluded_feature_codes": sorted(NOT_A_CURRENT_PLACE),
            },
        )
        if a["largest"]:
            names = [p.cells["name"] for p in a["largest"]]
            info = {
                "geonameids": [p.geonameid for p in a["largest"]],
                "feature_codes": [p.feature_code for p in a["largest"]],
                "scope": "excl_sections_historical",
            }
            if len(names) > 1:
                info["tie"] = True
            lines = [p.line for p in a["largest"]]
            add(
                "largest_place",
                None,
                " | ".join(names),
                "1",
                {**info, "population": str(a["largest_population"])},
                lines,
            )
            add(
                "largest_place_population",
                str(a["largest_population"]),
                None,
                "persons",
                {**info, "names": names},
                lines,
            )
        pplc = a["pplc"]
        add(
            "capital_pplc_count",
            str(len(pplc)),
            None,
            "places",
            {"definition": "places with GeoNames feature code PPLC (capital of a political entity)"},
        )
        if pplc:
            info = {"geonameids": [p.geonameid for p in pplc]}
            if len(pplc) > 1:
                info["multiple_pplc"] = True
                info["note"] = "GeoNames marks several places PPLC for this country; all listed, none chosen"
            add("capital_pplc", None, " | ".join(p.cells["name"] for p in pplc), "1", info, [p.line for p in pplc])
            if len(pplc) == 1 and pplc[0].population > 0:
                add(
                    "capital_pplc_population",
                    str(pplc[0].population),
                    None,
                    "persons",
                    {
                        **info,
                        "name": pplc[0].cells["name"],
                        "geonames_modification_date": pplc[0].cells["modification date"],
                    },
                    [pplc[0].line],
                )
    return out


def admin1_rows(rows: Sequence[Admin1], *, raw_sha256: str, retrieved_at: datetime) -> list[dict[str, Any]]:
    period = snapshot_period(retrieved_at)
    out: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    for a in rows:
        counts[a.country] += 1
        for col, ind, txt in (
            ("name", "admin1_name", a.name),
            ("asciiname", "admin1_asciiname", a.asciiname),
            ("geonameid", "admin1_geonameid", a.geonameid),
        ):
            if not txt.strip():
                continue
            out.append(
                _row(
                    dataset=DS_ADMIN1,
                    indicator=f"{ind}|code={a.code}",
                    geo=a.country,
                    period=period,
                    value=None,
                    text=txt.strip(),
                    unit="1",
                    flags={**BASE_FLAGS, "admin1_code": a.code},
                    raw_sha256=raw_sha256,
                    selector={"file": "admin1CodesASCII.txt", "line": a.line, "column": col},
                )
            )
    for cc, n in sorted(counts.items()):
        out.append(
            _row(
                dataset=DS_ADMIN1,
                indicator="admin1_count",
                geo=cc,
                period=period,
                value=str(n),
                text=None,
                unit="divisions",
                flags={**BASE_FLAGS, "derived_from": "admin1CodesASCII.txt (exact count)"},
                raw_sha256=raw_sha256,
                selector={"file": "admin1CodesASCII.txt", "derivation": "rows of country"},
            )
        )
    return out


def name_rows(
    names: Sequence[AltName],
    country_of: Mapping[str, str],
    *,
    raw_sha256: str,
    retrieved_at: datetime,
    totals: Mapping[tuple[str, str], int],
) -> list[dict[str, Any]]:
    period = snapshot_period(retrieved_at)
    out = []
    for a in names:
        flags = {
            **BASE_FLAGS,
            "geonameid": a.geonameid,
            "lang": a.lang,
            "names_in_language": totals.get((a.geonameid, a.lang), 1),
            "kept_at_most": MAX_NAMES_PER_LANG,
        }
        for k, v in (
            ("preferred", a.preferred),
            ("short", a.short),
            ("colloquial", a.colloquial),
            ("historic", a.historic),
        ):
            if v:
                flags[k] = True
        if a.from_:
            flags["from"] = a.from_
        if a.to:
            flags["to"] = a.to
        out.append(
            _row(
                dataset=DS_NAMES,
                indicator=f"altname|geonameid={a.geonameid}|lang={a.lang}|id={a.alternate_name_id}",
                geo=country_of[a.geonameid],
                period=period,
                value=None,
                text=a.name[:4000],
                unit="1",
                flags=flags,
                raw_sha256=raw_sha256,
                selector={"file": ALTNAMES_MEMBER, "line": a.line, "column": "alternate name"},
            )
        )
    return out


def _frame(rows: list[dict[str, Any]]) -> Any:
    import polars as pl

    from reva.atlas.tables import ATLAS_SCHEMA

    return pl.DataFrame(rows, schema=ATLAS_SCHEMA)


# --------------------------------------------------------------------------------------------- source spec
def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=CONNECTOR_NAME,
        version=CONNECTOR_VERSION,
        source_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="GeoNames",
        licence=RIGHTS["licence"],
        allowed_hosts=(HOST,),
        datasets=DATASETS,
        dataset_ids=("cities15000.zip", "admin1CodesASCII.txt", "alternateNamesV2.zip"),
        cadence_seconds=30 * 86400,
        planned_requests=2,  # cities15000 + admin1; alternateNamesV2 (on request) is topped up
        max_response_bytes=MAX_ALTNAMES_BYTES,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        code_digest=module_digest(Place),
        field_mappings={"geo": "country code (geonames_cc)", "numeric": sorted(NUMERIC), "text": sorted(TEXT)},
        notes="undated provider snapshot; rows dated by retrieval day; fr/en alternate names only on request",
    )


def _wants_names(run: Any) -> bool:
    if run.dataset is not None:
        return bool(run.dataset == DS_NAMES)
    return str((run.options or {}).get("altnames", "")).lower() in ("1", "true", "yes")


async def atlas_ingest(run: Any) -> dict[str, Any]:
    """Fetch cities15000 + admin1 (+ alternate names on request), archive, register the atlas tables, then
    load the GeoNames KG statements when Wikidata P1566 links are already in the KG (else: reported)."""
    summary: dict[str, Any] = {"connector": CONNECTOR_NAME, "tables": {}, "omitted_cells": {}}
    need_cities = any(run.wants(d) for d in (DS_CITIES, DS_COUNTRY, DS_NAMES)) or run.dataset is None
    places: list[Place] = []
    cities_raw = admin_raw = None
    if need_cities or run.wants(DS_ADMIN1):
        admin_raw = await run.get(URL_ADMIN1, key="admin1", media_types=("text/plain",), max_bytes=MAX_ADMIN1_BYTES)
        admin_sha = run.archive(admin_raw)
        admin = parse_admin1(admin_raw.body)
        live = run.origin == "live"  # recorded fixtures are excerpts: the completeness bounds apply to live bytes
        if live and len(admin) < MIN_ADMIN1:
            raise _err(f"admin1CodesASCII.txt has only {len(admin)} rows (expected >= {MIN_ADMIN1})")
        summary["admin1_raw_sha256"] = admin_sha
        if run.wants(DS_ADMIN1):
            t = run.register(
                dataset=DS_ADMIN1,
                frame=_frame(admin1_rows(admin, raw_sha256=admin_sha, retrieved_at=admin_raw.retrieved_at)),
                raw_sha256s=[admin_sha],
                geo_scheme=GEO_SCHEME,
                rights=RIGHTS,
                coverage_note=f"GeoNames admin1CodesASCII.txt: {len(admin)} first-order "
                "divisions (undated; retrieval snapshot)",
            )
            summary["tables"][DS_ADMIN1] = _table_summary(t)
    if need_cities:
        cities_raw = await run.get(
            URL_CITIES, key="cities15000", media_types=("application/zip",), max_bytes=MAX_CITIES_BYTES
        )
        sha = run.archive(cities_raw)
        places = parse_cities(cities_raw.body)
        problems = validate_cities(places, min_rows=MIN_CITIES if run.origin == "live" else 1)
        if problems:
            raise _err("; ".join(problems[:10]))
        summary["cities_raw_sha256"] = sha
        summary["places"] = len(places)
        a1 = {a.code: a for a in admin}
        if run.wants(DS_CITIES):
            rows, omitted = city_rows(places, a1, raw_sha256=sha, retrieved_at=cities_raw.retrieved_at)
            t = run.register(
                dataset=DS_CITIES,
                frame=_frame(rows),
                raw_sha256s=[sha],
                geo_scheme=GEO_SCHEME,
                rights=RIGHTS,
                coverage_note=f"GeoNames cities15000: {len(places)} populated places "
                "('population > 15000 or capitals'), values as declared, undated (retrieval snapshot); "
                f"omitted cells: {json.dumps(omitted, sort_keys=True)}",
            )
            summary["tables"][DS_CITIES] = _table_summary(t)
            summary["omitted_cells"] = omitted
        if run.wants(DS_COUNTRY):
            t = run.register(
                dataset=DS_COUNTRY,
                frame=_frame(country_rows(places, raw_sha256=sha, retrieved_at=cities_raw.retrieved_at)),
                raw_sha256s=[sha],
                geo_scheme=GEO_SCHEME,
                rights=RIGHTS,
                coverage_note="exact per-country counts/selections from cities15000 "
                "(places > 15000, largest place, PPLC places; several PPLC all listed)",
            )
            summary["tables"][DS_COUNTRY] = _table_summary(t)
        if _wants_names(run):
            alt = await run.get(
                URL_ALTNAMES, key="alternateNamesV2", media_types=("application/zip",), max_bytes=MAX_ALTNAMES_BYTES
            )
            alt_sha = run.archive(alt)
            country_of = {p.geonameid: p.country for p in places}
            found = list(iter_altnames(alt.body, set(country_of)))
            totals = Counter((a.geonameid, a.lang) for a in found)
            kept, dropped = bounded_names(found)
            del alt
            t = run.register(
                dataset=DS_NAMES,
                frame=_frame(
                    name_rows(kept, country_of, raw_sha256=alt_sha, retrieved_at=cities_raw.retrieved_at, totals=totals)
                ),
                raw_sha256s=[alt_sha],
                geo_scheme=GEO_SCHEME,
                rights=RIGHTS,
                coverage_note=f"fr/en alternate names of the cities15000 places, at most "
                f"{MAX_NAMES_PER_LANG} per place and language ({dropped} further names not kept)",
            )
            summary["tables"][DS_NAMES] = _table_summary(t)
            summary["names"] = {
                "found": len(found),
                "kept": len(kept),
                "dropped_over_bound": dropped,
                "places_with_fr": len({a.geonameid for a in kept if a.lang == "fr"}),
                "places_with_en": len({a.geonameid for a in kept if a.lang == "en"}),
            }
    if places and cities_raw is not None and admin_raw is not None:
        summary["kg"] = load_geonames_kg(
            run.engine,
            run.store,
            places,
            admin,
            retrieved_at=cities_raw.retrieved_at,
            origin=run.origin,
            cities_sha=summary["cities_raw_sha256"],
            admin_sha=summary["admin1_raw_sha256"],
        )
    return summary


def _table_summary(t: Mapping[str, Any]) -> dict[str, Any]:
    keep = ("dataset_key", "dataset_id", "artifact_sha256", "row_count", "created", "unmapped_provider_geo")
    return {k: t.get(k) for k in keep}


# --------------------------------------------------------------------------------------------- KG
SPEC_DIR = Path(__file__).resolve().parents[4] / "registry" / "kg" / "queries" / "places" / "wikidata"
SPEC_GEONAMES_IDS = SPEC_DIR / "geonames_ids.yaml"
SPEC_CAPITALS = SPEC_DIR / "country_capitals.yaml"
IDS_MARKER = "__GEONAMES_IDS__"
#: a geonameid is expected on at most a few items; a chunk returning more rows than this bound fails
#: (``row_bound_exceeded``), it is never truncated
ROWS_PER_ID_BOUND = 4


def geonames_qid_map(engine: Any, origin: str) -> tuple[dict[str, tuple[str, str | None]], dict[str, list[str]]]:
    """geonameid -> (QID, Wikidata statement id) from the current Wikidata P1566 statements in the KG, and the
    geonameids held by several items (ambiguous: never used)."""
    from sqlalchemy import text

    by_gn: dict[str, set[tuple[str, str | None]]] = defaultdict(set)
    with engine.connect() as conn:
        rows = conn.execute(
            text("""SELECT subject_qid, value->>'value' AS gn, statement_id FROM kg.statement_current
                    WHERE property_pid = 'P1566' AND origin = :o AND value IS NOT NULL
                      AND source_key IN ('wikidata.wdqs', 'wikidata.wbapi')"""),
            {"o": origin},
        ).all()
    for r in rows:
        if r.gn and str(r.gn).isdigit() and str(r.subject_qid).startswith("Q"):
            by_gn[str(r.gn)].add((str(r.subject_qid), r.statement_id))
    out: dict[str, tuple[str, str | None]] = {}
    ambiguous: dict[str, list[str]] = {}
    for gn, items in by_gn.items():
        qids = sorted({q for q, _ in items}, key=lambda q: int(q[1:]))
        if len(qids) == 1:
            out[gn] = (qids[0], sorted(s or "" for q, s in items)[0] or None)
        else:
            ambiguous[gn] = qids
    return out, ambiguous


def geonames_statements(
    places: Sequence[Place],
    admin1: Sequence[Admin1],
    gn_qids: Mapping[str, tuple[str, str | None]],
    country_qids: Mapping[str, str],
    *,
    cities_sha: str,
    admin_sha: str,
    retrieved_at: datetime,
) -> tuple[list[Any], dict[str, Any]]:
    """GeoNames-sourced statements on Wikidata items mapped through Wikidata's own P1566 (never by name)."""
    from reva.kg.model import KgStatement

    a1 = {a.code: a for a in admin1}
    sts: list[Any] = []
    info: Counter[str] = Counter()
    rev = cities_sha[:16]

    def ref(p: Place, extra: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        r = {
            "source": "GeoNames cities15000.txt",
            "url": URL_CITIES,
            "line": p.line,
            "raw_sha256": cities_sha,
            "retrieved_at": retrieved_at.isoformat(),
            "licence": RIGHTS["licence"],
        }
        return [{**r, **(extra or {})}]

    for p in places:
        m = gn_qids.get(p.geonameid)
        if m is None:
            info["places_unmapped"] += 1
            continue
        qid, wd_sid = m
        info["places_mapped"] += 1
        quals = {
            "geonameid": p.geonameid,
            "feature_code": p.feature_code,
            "subject_mapped_via": {"property": "P1566", "wikidata_statement_id": wd_sid},
        }
        iso3 = _geo_iso3(p.country)
        cq = country_qids.get(iso3 or "")

        def st(
            pid: str,
            *,
            obj: str | None = None,
            value: Mapping[str, Any] | None = None,
            q: Mapping[str, Any] | None = None,
            refs: list[dict[str, Any]] | None = None,
            p: Place = p,
            qid: str = qid,
            quals: Mapping[str, Any] = quals,
        ) -> None:
            sts.append(
                KgStatement(
                    subject_qid=qid,
                    property_pid=pid,
                    object_qid=obj,
                    value=value,
                    qualifiers={**quals, **(q or {})},
                    references=refs or ref(p),
                    source_key=SOURCE_KEY,
                    source_revision=rev,
                    statement_id=f"geonames:{p.geonameid}:{pid}",
                    raw_sha256=cities_sha,
                )
            )
            info[f"statements_{pid}"] += 1

        if cq:
            st("P17", obj=cq, q={"country_code": p.country, "country_iso3": iso3})
            if p.feature_code == "PPLC":
                st(
                    "P1376",
                    obj=cq,
                    q={
                        "country_code": p.country,
                        "country_iso3": iso3,
                        "basis": "GeoNames feature code PPLC (capital of a political entity)",
                    },
                )
        else:
            info["country_qid_missing"] += 1
        if p.population > 0:
            st(
                "P1082",
                value={"type": "quantity", "amount": str(p.population), "unit": None},
                q={
                    "geonames_modification_date": p.cells["modification date"],
                    "date_note": "GeoNames states no date for its population figure",
                },
            )
        key = p.admin1_key
        div = a1.get(key) if key else None
        if div is not None:
            dm = gn_qids.get(div.geonameid)
            if dm is not None:
                st(
                    "P131",
                    obj=dm[0],
                    q={
                        "admin1_code": div.code,
                        "admin1_geonameid": div.geonameid,
                        "object_mapped_via": {"property": "P1566", "wikidata_statement_id": dm[1]},
                    },
                    refs=ref(
                        p,
                        {
                            "admin1_file": "admin1CodesASCII.txt",
                            "admin1_line": div.line,
                            "admin1_raw_sha256": admin_sha,
                        },
                    ),
                )
            else:
                info["admin1_unmapped"] += 1
    return sts, dict(info)


def load_geonames_kg(
    engine: Any,
    store: Any,
    places: Sequence[Place],
    admin1: Sequence[Admin1],
    *,
    retrieved_at: datetime,
    origin: str,
    cities_sha: str,
    admin_sha: str,
    country_qids: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Load the GeoNames statements (``load_batch``, source ``geonames.cities``) for places whose geonameid
    Wikidata itself links to exactly one item (P1566 in the KG). Reports what is blocked; guesses nothing."""
    from reva.adapters.connectors.atlas_geo_common import ensure_kg_property, load_country_qids
    from reva.kg.model import MAX_STATEMENTS_PER_BATCH, load_batch

    try:
        gn_qids, ambiguous = geonames_qid_map(engine, origin)
    except Exception as e:  # a database without the kg schema: report, never fail the atlas run
        return {"loaded": False, "blocked": f"KG not readable ({type(e).__name__})"}
    if not gn_qids:
        return {
            "loaded": False,
            "blocked": "no Wikidata P1566 (GeoNames ID) statements in the KG yet: run "
            "'python -m reva.adapters.connectors.atlas_cities wikidata --db <db>'",
        }
    cq = dict(country_qids) if country_qids is not None else load_country_qids()
    for pid in ("P17", "P1376", "P131", "P1082"):
        ensure_kg_property(engine, pid)
    sts, info = geonames_statements(
        places, admin1, gn_qids, cq, cities_sha=cities_sha, admin_sha=admin_sha, retrieved_at=retrieved_at
    )
    reports = []
    for i in range(0, len(sts), MAX_STATEMENTS_PER_BATCH):
        rep = load_batch(
            engine,
            store,
            statements=sts[i : i + MAX_STATEMENTS_PER_BATCH],
            source_key=SOURCE_KEY,
            raw_sha256s=[cities_sha, admin_sha],
            retrieved_at=retrieved_at,
            origin=origin,
            parameters={"connector": CONNECTOR_NAME, "mapping": "Wikidata P1566 (unique holder)"},
        )
        reports.append(rep.as_dict())
    out: dict[str, Any] = {
        "loaded": True,
        **info,
        "ambiguous_geonameids": len(ambiguous),
        "ambiguous_sample": dict(list(sorted(ambiguous.items()))[:20]),
        "statements": len(sts),
        "reports": [_short_report(r) for r in reports],
    }
    if not cq:
        out["blocked_country_statements"] = "registry/entities/country_qids.json (ISO3 -> QID) not available"
    return out


def _short_report(r: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in r.items() if k != "rejected"} | {"rejected_sample": list(r.get("rejected") or [])[:5]}


# ---------------------------------------------------------------------- Wikidata harvest (WDHARVEST's client)
def _load_spec(path: Path) -> Any:
    from reva.kg.harvest import load_spec

    return load_spec(path)


def priority_ids(places: Sequence[Place], admin1: Sequence[Admin1]) -> list[str]:
    """GeoNames ids in mapping priority: capitals and seats of government, admin-1 divisions, seats of
    first-order divisions, then every other place by declared population (largest first)."""
    rank = {"PPLC": 0, "PPLG": 0, "PPLA": 2}
    first = [p.geonameid for p in places if rank.get(p.feature_code) == 0]
    seats = [p.geonameid for p in places if rank.get(p.feature_code) == 2]
    rest = [
        p.geonameid
        for p in sorted(places, key=lambda p: (-p.population, int(p.geonameid)))
        if p.feature_code not in rank
    ]
    return list(dict.fromkeys([*first, *(a.geonameid for a in admin1), *seats, *rest]))


def ids_query(spec: Any, ids: Sequence[str]) -> str:
    q = str(spec.data["sparql"])
    if IDS_MARKER not in q:
        raise ValueError(f"{spec.id}: query has no {IDS_MARKER} marker")
    bad = [i for i in ids if not i.isdigit()]
    if bad:
        raise ValueError(f"non-numeric geonameids {bad[:5]}")
    return q.replace(IDS_MARKER, " ".join(f'"{i}"' for i in ids))


async def harvest_p1566(
    client: Any,
    sink: Any,
    geonameids: Sequence[str],
    *,
    chunk_size: int = 300,
    state_dir: Path | None = None,
    max_chunks: int | None = None,
) -> dict[str, Any]:
    """Wikidata's own GeoNames ID statements (P1566) for ``geonameids``: one WDQS request per chunk (VALUES of
    string literals), rows -> KG statements with WDHARVEST's converter, loaded per chunk (resumable)."""
    from reva.adapters.connectors.wikidata import SOURCE_WDQS
    from reva.kg.harvest import HARVEST_VERSION, STATE_DIR, Batch, Harvester, HarvestState, RowBoundExceeded, with_limit

    spec = _load_spec(SPEC_GEONAMES_IDS)
    h = Harvester(client, sink)
    await h.probe_wdqs_as_of()
    ids = list(dict.fromkeys(geonameids))  # caller's priority order (a partial run maps the first ones)
    state = HarvestState(state_dir or STATE_DIR, spec.id)
    state.reset_for(spec.sha256)
    stats: Counter[str] = Counter()
    per_chunk_bound = ROWS_PER_ID_BOUND
    done = 0
    for i in range(0, len(ids), chunk_size):
        part = ids[i : i + chunk_size]
        key = f"ids:{len(part)}:{hashlib.sha256(' '.join(part).encode()).hexdigest()[:16]}"
        if key in state.data["chunks"]:
            stats["chunks_skipped_done"] += 1
            continue
        if max_chunks is not None and done >= max_chunks:
            stats["chunks_left"] += 1
            continue
        bound = len(part) * per_chunk_bound
        res = await client.sparql(
            with_limit(ids_query(spec, part), bound + 1),
            meta={"spec": spec.id, "chunk": key, "spec_sha256": spec.sha256},
        )
        if len(res.bindings) > bound:
            raise RowBoundExceeded(
                f"{spec.id}: chunk {key} returned more than {bound} rows", rows=len(res.bindings), max_rows=bound
            )
        stmts, skipped = h.rows_to_statements(spec, res)
        kept = [s for s in stmts if s is not None]
        if kept:
            sink.load(
                Batch(
                    SOURCE_WDQS,
                    [],
                    kept,
                    [res.raw_sha256],
                    res.retrieved_at,
                    {
                        "spec": spec.id,
                        "chunk": key,
                        "query_sha256": res.query_sha256,
                        "harvester": HARVEST_VERSION,
                        "caller": CONNECTOR_NAME,
                    },
                )
            )
        stats["rows"] += len(res.bindings)
        stats["statements"] += len(kept)
        stats["rows_skipped"] += skipped
        stats["dropped_deprecated"] += sum(1 for s in stmts if s is None)
        done += 1
        state.data["chunks"][key] = {"rows": len(res.bindings), "raw_sha256": res.raw_sha256, "statements": len(kept)}
        state.data["subjects"] = sorted(set(state.data.get("subjects") or []) | {s.subject for s in kept})
        state.save()
    stats["chunks_total"] = math.ceil(len(ids) / chunk_size)
    return {"spec": spec.id, "ids": len(ids), **stats, "subjects": len(state.data.get("subjects") or [])}


async def fetch_capitals(client: Any) -> tuple[list[dict[str, Any]], str]:
    """Rows of the ``places.country_capitals`` query (Wikidata P36 of every item holding an ISO 3166-1
    alpha-3 code, with the capitals' own GeoNames IDs). Archived by the client; nothing loaded."""
    from reva.kg.harvest import RowBoundExceeded, with_limit

    spec = _load_spec(SPEC_CAPITALS)
    bound = int(spec.data["max_rows"])
    res = await client.sparql(
        with_limit(str(spec.data["sparql"]), bound + 1),
        meta={"spec": spec.id, "chunk": "all", "spec_sha256": spec.sha256},
    )
    if len(res.bindings) > bound:
        raise RowBoundExceeded(f"{spec.id}: more than {bound} rows", rows=len(res.bindings), max_rows=bound)
    rows = [{k: v.get("value") for k, v in b.items()} for b in res.bindings]
    return rows, res.raw_sha256


def _q(uri: str | None) -> str | None:
    if not uri:
        return None
    tail = uri.rsplit("/", 1)[-1]
    return tail if tail.startswith("Q") else None


def capital_crosscheck(places: Sequence[Place], wd_rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """GeoNames PPLC places vs Wikidata P36 (current: no end time; rank preferred/normal), per ISO3.

    Agreement = some PPLC geonameid is a GeoNames ID (P1566) of a current Wikidata capital. Every other case is
    listed with both sides; nothing is resolved."""
    pplc: dict[str, list[Place]] = defaultdict(list)
    for p in places:
        if p.feature_code == "PPLC":
            iso3 = _geo_iso3(p.country)
            pplc[iso3 or f"cc:{p.country}"].append(p)
    wd: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for r in wd_rows:
        iso3, cap = r.get("iso3"), _q(r.get("capital"))
        if not iso3 or not cap or r.get("end"):
            continue
        e = wd[iso3].setdefault(
            cap, {"qid": cap, "country_qid": _q(r.get("country")), "geonameids": set(), "ranks": set(), "start": set()}
        )
        if r.get("gn"):
            e["geonameids"].add(r["gn"])
        if r.get("rank"):
            e["ranks"].add(r["rank"].rsplit("#", 1)[-1].replace("Rank", "").lower())
        if r.get("start"):
            e["start"].add(r["start"][:10])
    out: dict[str, Any] = {"countries": {}, "summary": Counter()}
    for iso3 in sorted(set(pplc) | set(wd)):
        g = pplc.get(iso3, [])
        w = wd.get(iso3, {})
        g_ids = {p.geonameid for p in g}
        w_ids = set().union(*(c["geonameids"] for c in w.values())) if w else set()
        countries = {c["country_qid"] for c in w.values()}
        if not g and w:
            status = "wikidata_only"
        elif g and not w:
            status = "geonames_only"
        elif g_ids & w_ids:
            status = "agree" if len(g) == 1 and len(w) == 1 else "agree_partial_multiple"
        else:
            status = "disagree"
        out["summary"][status] += 1
        if len(w) > 1:
            out["summary"]["wikidata_several_current_capitals"] += 1
        if len(countries) > 1:
            out["summary"]["iso3_on_several_items"] += 1
        out["countries"][iso3] = {
            "status": status,
            "geonames_pplc": [
                {"geonameid": p.geonameid, "name": p.cells["name"], "population": p.population} for p in g
            ],
            "wikidata_p36_current": [
                {
                    "qid": c["qid"],
                    "country_qid": c["country_qid"],
                    "geonameids": sorted(c["geonameids"]),
                    "ranks": sorted(c["ranks"]),
                    "start": sorted(c["start"]),
                }
                for c in w.values()
            ],
        }
    out["summary"] = dict(out["summary"])
    return out


def latest_raw(engine: Any, store: Any, url: str, origin: str = "live") -> tuple[bytes, str, datetime] | None:
    """Body, raw sha256 and retrieval time of the latest archived acquisition of ``url`` by this connector."""
    from sqlalchemy import text

    from reva.adapters.http.safe_client import request_fingerprint

    fp = request_fingerprint("GET", url)
    with engine.connect() as conn:
        r = conn.execute(
            text("""SELECT a.raw_sha256, a.retrieved_at FROM evidence.acquisition a
                    JOIN evidence.connector_version v ON v.id = a.connector_version_id
                    WHERE v.name = :n AND a.request_fingerprint = :fp AND a.origin = :o
                    ORDER BY a.retrieved_at DESC LIMIT 1"""),
            {"n": CONNECTOR_NAME, "fp": fp, "o": origin},
        ).first()
    if r is None:
        return None
    return store.get("raw", r.raw_sha256), str(r.raw_sha256), r.retrieved_at


async def harvest_wikidata(
    *,
    db_name: str,
    atlas_store: Any,
    labels: str = "capitals",
    max_chunks: int | None = None,
    chunk_size: int = 300,
    state_dir: Path | None = None,
    report_path: Path | None = None,
    ids_from: tuple[Path, Path] | None = None,
) -> dict[str, Any]:
    """Live: P1566 links of every cities15000 place and admin-1 division, Wikidata P36 cross-check, label
    snapshots (``labels``: none | capitals | all mapped places), then the GeoNames KG statements.

    Every Wikidata request goes through WDHARVEST's ``budgeted_client`` (machine-wide lock, Retry-After,
    budget ledger in ``db_name``, raw responses archived). The GeoNames raws are read from the atlas store
    where ``atlas.geonames_cities`` archived them. ``ids_from`` (cities15000.zip, admin1CodesASCII.txt copies) only
    supplies the geonameids to ask Wikidata about before the atlas ingest has run; the GeoNames KG statements
    are then left to the atlas ingest (they always cite the archived acquisition)."""
    from reva.adapters.connectors.wikidata import load_allowlist
    from reva.kg.harvest import STATE_DIR, Harvester, HarvestState, KgDbSink, SpecReport, budgeted_client

    async with budgeted_client(purpose="citiesgn", db_name=db_name, planned_wdqs=2, planned_wbapi=1) as sess:
        engine = sess.engine
        got_c = latest_raw(engine, atlas_store, URL_CITIES)
        got_a = latest_raw(engine, atlas_store, URL_ADMIN1)
        if got_c is not None and got_a is not None:
            places, admin1 = parse_cities(got_c[0]), parse_admin1(got_a[0])
        elif ids_from is not None:
            places, admin1 = parse_cities(ids_from[0].read_bytes()), parse_admin1(ids_from[1].read_bytes())
        else:
            raise _err("no archived cities15000/admin1 acquisition in this database: run the atlas ingest first")
        allow = load_allowlist()
        sink = KgDbSink(engine, sess.store)
        sink.register_properties(allow)
        ids = priority_ids(places, admin1)
        out: dict[str, Any] = {"database": db_name, "run_id": sess.run_id}
        out["p1566"] = await harvest_p1566(
            sess.client, sink, ids, chunk_size=chunk_size, state_dir=state_dir, max_chunks=max_chunks
        )
        rows, cap_sha = await fetch_capitals(sess.client)
        out["capitals"] = {"raw_sha256": cap_sha, "rows": len(rows), **capital_crosscheck(places, rows)}
        gn_qids, ambiguous = geonames_qid_map(engine, "live")
        out["mapping"] = {
            "places": len(places),
            "places_mapped": sum(1 for p in places if p.geonameid in gn_qids),
            "admin1": len(admin1),
            "admin1_mapped": sum(1 for a in admin1 if a.geonameid in gn_qids),
            "ambiguous": len(ambiguous),
            "ambiguous_sample": dict(list(sorted(ambiguous.items()))[:30]),
        }
        if labels != "none":
            spec = _load_spec(SPEC_GEONAMES_IDS)
            if labels == "capitals":
                todo = {gn_qids[p.geonameid][0] for p in places if p.feature_code == "PPLC" and p.geonameid in gn_qids}
                todo |= {c["qid"] for v in out["capitals"]["countries"].values() for c in v["wikidata_p36_current"]}
            else:
                todo = {q for q, _ in gn_qids.values()}
            known = sink.known_entities(sorted(todo))
            state = HarvestState(state_dir or STATE_DIR, spec.id + ".labels")
            state.reset_for(spec.sha256)
            rep = SpecReport(spec.id, "statements")
            await Harvester(sess.client, sink, allow=allow).fetch_entities(
                spec, sorted(q for q in todo if q not in known), state, rep, full=False, kind="city"
            )
            out["labels"] = {
                "requested": len(todo),
                "already_known": len(known),
                **{k: rep.as_dict()[k] for k in ("entities_labels", "statements", "failures")},
            }
        if got_c is not None and got_a is not None:
            out["kg"] = load_geonames_kg(
                engine,
                atlas_store,
                places,
                admin1,
                retrieved_at=got_c[2],
                origin="live",
                cities_sha=got_c[1],
                admin_sha=got_a[1],
            )
        else:
            out["kg"] = {"loaded": False, "pending": "the GeoNames statements load with the atlas ingest"}
        out["client"] = sess.client.stats.as_dict()
    out["budget"] = sess.usage.get("budget")
    if report_path is not None:
        report_path.write_text(json.dumps(out, indent=1, default=str, ensure_ascii=False))
    return out


# ------------------------------------------------- Wikibase API path (when WDQS refuses our egress)
CITY_PROPERTIES = ("P31", "P17", "P131", "P1376", "P1566", "P625", "P1082")


def _claim_values(claims: Mapping[str, Any], pid: str) -> list[tuple[Any, Mapping[str, Any]]]:
    """(value, claim) for the non-deprecated claims of ``pid`` in a wbgetentities snapshot."""
    out = []
    for c in claims.get(pid) or []:
        if c.get("rank") == "deprecated":
            continue
        dv = ((c.get("mainsnak") or {}).get("datavalue") or {}).get("value")
        if isinstance(dv, Mapping) and "id" in dv:
            dv = dv["id"]
        if dv is not None:
            out.append((dv, c))
    return out


def capital_rows_from_entities(
    countries: Mapping[str, str], entities: Mapping[str, Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Rows shaped like the ``places.country_capitals`` SPARQL result, built from wbgetentities snapshots of
    the countries (P36 claims, rank, P580/P582 qualifiers) and of their capitals (P1566)."""
    rows: list[dict[str, Any]] = []
    ent = "http://www.wikidata.org/entity/"
    for iso3, cq in sorted(countries.items()):
        e = entities.get(cq)
        if not e:
            continue
        for cap, claim in _claim_values(e.get("claims") or {}, "P36"):
            q = claim.get("qualifiers") or {}

            def qtime(pid: str, q: Mapping[str, Any] = q) -> str | None:
                for snak in q.get(pid) or []:
                    t = ((snak.get("datavalue") or {}).get("value") or {}).get("time")
                    if t:
                        return str(t).lstrip("+")
                    if snak.get("snaktype") == "somevalue":
                        return "somevalue"
                return None

            rank = "http://wikiba.se/ontology#" + str(claim.get("rank", "normal")).capitalize() + "Rank"
            base = {
                "country": ent + cq,
                "iso3": iso3,
                "capital": ent + str(cap),
                "rank": rank,
                "start": qtime("P580"),
                "end": qtime("P582"),
            }
            gns = [str(v) for v, _ in _claim_values((entities.get(str(cap)) or {}).get("claims") or {}, "P1566")]
            rows.extend([{**base, "gn": g} for g in gns] or [{**base, "gn": None}])
    return rows


async def map_via_search(
    sess: Any, sink: Any, allow: Any, ids: Sequence[str], *, state_dir: Path | None = None
) -> dict[str, Any]:
    """Candidates by CirrusSearch ``haswbstatement:P1566=<id>|...`` (Wikibase API host, not WDQS), then full
    wbgetentities snapshots of the candidates loaded through WDHARVEST's harvester (claims filtered to
    :data:`CITY_PROPERTIES`). The link itself is only ever read from those snapshots' own P1566 claims."""
    from reva.kg.harvest import STATE_DIR, Harvester, HarvestState, SpecReport, or_groups

    spec = _load_spec(SPEC_GEONAMES_IDS)
    cands: list[str] = []
    truncated: list[str] = []
    for q in or_groups("haswbstatement:", [f"P1566={i}" for i in ids]):
        hits, total, _shas = await sess.client.search_all(
            q, meta={"purpose": "geonames_p1566", "caller": CONNECTOR_NAME}
        )
        cands.extend(hits)
        if total > len(hits):
            truncated.append(q[:120])
    cands = sorted(set(cands), key=lambda x: int(x[1:]))
    state = HarvestState(state_dir or STATE_DIR, spec.id + ".api")
    state.reset_for(spec.sha256)
    rep = SpecReport(spec.id, "seed_entities")
    await Harvester(sess.client, sink, allow=allow).fetch_entities(
        spec, cands, state, rep, full=True, kind="city", properties=CITY_PROPERTIES
    )
    return {
        "ids": len(ids),
        "candidates": len(cands),
        "search_window_exceeded": truncated,
        **{k: rep.as_dict()[k] for k in ("entities_full", "statements", "failures")},
    }


async def harvest_wikidata_api(
    *,
    db_name: str,
    atlas_store: Any,
    max_ids: int = 600,
    state_dir: Path | None = None,
    report_path: Path | None = None,
) -> dict[str, Any]:
    """The WDQS-free variant of :func:`harvest_wikidata` for a bounded priority subset of geonameids
    (capitals and seats of government first): P1566 links from entity snapshots, the P36 cross-check from
    the countries' own snapshots (``registry/entities/country_qids.json``), then the GeoNames KG statements."""
    from reva.adapters.connectors.atlas_geo_common import load_country_qids
    from reva.adapters.connectors.wikidata import load_allowlist
    from reva.kg.harvest import KgDbSink, budgeted_client

    async with budgeted_client(purpose="citiesapi", db_name=db_name, planned_wdqs=0, planned_wbapi=40) as sess:
        engine = sess.engine
        got_c = latest_raw(engine, atlas_store, URL_CITIES)
        got_a = latest_raw(engine, atlas_store, URL_ADMIN1)
        if got_c is None or got_a is None:
            raise _err("no archived cities15000/admin1 acquisition in this database: run the atlas ingest first")
        places, admin1 = parse_cities(got_c[0]), parse_admin1(got_a[0])
        allow = load_allowlist()
        sink = KgDbSink(engine, sess.store)
        sink.register_properties(allow)
        ids = priority_ids(places, admin1)[:max_ids]
        out: dict[str, Any] = {"database": db_name, "run_id": sess.run_id, "via": "wikibase_api"}
        out["p1566"] = await map_via_search(sess, sink, allow, ids, state_dir=state_dir)
        countries = load_country_qids()
        res = await sess.client.get_entities(
            sorted(set(countries.values()), key=lambda q: int(q[1:])),
            props=("info", "claims"),
            languages=("en",),
            meta={"purpose": "country_capitals", "caller": CONNECTOR_NAME},
        )
        ents = dict(res.entities)
        caps = {str(v) for e in ents.values() for v, _ in _claim_values(e.get("claims") or {}, "P36")}
        missing = sorted((c for c in caps if c not in ents and c.startswith("Q")), key=lambda q: int(q[1:]))
        if missing:
            res2 = await sess.client.get_entities(
                missing,
                props=("info", "claims"),
                languages=("en",),
                meta={"purpose": "capital_geonames_ids", "caller": CONNECTOR_NAME},
            )
            ents.update(res2.entities)
        rows = capital_rows_from_entities(countries, ents)
        out["capitals"] = {"raw_sha256": list(res.raw_sha256s), "rows": len(rows), **capital_crosscheck(places, rows)}
        gn_qids, ambiguous = geonames_qid_map(engine, "live")
        out["mapping"] = {
            "places": len(places),
            "ids_asked": len(ids),
            "places_mapped": sum(1 for p in places if p.geonameid in gn_qids),
            "admin1": len(admin1),
            "admin1_mapped": sum(1 for a in admin1 if a.geonameid in gn_qids),
            "pplc": sum(1 for p in places if p.feature_code == "PPLC"),
            "pplc_mapped": sum(1 for p in places if p.feature_code == "PPLC" and p.geonameid in gn_qids),
            "ambiguous": len(ambiguous),
            "ambiguous_sample": dict(list(sorted(ambiguous.items()))[:30]),
        }
        out["kg"] = load_geonames_kg(
            engine,
            atlas_store,
            places,
            admin1,
            retrieved_at=got_c[2],
            origin="live",
            cities_sha=got_c[1],
            admin_sha=got_a[1],
        )
        out["client"] = sess.client.stats.as_dict()
    out["budget"] = sess.usage.get("budget")
    if report_path is not None:
        report_path.write_text(json.dumps(out, indent=1, default=str, ensure_ascii=False))
    return out


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - operator entry point
    import argparse
    import asyncio

    from reva.adapters.objects.filesystem import FilesystemArtifactStore
    from reva.operations.budgets import BudgetExhausted

    ap = argparse.ArgumentParser(prog="python -m reva.adapters.connectors.atlas_cities")
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("wikidata", help="P1566 links + P36 cross-check + GeoNames KG statements (throwaway db)")
    w.add_argument("--db", required=True, help="throwaway reva_* database holding the atlas ingest")
    w.add_argument("--atlas-object-root", required=True, type=Path, help="object root of the atlas ingest")
    w.add_argument("--labels", choices=("none", "capitals", "all"), default="capitals")
    w.add_argument(
        "--via",
        choices=("wdqs", "api"),
        default="wdqs",
        help="api: CirrusSearch + wbgetentities for a priority subset (WDQS refusing our egress)",
    )
    w.add_argument("--max-ids", type=int, default=600, help="--via api: how many priority geonameids")
    w.add_argument("--max-chunks", type=int, default=None)
    w.add_argument("--chunk-size", type=int, default=300)
    w.add_argument("--state-dir", type=Path, default=None)
    w.add_argument("--report", type=Path, default=None)
    w.add_argument(
        "--ids-from",
        type=Path,
        nargs=2,
        default=None,
        metavar=("CITIES_ZIP", "ADMIN1_TXT"),
        help="local copies giving the geonameids when the atlas ingest has not run yet",
    )
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    from reva.kg.harvest import harvest_settings

    settings = harvest_settings(args.db)
    store = FilesystemArtifactStore(args.atlas_object_root, settings.reva_s3_prefix)
    try:
        if args.via == "api":
            out = asyncio.run(
                harvest_wikidata_api(
                    db_name=args.db,
                    atlas_store=store,
                    max_ids=args.max_ids,
                    state_dir=args.state_dir,
                    report_path=args.report,
                )
            )
        else:
            out = asyncio.run(
                harvest_wikidata(
                    db_name=args.db,
                    atlas_store=store,
                    labels=args.labels,
                    ids_from=(args.ids_from[0], args.ids_from[1]) if args.ids_from else None,
                    max_chunks=args.max_chunks,
                    chunk_size=args.chunk_size,
                    state_dir=args.state_dir,
                    report_path=args.report,
                )
            )
    except BudgetExhausted as e:
        print(json.dumps({"status": "budget_exhausted", "message": e.message, **e.details}, default=str))
        return 3
    caps = out.get("capitals", {})
    print(
        json.dumps(
            {k: v for k, v in out.items() if k != "capitals"} | {"capitals_summary": caps.get("summary")},
            indent=1,
            default=str,
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
