"""Air networks: OpenFlights routes (country-pair route counts, 2014) and OurAirports (airports per country).

Two atlas sources in one module (``atlas_ingest`` dispatches on ``run.spec.name``), agent A-INFRA (V3).

``atlas.openflights`` -- OpenFlights Airport/Airline/Route databases
--------------------------------------------------------------------
* Files: ``airports.dat``, ``airlines.dat``, ``routes.dat``, ``countries.dat`` from the project's GitHub
  repository (``jpatokal/openflights``, ``data/``), plus the provider's data page
  ``https://openflights.org/data.php`` (archived on every run: it states the licence and the staleness
  warning; a run whose page no longer states the Open Database License is refused -- a licence change is a
  rights event, never assumed).
* **Licence: ODbL 1.0** (database) + Database Contents License (contents). Obligations recorded on every
  row and in the source registration: attribute "OpenFlights.org", and any derived database made public
  must be offered under ODbL (share-alike). The repository's AGPL-3.0 ``LICENSE`` covers the code only.
* **Stale**: the provider's own warning -- "The third-party that OpenFlights uses for route data ceased
  providing updates in June 2014. The current data is of historical value only." -- every route row carries
  ``stale`` and ``data_as_of: 2014-06`` and the period is June 2014.
* Countries: ``airports.dat`` names a country by English name. The name is mapped with the provider's own
  ``countries.dat`` (name -> ISO 3166-1 alpha-2) and, for 19 airport-country names that ``countries.dat``
  spells differently (``Burma``, ``Congo (Kinshasa)``...), with the alias scheme ``openflights_name`` of
  ``registry/atlas/geo_aliases/a_infra.yaml`` (each with evidence). ``West Bank`` (one airport, Jerusalem
  Atarot) and the ``countries.dat`` rows without an ISO code stay **unmapped** (kept, listed).

Derived counts (``routes_2014``, period 2014-06-01..2014-07-01): one ``routes.dat`` record = one airline
on one directed airport pair. Endpoint airports are resolved by OpenFlights airport id, else by IATA/ICAO
code; records whose airport cannot be resolved are counted in the run report, never assigned.

* ``ROUTES_OPERATED|partner=<ISO3>`` (directed origin country -> destination country, A != B): records with
  ``codeshare`` != ``Y`` (operated by the listed airline).
* ``ROUTE_AIRPORT_PAIRS|partner=<ISO3>``: distinct directed (origin airport, destination airport), all
  records incl. codeshares.
* ``ROUTE_AIRLINES|partner=<ISO3>``: distinct airlines (OpenFlights airline id, else code), all records.
* per country: ``ROUTES_OPERATED_INTL_OUT``, ``ROUTES_OPERATED_DOMESTIC``, ``ROUTE_DEST_COUNTRIES``
  (distinct destination countries != origin, all records), ``ROUTE_AIRPORTS`` (distinct airports of the
  country appearing in any record).

``atlas.ourairports`` -- OurAirports open data (public domain, updated daily)
---------------------------------------------------------------------------
``airports.csv`` + ``countries.csv`` from ``davidmegginson.github.io/ourairports-data`` and the terms page
``https://ourairports.com/data/`` ("All data is released to the Public Domain", checked every run).
Counts per ``iso_country`` (ISO alpha-2; ``XK`` Kosovo -> ``XKX`` via scheme ``ourairports``; ``XP``
Paracel Islands (disputed) and ``ZZ`` unknown stay unmapped), period = the retrieval day (a snapshot of a
crowd-sourced, daily-updated database, not an official register):
``AIRPORTS|type=<type>`` for each open type, ``AIRPORTS_OPEN`` (every type except ``closed``),
``AIRPORTS_SCHEDULED_SERVICE`` (open and ``scheduled_service = yes``), ``AIRPORTS_CLOSED`` (records the
source marks closed).
"""

from __future__ import annotations

import csv
import io
import sys
from collections import defaultdict
from typing import Any

