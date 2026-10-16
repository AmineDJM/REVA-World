"""Statistics Netherlands (CBS) — Caribbean Netherlands (BES) gap fill (V3, GAPHUNT).

Bonaire, Sint Eustatius and Saba (ISO 3166-1 ``BES``) are public bodies of the Netherlands since
2010-10-10; the World Bank, IMF, UN WPP … publish nothing for them as one entity. CBS publishes them
in StatLine. Source: the CBS OData v3 API ``https://opendata.cbs.nl/ODataApi/odata/<table>`` (open, no
key, GET JSON). Licence: CBS website copyright notice "Unless otherwise stated, the content of this
website is subject to Creative Commons Attribution (CC BY 4.0)" (read 2026-10-16). Four requests:

``bes_population`` 83774ENG "Caribbean Netherlands; population, births, deaths, migration":
                   population on 1 January and on 31 December, live births, deaths (persons).
``bes_gdp``        84789ENG "Caribbean Netherlands; gross domestic product (GDP)" (million USD at current
                   prices and at 2017 prices, value / volume changes %) and 85251ENG "… GDP per capita"
                   (USD). CBS compiles BES GDP in US dollars, the islands' currency since 2011.
``bes_labour``     83165ENG "Caribbean Netherlands; labour participation, key figures" (two-yearly survey),
                   filtered server-side to both sexes, ages 15-74, all education levels: unemployment
                   rate, net and gross participation rates (%), labour force (persons).

Geography: CBS region code ``CN01`` = "Caribbean Netherlands" maps to ``BES`` (scheme ``cbs_gaphunt``);
the islands (GM9001 Bonaire, GM9002 St. Eustatius, GM9003 Saba) are kept with ``iso3 = null`` and
``flags.partial_of_iso3 = BES`` (parts of an entity, never the entity). Codes are stored without the
padding blanks CBS adds (``"CN01  "``). ``None`` cells are omitted; CBS reports structural zeros of
empty survey cells as ``0`` *counts* with a null rate, so for the labour table only rates/levels of
rows whose rate is not null are kept (never a filled zero).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from reva.adapters.connectors import atlas_gap_common as common
from reva.adapters.connectors.atlas_gap_common import Fetched, GapItem, GapTable, canon, strict_decimal
from reva.atlas.sources import AtlasSourceSpec
from reva.atlas.tables import country_iso3
from reva.kernel.errors import SourceResponseError

NAME = "atlas.gap_cbs_bes"
SOURCE_KEY = "cbs.opendata"
HOST = "opendata.cbs.nl"
BASE = f"https://{HOST}/ODataApi/odata"
ATLAS_SOURCE = "cbs"
GEO_SCHEME = "cbs_gaphunt"
PARSER_VERSION = "atlas_gap_cbs_bes/1"
RIGHTS = {
    "licence": "CC BY 4.0 (CBS website copyright notice: 'Unless otherwise stated, the content of this website is subject to Creative Commons Attribution (CC BY 4.0)')",
    "licence_url": "https://www.cbs.nl/en-gb/about-us/website/copyright",
    "attribution": "Source: CBS (Statistics Netherlands), StatLine",
}
LABOUR_FILTER = "$filter=Sex%20eq%20%27T001038%27%20and%20Age%20eq%20%2752052%27%20and%20LevelOfEducation%20eq%20%27T001143%27"
#: table -> (atlas dataset, query suffix, {topic key: (indicator, unit)})
TABLES: dict[str, tuple[str, str, dict[str, tuple[str, str]]]] = {
    "83774ENG": (
        "bes_population",
        "$format=json",
        {
            "PopulationOn1January_1": ("population_1_january", "persons"),
            "PopulationOn31December_17": ("population_31_december", "persons"),
            "LiveBornChildren_2": ("live_births", "persons"),
            "Deaths_3": ("deaths", "persons"),
        },
    ),
    "84789ENG": (
        "bes_gdp",
        "$format=json",
        {
            "GDPValueAtCurrentPrices_1": ("gdp_current_prices", "million USD"),
            "GDPValueAtPricesOf2017_2": ("gdp_prices_of_2017", "million USD (prices of 2017)"),
            "GDPValueChanges_3": ("gdp_value_change", "% change"),
            "GDPVolumeChanges_4": ("gdp_volume_change", "% change"),
        },
    ),
    "85251ENG": ("bes_gdp", "$format=json", {"GrossDomesticProductPerCapita_1": ("gdp_per_capita", "USD")}),
    "83165ENG": (
        "bes_labour",
        f"$format=json&{LABOUR_FILTER}",
        {
            "UnemploymentRate_20": ("unemployment_rate|sex=total|age=15-74", "%"),
            "NetLabourParticipationRate_23": ("net_labour_participation_rate|sex=total|age=15-74", "%"),
            "GrossLabourParticipationRate_22": ("gross_labour_participation_rate|sex=total|age=15-74", "%"),
            "LabourForce_2": ("labour_force|sex=total|age=15-74", "persons"),
            "UnemployedLabourForce_19": ("unemployed_labour_force|sex=total|age=15-74", "persons"),
        },
    ),
}
#: CBS region codes and labels (checked against the API's region code list in every row we read)
REGIONS = {"CN01": "Caribbean Netherlands", "GM9001": "Bonaire", "GM9002": "St. Eustatius", "GM9003": "Saba"}
_PERIOD = re.compile(r"^([12][0-9]{3})JJ00$")


def url(table: str) -> str:
    return f"{BASE}/{table}/TypedDataSet?{TABLES[table][1]}"


def items(*, fixture: bool = False) -> list[GapItem]:  # noqa: ARG001 - fixtures are full captures
    return [
        GapItem(f"data:{t}", url(t), (ds,), ("application/json",), max_bytes=20_000_000) for t, (ds, _q, _m) in TABLES.items()
    ]


def parse_table(table: str, f: Fetched) -> list[dict[str, Any]]:
    dataset, _q, topics = TABLES[table]
    try:
        doc = json.loads(f.body.decode("utf-8"), parse_float=str, parse_int=str)
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise SourceResponseError(f"CBS {table}: not JSON", retryable=False) from e
    recs = doc.get("value") if isinstance(doc, dict) else None
    if not isinstance(recs, list) or not recs:
        raise SourceResponseError(f"CBS {table}: no rows", retryable=False)
    geo_key = next((k for k in recs[0] if k.lower().replace("carribean", "caribbean") == "caribbeannetherlands"), None)
    if geo_key is None or "Periods" not in recs[0]:
        raise SourceResponseError(f"CBS {table}: region/period column missing", columns=list(recs[0])[:10], retryable=False)
    missing = [t for t in topics if t not in recs[0]]
    if missing:
        raise SourceResponseError(f"CBS {table}: topic columns missing", missing=missing, retryable=False)
    rows = []
    for r in recs:
        geo = str(r[geo_key]).strip()
        if geo not in REGIONS:
            raise SourceResponseError(f"CBS {table}: unknown region code", code=geo[:12], retryable=False)
        m = _PERIOD.match(str(r["Periods"]).strip())
        if not m:
            raise SourceResponseError(f"CBS {table}: non-annual period", period=str(r["Periods"])[:12], retryable=False)
        start, end = common.year_period(int(m.group(1)))
        if table == "83165ENG" and r.get("UnemploymentRate_20") is None:
            continue  # empty survey cell: counts are reported as 0 with null rates
        for topic, (indicator, unit) in topics.items():
            value = strict_decimal(r.get(topic))
            if value is None:
                continue
            flags: dict[str, Any] = {"cbs_table": table, "region_label": REGIONS[geo], "cbs_region_code_raw": str(r[geo_key])}
            if geo != "CN01":
                flags["partial_of_iso3"] = "BES"
            if table == "83165ENG":
                flags["survey"] = "CBS Labour Force Survey Caribbean Netherlands (two-yearly)"
            if indicator.startswith("population_1_january"):
                flags["reference"] = "stock on 1 January of the year"
            rows.append(
                {
                    "source": ATLAS_SOURCE,
                    "dataset": dataset,
                    "indicator": indicator,
                    "iso3": country_iso3(geo, GEO_SCHEME),
                    "provider_geo": geo,
                    "period_start": start,
                    "period_end": end,
                    "value": value,
                    "unit": unit,
                    "flags": canon(flags),
                    "raw_sha256": f.sha256,
                    "selector": canon({"table": table, "id": r.get("ID"), "column": topic}),
                }
            )
    return rows


def build(got: Mapping[str, Fetched]) -> list[GapTable]:
    per: dict[str, tuple[list[dict[str, Any]], list[str], list[str]]] = {}
    for table, (dataset, _q, _m) in TABLES.items():
        f = got.get(f"data:{table}")
        if f is None:
            continue
        rows, shas, tabs = per.setdefault(dataset, ([], [], []))
        rows.extend(parse_table(table, f))
        shas.append(f.sha256)
        tabs.append(table)
    out = []
    for dataset, (rows, shas, tabs) in per.items():
        common.dedupe_check(rows, f"CBS {dataset}")
        note = f"CBS StatLine OData tables {', '.join(tabs)}: {len(rows)} cells; CN01 -> BES, islands kept unmapped."
        out.append(GapTable(dataset, common.frame(rows), tuple(shas), GEO_SCHEME, note, RIGHTS))
    return out


def _module() -> Any:
    import sys

    return sys.modules[__name__]


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=NAME,
        version="1.0.0",
        source_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="CBS (Statistics Netherlands)",
        licence=f"{RIGHTS['licence']} ({RIGHTS['licence_url']})",
        allowed_hosts=(HOST,),
        datasets=("bes_population", "bes_gdp", "bes_labour"),
        dataset_ids=tuple(TABLES),
        cadence_seconds=30 * 86400,
        planned_requests=len(TABLES),
        max_response_bytes=20_000_000,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        code_digest=common.code_digest(_module()),
        field_mappings={"geo_scheme": GEO_SCHEME},
        notes="Caribbean Netherlands (Bonaire, St. Eustatius, Saba): population, GDP (USD), labour survey.",
    )


async def atlas_ingest(run: Any) -> dict[str, Any]:
    return await common.ingest_items(run, items(fixture=run.origin == "test_fixture"), build)


__all__ = ["NAME", "REGIONS", "TABLES", "atlas_ingest", "atlas_source", "build", "items", "parse_table", "url"]
