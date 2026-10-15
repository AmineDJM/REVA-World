"""Nobel Prize laureates (Nobel Prize Outreach API v2.1) -> atlas counts + KG statements (V3, A-SCI).

Source
------
``https://api.nobelprize.org/2.1/laureates`` — every laureate (persons and organisations) with
their prizes. Terms: "The Services are free to use according to the Creative Commons Zero (CC0)
license" (https://www.nobelprize.org/about/terms-of-use-for-api-nobelprize-org-and-data-nobelprize-org/,
checked 2026-10-15); the terms ask applications not to alter the data, to cache locally and to cite
the service. One request (``limit`` above the total; ~4 MB); the parser refuses a truncated page.

What is published
-----------------
*Atlas* (``source = nobel``, ``dataset = laureates``): per country and award year, the number of
laureate-prize records (an individual laureate in one award year and category; Marie Curie counts
in 1903 and in 1911) by

* ``born|category=<c>`` — the laureate's **birth country in present-day borders**
  (``birth.place.countryNow`` as stated by the Nobel API, e.g. born in Breslau, Germany -> "Poland");
* ``affiliated|category=<c>`` — a country of the laureate's **affiliation at the time of the award**
  (``nobelPrizes[].affiliations[].countryNow``); a laureate with affiliations in two countries
  counts once in each (full counting), so these counts do not sum to the number of laureates;
* ``born_total`` / ``affiliated_total`` — the same over all award years (period 1901 .. last award
  year + 1).

``<c>`` is ``phy``, ``che``, ``med``, ``lit``, ``pea``, ``eco`` (Sveriges Riksbank Prize in Economic
Sciences, from 1969) or ``all``. Organisations (Peace Prize) have no birth country and are not in
``born``; they appear in ``affiliated`` only when the API lists an affiliation (it does not). Counts
are deterministic aggregations computed by REVA from the provider's records (flag
``aggregation: count``, ``computed_by: reva``); countries without a laureate get no row (the source
is the complete list of laureates, so an absent row means none is recorded in it — it is not a
reported zero). Records whose prize status is ``declined`` or ``restricted`` are counted (the prize
was awarded) and flagged per row.

Countries: the Nobel API names present-day countries in English (``"USA"``, ``"the Netherlands"``,
``"Scotland"``) with a Wikidata QID when it has one. Names are mapped to crosswalk keys by the
reviewed table :data:`COUNTRY_NOW_ISO3` (constituent countries of the United Kingdom -> ``GBR``,
flagged ``subnational_mapped``); a name not in the table, or Taiwan (not a crosswalk entity), keeps
``iso3 = null`` and is listed as unmapped. Counts are aggregated per crosswalk key; the Nobel names
merged into a cell are listed in its flags.

*KG* (:func:`kg_statements`): per laureate-prize, ``<laureate QID> award_received (P166) <prize QID>``
with ``point_in_time`` = award year, motivation, portion and prize status; per person laureate with
a birth city QID, ``place_of_birth (P19) <city QID>``. Subjects/objects are the QIDs published by
the Nobel API itself (``wikidata.id``, ``cityNow.sameAs``); every statement carries the raw sha256
and a selector. Nobel records are curated by Nobel Prize Outreach (not community-edited).
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from reva.adapters.connectors.atlas_sci_common import atlas_frame, atlas_spec, canon, code_digest
from reva.adapters.connectors.base import to_raw
from reva.kernel.errors import SourceResponseError
from reva.ports.connectors import (
    ConnectorSpec,
    Cursor,
    FetchBudget,
    FetchItem,
    FetchPlan,
    ObservationBatch,
    RawAcquisition,
)

CONNECTOR_NAME = "atlas.nobel"
CONNECTOR_VERSION = "1.0.0"
PARSER_VERSION = "atlas-nobel-parse-1"
SOURCE_KEY = "nobelprize.api"
ATLAS_SOURCE = "nobel"
ATLAS_DATASET = "laureates"
HOST = "api.nobelprize.org"
PAGE_LIMIT = 1100  # > 1,028 laureates at 2026-10-15: one page
MAX_BYTES = 12_000_000
MIN_EXPECTED_LAUREATES = 1000  # 1,028 at 2026-10-15; fewer means truncation or a changed endpoint
FIRST_AWARD_YEAR = 1901

RIGHTS: dict[str, str] = {
    "licence": "CC0-1.0",
    "licence_url": "https://www.nobelprize.org/about/terms-of-use-for-api-nobelprize-org-and-data-nobelprize-org/",
    "attribution": "Nobel Prize Outreach, api.nobelprize.org (CC0)",
    "terms": "do not alter or censor the data; cache locally; no Nobel trademarks in titles/logos",
}

#: API ``category.en`` -> (short key, Wikidata QID of the prize). QIDs checked 2026-10-15 against the
#: ``wikibase_item`` page property of the English Wikipedia articles of the six prizes (see
#: docs/dev/v3_a_sci.md); used only as KG objects.
CATEGORIES: dict[str, tuple[str, str]] = {
    "Physics": ("phy", "Q38104"),
    "Chemistry": ("che", "Q44585"),
    "Physiology or Medicine": ("med", "Q80061"),
    "Literature": ("lit", "Q37922"),
    "Peace": ("pea", "Q35637"),
    "Economic Sciences": ("eco", "Q47170"),
}
CATEGORY_KEYS = (*(k for k, _ in CATEGORIES.values()), "all")

#: Nobel API ``countryNow.en`` -> crosswalk key. Reviewed by hand against the API's own QIDs
#: (2026-10-15 snapshot: 85 distinct names). Constituent countries of the UK map to GBR (the Nobel
#: API itself gives Scotland the QID of the United Kingdom, Q145).
COUNTRY_NOW_ISO3: dict[str, str] = {
    "Algeria": "DZA",
    "Argentina": "ARG",
    "Australia": "AUS",
    "Austria": "AUT",
    "Azerbaijan": "AZE",
    "Bangladesh": "BGD",
    "Belarus": "BLR",
    "Belgium": "BEL",
    "Bosnia and Herzegovina": "BIH",
    "Brazil": "BRA",
    "Bulgaria": "BGR",
    "Canada": "CAN",
    "Chile": "CHL",
    "China": "CHN",
    "Colombia": "COL",
    "Costa Rica": "CRI",
    "Croatia": "HRV",
    "Cyprus": "CYP",
    "Czech Republic": "CZE",
    "Democratic Republic of the Congo": "COD",
    "Denmark": "DNK",
    "East Timor": "TLS",
    "Egypt": "EGY",
    "Ethiopia": "ETH",
    "Faroe Islands (Denmark)": "FRO",
    "Finland": "FIN",
    "France": "FRA",
    "Germany": "DEU",
    "Ghana": "GHA",
    "Greece": "GRC",
    "Guadeloupe, France": "GLP",
    "Guatemala": "GTM",
    "Hungary": "HUN",
    "Iceland": "ISL",
    "India": "IND",
    "Indonesia": "IDN",
    "Iran": "IRN",
    "Iraq": "IRQ",
    "Ireland": "IRL",
    "Israel": "ISR",
    "Italy": "ITA",
    "Japan": "JPN",
    "Jordan": "JOR",
    "Kenya": "KEN",
    "Latvia": "LVA",
    "Lebanon": "LBN",
    "Liberia": "LBR",
    "Lithuania": "LTU",
    "Luxembourg": "LUX",
    "Madagascar": "MDG",
    "Mauritius": "MUS",
    "Mexico": "MEX",
    "Morocco": "MAR",
    "Myanmar": "MMR",
    "New Zealand": "NZL",
    "Nigeria": "NGA",
    "North Macedonia": "MKD",
    "Northern Ireland": "GBR",
    "Norway": "NOR",
    "Pakistan": "PAK",
    "Palestine": "PSE",
    "Peru": "PER",
    "Philippines": "PHL",
    "Poland": "POL",
    "Portugal": "PRT",
    "Romania": "ROU",
    "Russia": "RUS",
    "Saint Lucia": "LCA",
    "Scotland": "GBR",
    "Slovakia": "SVK",
    "Slovenia": "SVN",
    "South Africa": "ZAF",
    "South Korea": "KOR",
    "Spain": "ESP",
    "Sweden": "SWE",
    "Switzerland": "CHE",
    "Trinidad and Tobago": "TTO",
    "Tunisia": "TUN",
    "Turkey": "TUR",
    "USA": "USA",
    "Ukraine": "UKR",
    "United Kingdom": "GBR",
    "Venezuela": "VEN",
    "Vietnam": "VNM",
    "Yemen": "YEM",
    "Zimbabwe": "ZWE",
    "the Netherlands": "NLD",
}
SUBNATIONAL = frozenset({"Scotland", "Northern Ireland"})


class NobelParseError(SourceResponseError):
    pass


def _err(msg: str, kind: str = "schema_drift") -> NobelParseError:
    return NobelParseError(msg, retryable=False, kind=kind)


@dataclass(frozen=True)
class Place:
    name: str  # countryNow.en (or cityNow.en)
    qid: str | None


@dataclass(frozen=True)
class LaureatePrize:
    laureate_index: int
    prize_index: int
    laureate_id: str
    laureate_qid: str | None
    kind: str  # person | organization
    name_en: str
    award_year: int
    category: str  # short key
    prize_qid: str
    status: str
    portion: str | None
    motivation_en: str | None
    date_awarded: str | None
    birth_country_now: Place | None
    birth_country_then: str | None
    birth_city_now: Place | None
    affiliations: tuple[tuple[str, Place | None], ...]  # (affiliation name, countryNow)


# ----------------------------------------------------------------------------- connector
def laureates_url(offset: int = 0, limit: int = PAGE_LIMIT) -> str:
    return f"https://{HOST}/2.1/laureates?limit={limit}&offset={offset}"


class NobelAtlasConnector:
    """Atlas bulk connector (archive raw -> atlas table + KG statements; no observations)."""

    def describe(self) -> ConnectorSpec:
        return atlas_spec(
            name=CONNECTOR_NAME,
            version=CONNECTOR_VERSION,
            source_key=SOURCE_KEY,
            provider="Nobel Prize Outreach",
            licence="CC0-1.0",
            allowed_hosts=(HOST,),
            dataset_ids=("laureates",),
            field_mappings={"atlas": f"{ATLAS_SOURCE}.{ATLAS_DATASET}", "kg": ["P166", "P19"]},
            units={"born": "laureate-prize records", "affiliated": "laureate-prize records"},
            geography=tuple(sorted(set(COUNTRY_NOW_ISO3.values()))),
            time_semantics="award year (calendar year of awardYear); birth country in present-day borders",
            lineage_family="nobelprize",
            cadence_seconds=30 * 86400,
            release_aware=False,
            revision_strategy="full snapshot per run; new bytes -> new atlas table version",
            max_response_bytes=MAX_BYTES,
            max_pages=5,
            parser_version=PARSER_VERSION,
            code_digest=code_digest(inspect_module()),
        )

    def plan(self, cursor: Cursor | None = None, budget: FetchBudget | None = None) -> FetchPlan:
        return FetchPlan(
            connector=CONNECTOR_NAME,
            items=(FetchItem(key="laureates:0", url=laureates_url(0), max_bytes=MAX_BYTES, params={"offset": 0}),),
            pagination="limit above the total; the parser refuses a page with fewer laureates than meta.count",
            availability_expectation="updated after each October announcement",
        )

    async def fetch(self, item: FetchItem, http: Any) -> RawAcquisition:
        result = await http.get(item.url, max_bytes=item.max_bytes)
        return to_raw(item, result, getattr(http, "origin", "live"))

    def parse(self, raw: RawAcquisition) -> Iterator[ObservationBatch]:
        return iter(())  # atlas connector: no evidence observations


def inspect_module() -> Any:
    import sys

    return sys.modules[__name__]


# ----------------------------------------------------------------------------- parsing
def _en(v: Any) -> str | None:
    if isinstance(v, Mapping):
        x = v.get("en")
        return str(x) if x is not None else None
    return None


def _qid_from_same_as(node: Any) -> str | None:
    if not isinstance(node, Mapping):
        return None
    for s in node.get("sameAs") or []:
        if isinstance(s, str) and "wikidata.org/wiki/Q" in s:
            q = s.rsplit("/", 1)[1]
            if q[1:].isdigit():
                return q
    return None


def _place(node: Any) -> Place | None:
    name = _en(node)
    if not name:
        return None
    return Place(name, _qid_from_same_as(node))


def parse_laureates(
    body: bytes, *, min_expected: int = MIN_EXPECTED_LAUREATES
) -> tuple[list[LaureatePrize], dict[str, Any]]:
    """Laureate-prize records of one ``/2.1/laureates`` page (must hold every laureate)."""
    try:
        doc = json.loads(body)
    except ValueError as e:
        raise _err(f"laureates: not JSON ({e})") from None
    if (
        not isinstance(doc, Mapping)
        or not isinstance(doc.get("laureates"), list)
        or not isinstance(doc.get("meta"), Mapping)
    ):
        raise _err("laureates: expected an object with 'laureates' list and 'meta'")
    meta = doc["meta"]
    laureates = doc["laureates"]
    total = int(meta.get("count", -1))
    offset = int(meta.get("offset", 0))
    if offset != 0 or total != len(laureates):
        raise _err(f"laureates page is partial (offset {offset}, {len(laureates)} of {total})", kind="incomplete")
    if total < min_expected:
        raise _err(f"only {total} laureates (expected >= {min_expected})", kind="incomplete")
    out: list[LaureatePrize] = []
    for i, lau in enumerate(laureates):
        if not isinstance(lau, Mapping) or "id" not in lau or not isinstance(lau.get("nobelPrizes"), list):
            raise _err(f"laureate {i}: missing id or nobelPrizes")
        person = "orgName" not in lau
        name = (_en(lau.get("knownName")) or _en(lau.get("fullName"))) if person else _en(lau.get("orgName"))
        if not name:
            raise _err(f"laureate {i}: no name")
        wd: Mapping[str, Any] = lau["wikidata"] if isinstance(lau.get("wikidata"), Mapping) else {}
        qid = wd.get("id") if isinstance(wd.get("id"), str) and str(wd.get("id")).startswith("Q") else None
        bplace = ((lau.get("birth") or {}).get("place") or {}) if person else {}
        for j, p in enumerate(lau["nobelPrizes"]):
            cat_en = _en(p.get("category"))
            if cat_en not in CATEGORIES:
                raise _err(f"laureate {i} prize {j}: unknown category {cat_en!r}")
            year_s = str(p.get("awardYear", ""))
            if not (year_s.isdigit() and len(year_s) == 4):
                raise _err(f"laureate {i} prize {j}: awardYear {year_s!r}")
            affs = []
            for a in p.get("affiliations") or []:
                affs.append((_en(a.get("name")) or "", _place(a.get("countryNow"))))
            key, pqid = CATEGORIES[cat_en]
            out.append(
                LaureatePrize(
                    laureate_index=i,
                    prize_index=j,
                    laureate_id=str(lau["id"]),
                    laureate_qid=qid,
                    kind="person" if person else "organization",
                    name_en=name,
                    award_year=int(year_s),
                    category=key,
                    prize_qid=pqid,
                    status=str(p.get("prizeStatus") or "received"),
                    portion=p.get("portion"),
                    motivation_en=_en(p.get("motivation")),
                    date_awarded=p.get("dateAwarded"),
                    birth_country_now=_place(bplace.get("countryNow")),
                    birth_country_then=_en(bplace.get("country")),
                    birth_city_now=_place(bplace.get("cityNow")),
                    affiliations=tuple(affs),
                )
            )
    info = {"laureates": total, "laureate_prizes": len(out), "terms": meta.get("terms"), "license": meta.get("license")}
    return out, info


# ----------------------------------------------------------------------------- atlas rows
def _iso3(name: str) -> str | None:
    if name == "Taiwan":  # not a crosswalk entity (kept unmapped, never merged into CHN)
        return None
    return COUNTRY_NOW_ISO3.get(name)


def atlas_rows(records: Sequence[LaureatePrize], *, raw_sha256: str, retrieved_at: datetime) -> list[dict[str, Any]]:
    """Per-country counts (atlas long schema rows; iso3 filled here, provider_geo = iso3 or the
    verbatim Nobel name when unmapped)."""
    # (indicator family, geo) -> year -> category -> list of records
    cells: dict[tuple[str, str], dict[int, dict[str, list[tuple[LaureatePrize, str]]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(list))
    )
    for r in records:
        if r.kind == "person" and r.birth_country_now is not None:
            g = _iso3(r.birth_country_now.name) or r.birth_country_now.name
            for c in (r.category, "all"):
                cells[("born", g)][r.award_year][c].append((r, r.birth_country_now.name))
        seen: dict[str, str] = {}
        for _, place in r.affiliations:
            if place is None:
                continue
            g = _iso3(place.name) or place.name
            seen.setdefault(g, place.name)
        for g, nm in seen.items():
            for c in (r.category, "all"):
                cells[("affiliated", g)][r.award_year][c].append((r, nm))
    last_year = max(r.award_year for r in records)
    total_period = (date(FIRST_AWARD_YEAR, 1, 1), date(last_year + 1, 1, 1))
    rows: list[dict[str, Any]] = []
    for (fam, geo), by_year in sorted(cells.items()):
        mapped = geo if geo in COUNTRY_NOW_ISO3.values() and len(geo) == 3 and geo.isupper() else None
        totals: dict[str, list[tuple[LaureatePrize, str]]] = defaultdict(list)
        for year in sorted(by_year):
            for cat in CATEGORY_KEYS:
                recs = by_year[year].get(cat)
                if not recs:
                    continue
                totals[cat].extend(recs)
                rows.append(
                    _row(
                        f"{fam}|category={cat}",
                        fam,
                        cat,
                        geo,
                        mapped,
                        (date(year, 1, 1), date(year + 1, 1, 1)),
                        recs,
                        raw_sha256,
                        retrieved_at,
                        {"awardYear": year},
                    )
                )
        for cat, recs in totals.items():
            rows.append(
                _row(
                    f"{fam}_total|category={cat}",
                    fam,
                    cat,
                    geo,
                    mapped,
                    total_period,
                    recs,
                    raw_sha256,
                    retrieved_at,
                    {"awardYear": f"{FIRST_AWARD_YEAR}..{last_year}"},
                )
            )
    return rows


_FAMILY_PATH = {
    "born": "laureates[].birth.place.countryNow.en",
    "affiliated": "laureates[].nobelPrizes[].affiliations[].countryNow.en",
}


def _row(
    indicator: str,
    fam: str,
    cat: str,
    geo: str,
    iso3: str | None,
    period: tuple[date, date],
    recs: Sequence[tuple[LaureatePrize, str]],
    raw_sha256: str,
    retrieved_at: datetime,
    filt: Mapping[str, Any],
) -> dict[str, Any]:
    names = sorted({nm for _, nm in recs})
    qids = sorted({q for r, nm in recs for q in _country_qids(r, fam, nm)})
    flags: dict[str, Any] = {
        "aggregation": "count",
        "computed_by": "reva",
        "unit_of_count": "laureate-prize record (laureate x award year x category)",
        "nobel_country_now": names,
        "retrieved_at": retrieved_at.isoformat(),
        "licence": RIGHTS["licence"],
    }
    if fam == "affiliated":
        flags["full_counting"] = "a laureate affiliated in several countries counts once in each"
    if fam == "born":
        flags["borders"] = "birth place in present-day borders (Nobel API countryNow)"
    sub = sorted(set(names) & SUBNATIONAL)
    if sub:
        flags["subnational_mapped"] = sub
    st = sorted({r.status for r, _ in recs} - {"received"})
    if st:
        flags["includes_prize_status"] = {s: sum(1 for r, _ in recs if r.status == s) for s in st}
    ids = sorted({r.laureate_id for r, _ in recs}, key=int)
    selector: dict[str, Any] = {
        "file": "laureates.json",
        "path": _FAMILY_PATH[fam],
        "filter": {**filt, "category": cat, "countryNow.en": names},
    }
    if len(ids) <= 60:
        selector["laureate_ids"] = ids
    else:
        selector["laureate_ids_count"] = len(ids)
    return {
        "source": ATLAS_SOURCE,
        "dataset": ATLAS_DATASET,
        "indicator": indicator,
        "concept_id": f"sci.nobel.{fam}{'_total' if '_total' in indicator else ''}.{cat}",
        "iso3": iso3,
        "provider_geo": geo,
        "entity_qid": qids[0] if len(qids) == 1 else None,
        "period_start": period[0],
        "period_end": period[1],
        "value": str(len(recs)),
        "value_text": None,
        "unit": "laureate-prize records",
        "flags": canon(flags),
        "raw_sha256": raw_sha256,
        "selector": canon(selector),
    }


def _country_qids(r: LaureatePrize, fam: str, name: str) -> list[str]:
    if fam == "born":
        return [r.birth_country_now.qid] if r.birth_country_now and r.birth_country_now.qid else []
    return [p.qid for _, p in r.affiliations if p is not None and p.name == name and p.qid]


def unmapped_names(records: Sequence[LaureatePrize]) -> list[str]:
    names = {r.birth_country_now.name for r in records if r.kind == "person" and r.birth_country_now}
    names |= {p.name for r in records for _, p in r.affiliations if p is not None}
    return sorted(n for n in names if _iso3(n) is None)


def frame(records: Sequence[LaureatePrize], *, raw_sha256: str, retrieved_at: datetime) -> Any:
    return atlas_frame(atlas_rows(records, raw_sha256=raw_sha256, retrieved_at=retrieved_at))


# ----------------------------------------------------------------------------- KG statements
def kg_statements(records: Sequence[LaureatePrize], *, raw_sha256: str, retrieved_at: datetime) -> list[dict[str, Any]]:
    """Source-neutral statement dicts (converted to ``reva.kg.model`` objects by :func:`to_kg_batch`).

    Only QIDs published by the Nobel API are used; a laureate without a QID yields no statement
    (counted in the ingest report)."""
    out: list[dict[str, Any]] = []
    born_done: set[str] = set()
    for r in records:
        if r.laureate_qid is None:
            continue
        sel = {
            "file": "laureates.json",
            "laureate_index": r.laureate_index,
            "prize_index": r.prize_index,
            "laureate_id": r.laureate_id,
        }
        quals: dict[str, Any] = {"P585": {"time": f"+{r.award_year:04d}-00-00T00:00:00Z", "precision": 9}}
        extra: dict[str, Any] = {"prize_status": r.status}
        if r.portion:
            extra["portion"] = r.portion
        if r.motivation_en:
            extra["motivation_en"] = r.motivation_en
        if r.date_awarded:
            extra["date_awarded"] = r.date_awarded
        affs = [
            {"name_en": n, "country_now_en": p.name if p else None, "country_now_qid": p.qid if p else None}
            for n, p in r.affiliations
        ]
        if affs:
            extra["affiliations_at_award"] = affs
        out.append(
            {
                "subject": r.laureate_qid,
                "property": "P166",
                "object": r.prize_qid,
                "point_in_time": (r.award_year, 9),
                "qualifiers": quals,
                "extra": extra,
                "raw_sha256": raw_sha256,
                "selector": sel,
                "retrieved_at": retrieved_at,
                "statement_key": f"nobel:{r.laureate_id}:{r.category}:{r.award_year}",
            }
        )
        if r.kind == "person" and r.birth_city_now and r.birth_city_now.qid and r.laureate_qid not in born_done:
            born_done.add(r.laureate_qid)
            out.append(
                {
                    "subject": r.laureate_qid,
                    "property": "P19",
                    "object": r.birth_city_now.qid,
                    "point_in_time": None,
                    "qualifiers": {},
                    "extra": {
                        "birth_city_now_en": r.birth_city_now.name,
                        "birth_country_then_en": r.birth_country_then,
                    },
                    "raw_sha256": raw_sha256,
                    "selector": {**sel, "path": "birth.place.cityNow"},
                    "retrieved_at": retrieved_at,
                    "statement_key": f"nobel:{r.laureate_id}:birthplace",
                }
            )
    return out


def to_kg_statements(stmts: Sequence[Mapping[str, Any]]) -> list[Any]:
    """:class:`reva.kg.model.KgStatement` objects for ``load_batch(source_key=SOURCE_KEY, ...)``.

    ``statement_id`` is built from the provider's own ids (laureate id, category, award year), so a
    corrected motivation is a new revision of the same statement; ``source_revision`` is the raw
    artifact's sha256 prefix (the API has no revision id). References cite the laureate's API URL.
    """
    from reva.kg.model import KgStatement, KgTime

    out = []
    for s in stmts:
        pit = s["point_in_time"]
        sel = s["selector"]
        out.append(
            KgStatement(
                subject_qid=s["subject"],
                property_pid=s["property"],
                object_qid=s["object"],
                point_in_time=KgTime(f"{pit[0]:04d}", pit[1]) if pit else None,
                qualifiers={**s["extra"], "nobel_laureate_id": sel["laureate_id"]},
                rank="normal",
                references=(
                    {
                        "P854": f"https://{HOST}/2.1/laureate/{sel['laureate_id']}",
                        "P813": s["retrieved_at"].date().isoformat(),
                        "publisher": "Nobel Prize Outreach (curated, CC0)",
                        "selector": sel,
                    },
                ),
                source_key=SOURCE_KEY,
                source_revision="sha256:" + s["raw_sha256"][:16],
                statement_id=s["statement_key"],
                raw_sha256=s["raw_sha256"],
            )
        )
    return out


#: KG properties used (allow-listed in registry/kg/properties.yaml by WDHARVEST).
KG_PROPERTIES = {
    "P166": ("award_received", "awarded_to", "award received"),
    "P19": ("place_of_birth", "birthplace_of", "place of birth"),
}


__all__ = [
    "ATLAS_DATASET",
    "ATLAS_SOURCE",
    "CATEGORIES",
    "CONNECTOR_NAME",
    "COUNTRY_NOW_ISO3",
    "LaureatePrize",
    "NobelAtlasConnector",
    "atlas_rows",
    "frame",
    "kg_statements",
    "laureates_url",
    "parse_laureates",
    "to_kg_statements",
    "unmapped_names",
]