from reva.adapters.connectors import atlas_infra_common as ic
from reva.atlas.sources import AtlasSourceSpec
from reva.kernel.errors import SourceResponseError

# ----------------------------------------------------------------------------- OpenFlights
OF_NAME = "atlas.openflights"
OF_VERSION = "1.0.1"
OF_SOURCE_KEY = "openflights.data"
OF_ATLAS_SOURCE = "openflights"
OF_DATASET = "routes_2014"
OF_PARSER = "atlas_openflights/1"
OF_RAW_BASE = "https://raw.githubusercontent.com/jpatokal/openflights/master/data/"
OF_FILES = ("countries.dat", "airports.dat", "airlines.dat", "routes.dat")
OF_PAGE = "https://openflights.org/data.php"
OF_PERIOD = ic.month_span(2014, 6)
OF_LICENCE_MARKER = "Open Database License"
OF_STALE_MARKER = "ceased providing updates in June 2014"
OF_RIGHTS = {
    "licence": "ODbL-1.0",
    "licence_url": "https://opendatacommons.org/licenses/odbl/1-0/",
    "contents_licence": "Database Contents License (DbCL) 1.0",
    "attribution": "OpenFlights.org (Airport, Airline and Route databases), https://openflights.org/data.php",
    "share_alike": "a derived database made available to the public must be offered under ODbL 1.0",
    "provider_terms": OF_PAGE,
}
OF_STALE = "provider warning: route data ceased being updated in June 2014; of historical value only"

# ----------------------------------------------------------------------------- OurAirports
OA_NAME = "atlas.ourairports"
OA_VERSION = "1.0.1"
OA_SOURCE_KEY = "ourairports.data"
OA_ATLAS_SOURCE = "ourairports"
OA_DATASET = "airports"
OA_PARSER = "atlas_ourairports/1"
OA_BASE = "https://davidmegginson.github.io/ourairports-data/"
OA_PAGE = "https://ourairports.com/data/"
OA_LICENCE_MARKER = "released to the Public Domain"
OA_TYPES = ("large_airport", "medium_airport", "small_airport", "heliport", "seaplane_base", "balloonport")
OA_RIGHTS = {
    "licence": "Public domain (OurAirports: 'All data is released to the Public Domain')",
    "licence_url": OA_PAGE,
    "attribution": "OurAirports (https://ourairports.com), open data by David Megginson and contributors",
}

NULL = "\\N"
#: truncation guards (a full dump is far above these; tests on trimmed fixtures lower them)
MIN_OF_COUNTRIES = 200
MIN_OF_AIRPORTS = 5000
MIN_OF_ROUTES = 50_000
MIN_OA_AIRPORTS = 50_000


def _err(msg: str) -> SourceResponseError:
    return SourceResponseError(msg, retryable=False, kind="schema_drift")


def code_digest() -> str:
    return ic.code_digest(sys.modules[__name__])


def atlas_source() -> list[AtlasSourceSpec]:
    digest = code_digest()
    return [
        AtlasSourceSpec(
            name=OF_NAME,
            version=OF_VERSION,
            source_key=OF_SOURCE_KEY,
            atlas_source=OF_ATLAS_SOURCE,
            provider="OpenFlights.org (Airport, Airline and Route databases; GitHub jpatokal/openflights)",
            licence=f"{OF_RIGHTS['licence']} + {OF_RIGHTS['contents_licence']}; attribution: "
            f"{OF_RIGHTS['attribution']}; share-alike: {OF_RIGHTS['share_alike']}",
            allowed_hosts=("raw.githubusercontent.com", "openflights.org"),
            datasets=(OF_DATASET,),
            dataset_ids=tuple(f"openflights:{f}" for f in OF_FILES),
            cadence_seconds=365 * ic.DAY,
            planned_requests=1 + len(OF_FILES),
            max_response_bytes=8_000_000,
            min_interval_seconds=1.0,
            parser_version=OF_PARSER,
            code_digest=digest,
            notes="Route data frozen in June 2014 (provider warning): every row flagged stale.",
        ),
        AtlasSourceSpec(
            name=OA_NAME,
            version=OA_VERSION,
            source_key=OA_SOURCE_KEY,
            atlas_source=OA_ATLAS_SOURCE,
            provider="OurAirports (open data, davidmegginson.github.io/ourairports-data)",
            licence=f"{OA_RIGHTS['licence']}; attribution (courtesy): {OA_RIGHTS['attribution']}",
            allowed_hosts=("davidmegginson.github.io", "ourairports.com"),
            datasets=(OA_DATASET,),
            dataset_ids=("ourairports:airports.csv", "ourairports:countries.csv"),
            cadence_seconds=30 * ic.DAY,
            planned_requests=3,
            max_response_bytes=40_000_000,
            min_interval_seconds=1.0,
            parser_version=OA_PARSER,
            code_digest=digest,
            notes="Daily-updated crowd-sourced airport database; counts are snapshots of the retrieval day.",
        ),
    ]


async def atlas_ingest(run: Any) -> dict[str, Any]:
    if run.spec.name == OF_NAME:
        return await ingest_openflights(run)
    if run.spec.name == OA_NAME:
        return await ingest_ourairports(run)
    raise ValueError(f"atlas_flights does not provide {run.spec.name}")


# ============================================================================= OpenFlights parsing
def _csv(body: bytes) -> list[list[str]]:
    return list(csv.reader(io.StringIO(body.decode("utf-8"))))


def _nn(v: str) -> str | None:
    v = v.strip()
    return None if v in ("", NULL) else v


def openflights_country_map(countries_body: bytes) -> dict[str, str | None]:
    """countries.dat: name -> ISO 3166-1 alpha-2 as the provider gives it (None when it gives none)."""
    out: dict[str, str | None] = {}
    for i, r in enumerate(_csv(countries_body)):
        if len(r) != 3:
            raise _err(f"countries.dat row {i}: {len(r)} fields, expected 3")
        out[r[0]] = _nn(r[1])
    if len(out) < MIN_OF_COUNTRIES:
        raise _err(f"countries.dat lists only {len(out)} countries")
    return out


def resolve_country(name: str, cmap: dict[str, str | None]) -> str | None:
    from reva.atlas.tables import country_iso3

    iso2 = cmap.get(name)
    if iso2:
        return country_iso3(iso2, "iso2")
    if name in cmap:  # listed by the provider without an ISO code
        return None
    return country_iso3(name, "openflights_name")


def parse_airports(body: bytes) -> tuple[dict[str, dict[str, str]], dict[str, str], dict[str, str]]:
    """airports.dat -> (by OpenFlights id, IATA -> id, ICAO -> id)."""
    by_id: dict[str, dict[str, str]] = {}
    iata: dict[str, str] = {}
    icao: dict[str, str] = {}
    for i, r in enumerate(_csv(body)):
        if len(r) != 14:
            raise _err(f"airports.dat row {i}: {len(r)} fields, expected 14")
        aid = r[0].strip()
        by_id[aid] = {"id": aid, "name": r[1], "country": r[3], "row": str(i)}
        if _nn(r[4]):
            iata.setdefault(r[4].strip(), aid)
        if _nn(r[5]):
            icao.setdefault(r[5].strip(), aid)
    if len(by_id) < MIN_OF_AIRPORTS:
        raise _err(f"airports.dat lists only {len(by_id)} airports (truncated?)")
    return by_id, iata, icao


def build_openflights(files: dict[str, ic.Fetched]) -> tuple[ic.RowBuilder, dict[str, Any]]:
    cmap = openflights_country_map(files["countries.dat"].body)
    airports, iata, icao = parse_airports(files["airports.dat"].body)
    routes = _csv(files["routes.dat"].body)
    if len(routes) < MIN_OF_ROUTES:
        raise _err(f"routes.dat has only {len(routes)} records (truncated?)")

    def airport(aid: str, code: str) -> dict[str, str] | None:
        if _nn(aid) and aid in airports:
            return airports[aid]
        c = code.strip()
        k = iata.get(c) or icao.get(c)
        return airports.get(k) if k else None

    operated: dict[tuple[str, str], int] = defaultdict(int)
    pairs: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
    airlines: dict[tuple[str, str], set[str]] = defaultdict(set)
    dests: dict[str, set[str]] = defaultdict(set)
    ap_in: dict[str, set[str]] = defaultdict(set)
    unresolved = 0
    for i, r in enumerate(routes):
        if len(r) != 9:
            raise _err(f"routes.dat row {i}: {len(r)} fields, expected 9")
        a, b = airport(r[3], r[2]), airport(r[5], r[4])
        if a is None or b is None:
            unresolved += 1
            continue
        ca, cb = a["country"], b["country"]
        ap_in[ca].add(a["id"])
        ap_in[cb].add(b["id"])
        if r[6].strip() != "Y":
            operated[(ca, cb)] += 1
        pairs[(ca, cb)].add((a["id"], b["id"]))
        airlines[(ca, cb)].add(_nn(r[1]) or f"code:{r[0].strip()}")
        if ca != cb:
            dests[ca].add(cb)

    iso: dict[str, str | None] = {n: resolve_country(n, cmap) for n in set(ap_in)}
    rb = ic.RowBuilder(OF_ATLAS_SOURCE, OF_DATASET)
    routes_f = files["routes.dat"]
    lookups = [files["airports.dat"], files["countries.dat"], files["airlines.dat"]]
    base = {"stale": OF_STALE, "data_as_of": "2014-06", "licence": OF_RIGHTS["licence"], "share_alike": True}

    def emit(ind: str, geo: str, value: int, unit: str, formula: str, where: dict[str, Any], **extra: Any) -> None:
        rb.count(
            indicator=ind,
            provider_geo=geo,
            iso3=iso.get(geo),
            period=OF_PERIOD,
            value=value,
            unit=unit,
            raw_sha256=routes_f.sha256,
            selector={"file": "routes.dat", "aggregate": ind.split("|")[0], "where": where},
            flags=ic.derived(formula, inputs=lookups, **base, **extra),
        )

    for ca, cb in sorted(set(operated) | set(pairs)):
        if ca == cb:
            continue
        p = ic.Partner(iso.get(cb), cb)
        w = {"origin_country": ca, "destination_country": cb}
        f = p.flags()
        emit(
            f"ROUTES_OPERATED|{p.dim()}",
            ca,
            operated.get((ca, cb), 0),
            "routes",
            "count of routes.dat records (airline x origin airport x destination airport) with codeshare != 'Y', "
            "origin airport in origin country, destination airport in destination country",
            w,
            **f,
        )
        emit(
            f"ROUTE_AIRPORT_PAIRS|{p.dim()}",
            ca,
            len(pairs[(ca, cb)]),
            "airport pairs",
            "count of distinct directed (origin airport, destination airport) in routes.dat records, incl. codeshares",
            w,
            **f,
        )
        emit(
            f"ROUTE_AIRLINES|{p.dim()}",
            ca,
            len(airlines[(ca, cb)]),
            "airlines",
            "count of distinct airlines (airline id, else code) in routes.dat records, incl. codeshares",
            w,
            **f,
        )
    for c in sorted(ap_in):
        intl = sum(v for (x, y), v in operated.items() if x == c and y != c)
        emit(
            "ROUTES_OPERATED_INTL_OUT",
            c,
            intl,
            "routes",
            "count of routes.dat records with codeshare != 'Y' from an airport in the country to an airport in "
            "another country",
            {"origin_country": c},
        )
        emit(
            "ROUTES_OPERATED_DOMESTIC",
            c,
            operated.get((c, c), 0),
            "routes",
            "count of routes.dat records with codeshare != 'Y' with both airports in the country",
            {"country": c},
        )
        emit(
            "ROUTE_DEST_COUNTRIES",
            c,
            len(dests[c]),
            "countries",
            "count of distinct other countries reached by routes.dat records (incl. codeshares) from the country",
            {"origin_country": c},
        )
        emit(
            "ROUTE_AIRPORTS",
            c,
            len(ap_in[c]),
            "airports",
            "count of distinct airports of the country appearing as origin or destination in routes.dat records",
            {"country": c},
        )
    report = {
        "routes_records": len(routes),
        "routes_unresolved_airport": unresolved,
        "countries_in_routes": len(ap_in),
        "unmapped_countries": sorted(n for n, v in iso.items() if v is None),
        "country_pairs": sum(1 for (a, b) in pairs if a != b),
    }
    return rb, report


async def ingest_openflights(run: Any) -> dict[str, Any]:
    if not run.wants(OF_DATASET):
        return {"ok": True, "tables": [], "skipped": OF_DATASET}
    page = await ic.fetch_archived(
        run, OF_PAGE, name="data.php", key="openflights:data_page", media_types=("text/html",), max_bytes=2_000_000
    )
    text = page.body.decode("utf-8", "replace")
    if OF_LICENCE_MARKER not in text:
        raise SourceResponseError("OpenFlights data page no longer states the ODbL", retryable=False, kind="licence")
    stale_confirmed = OF_STALE_MARKER in text
    files: dict[str, ic.Fetched] = {}
    for f in OF_FILES:
        files[f] = await ic.fetch_archived(
            run, OF_RAW_BASE + f, name=f, key=f"openflights:{f}", media_types=("text/plain",), max_bytes=8_000_000
        )
    rb, report = build_openflights(files)
    shas = [page.sha256, *(x.sha256 for x in files.values())]
    out = run.register(
        dataset=OF_DATASET,
        frame=rb.frame(),
        raw_sha256s=shas,
        coverage_note=(
            f"OpenFlights routes as of June 2014 (STALE, provider warning); {report['country_pairs']} directed "
            f"country pairs from {report['routes_records']} route records; derived counts; unmapped: "
            f"{', '.join(report['unmapped_countries']) or 'none'}"
        ),
        rights={**OF_RIGHTS, "source_key": OF_SOURCE_KEY, "stale": OF_STALE},
    )
    return {
        "ok": True,
        "tables": [_table(out)],
        "report": {**report, "stale_warning_on_page": stale_confirmed},
    }


# ============================================================================= OurAirports
def build_ourairports(airports: ic.Fetched, countries: ic.Fetched, day: Any) -> tuple[ic.RowBuilder, dict[str, Any]]:
    from reva.atlas.tables import country_iso3

    rows = list(csv.DictReader(io.StringIO(airports.body.decode("utf-8-sig"))))
    need = {"type", "iso_country", "scheduled_service", "ident"}
    if not rows or not need <= set(rows[0]):
        raise _err(f"airports.csv lacks columns {sorted(need - set(rows[0] if rows else {}))}")
    if len(rows) < MIN_OA_AIRPORTS:
        raise _err(f"airports.csv has only {len(rows)} rows (truncated?)")
    names = {r["code"]: r["name"] for r in csv.DictReader(io.StringIO(countries.body.decode("utf-8-sig")))}
    by: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    unknown_types: dict[str, int] = defaultdict(int)
    for r in rows:
        t, c = r["type"].strip(), r["iso_country"].strip()
        if t not in OA_TYPES and t != "closed":
            unknown_types[t] += 1
            continue
        if t == "closed":
            by[c]["closed"] += 1
            continue
        by[c][t] += 1
        by[c]["open"] += 1
        if r["scheduled_service"].strip() == "yes":
            by[c]["scheduled"] += 1
    if unknown_types:
        raise _err(f"airports.csv has unknown airport types {dict(unknown_types)}")
    period = ic.day_span(day)
    rb = ic.RowBuilder(OA_ATLAS_SOURCE, OA_DATASET)
    snap = {"snapshot": "retrieval day of a daily-updated crowd-sourced database", "licence": "public domain"}
    for c in sorted(by):
        iso3 = country_iso3(c, "ourairports")
        extra = {**snap, "provider_country_name": names.get(c)}

        def emit(ind: str, v: int, formula: str, where: dict[str, Any], c: str = c, iso3: Any = iso3,
                 extra: dict[str, Any] = extra) -> None:  # fmt: skip
            rb.count(indicator=ind, provider_geo=c, iso3=iso3, period=period, value=v, unit="airports",
                     raw_sha256=airports.sha256, selector={"file": "airports.csv", "aggregate": "count", "where": where},
                     flags=ic.derived(formula, inputs=[countries], **extra))  # fmt: skip

        for t in OA_TYPES:
            emit(
                f"AIRPORTS|type={t}",
                by[c][t],
                f"count of airports.csv rows with type = {t}",
                {"iso_country": c, "type": t},
            )
        emit("AIRPORTS_OPEN", by[c]["open"], "count of airports.csv rows with type != closed", {"iso_country": c})
        emit(
            "AIRPORTS_SCHEDULED_SERVICE",
            by[c]["scheduled"],
            "count of airports.csv rows with type != closed and scheduled_service = yes",
            {"iso_country": c},
        )
        emit(
            "AIRPORTS_CLOSED",
            by[c]["closed"],
            "count of airports.csv rows with type = closed",
            {"iso_country": c, "type": "closed"},
        )
    report = {
        "airport_records": len(rows),
        "countries": len(by),
        "unmapped": sorted(c for c in by if country_iso3(c, "ourairports") is None),
    }
    return rb, report


async def ingest_ourairports(run: Any) -> dict[str, Any]:
    if not run.wants(OA_DATASET):
        return {"ok": True, "tables": [], "skipped": OA_DATASET}
    page = await ic.fetch_archived(
        run, OA_PAGE, name="ourairports_data_page", key="ourairports:terms", media_types=("text/html",),
        max_bytes=2_000_000,
    )  # fmt: skip
    if OA_LICENCE_MARKER not in page.body.decode("utf-8", "replace"):
        raise SourceResponseError("OurAirports data page no longer states public domain", retryable=False,
                                  kind="licence")  # fmt: skip
    countries = await ic.fetch_archived(
        run, OA_BASE + "countries.csv", name="countries.csv", key="ourairports:countries.csv",
        media_types=("text/csv",), max_bytes=2_000_000,
    )  # fmt: skip
    airports = await ic.fetch_archived(
        run, OA_BASE + "airports.csv", name="airports.csv", key="ourairports:airports.csv",
        media_types=("text/csv",), max_bytes=40_000_000,
    )  # fmt: skip
    rb, report = build_ourairports(airports, countries, airports.retrieved_at.date())
    out = run.register(
        dataset=OA_DATASET,
        frame=rb.frame(),
        raw_sha256s=[page.sha256, countries.sha256, airports.sha256],
        coverage_note=(
            f"OurAirports snapshot {airports.retrieved_at.date().isoformat()}: {report['airport_records']} records, "
            f"{report['countries']} provider countries; unmapped: {', '.join(report['unmapped']) or 'none'}"
        ),
        geo_scheme="ourairports",
        rights={**OA_RIGHTS, "source_key": OA_SOURCE_KEY},
    )
    return {"ok": True, "tables": [_table(out)], "report": report}


def _table(out: dict[str, Any]) -> dict[str, Any]:
    return {
        "table": out["dataset_key"].removeprefix("atlas."),
        "rows": out["row_count"],
        "created": out["created"],
        "artifact_sha256": out["artifact_sha256"],
        "unmapped_provider_geo": [u["provider_geo"] for u in out.get("unmapped_provider_geo") or []],
    }
