"""World Bank International Comparison Program (ICP 2021) -> atlas table (V4, agent V4ECON).

Price levels and purchasing power parities **by expenditure category** (GDP, actual individual consumption, food
and non-alcoholic beverages, bread and cereals, meat, …, machinery and equipment, construction: the 48 ICP
headings the World Bank publishes), for the ICP 2021 benchmark year and the revised 2017 results, ~176 economies
plus regional aggregates.

Source: World Bank Indicators API v2, database "ICP 2021" (source id 90, ``lastupdated`` 2024-08-04), one request:
``https://api.worldbank.org/v2/sources/90/country/all/series/all/classification/PX.WL;PPPGlob/time/YR2021;YR2017
?format=json&per_page=50000``. Budget key ``worldbank.atlas`` (A-WB's key for ``api.worldbank.org``).

Classifications taken (labels quoted from ``/v2/sources/90/classification``):

* ``PX.WL`` "Price level index (World = 100)";
* ``PPPGlob`` "Purchasing power parity (PPP) (US$ = 1)".

Not taken (same API, same request shape; disk budget): expenditure in local currency / US$ / PPP-based,
per-capita expenditure and its indices, shares; the years 2018-2020 of the ICP 2021 database.

Cells: ``indicator`` = ``<ICP series code>|classification=<code>`` (e.g. ``1101000|classification=PX.WL`` = food
and non-alcoholic beverages, price level index); ``unit`` = the classification label verbatim; the series label
(``1101000:FOOD AND NON-ALCOHOLIC BEVERAGES``) in ``flags.series_label``; values are the JSON numbers' literal
text (``parse_float=str``), a ``null`` value is not a cell (counted). ``provider_geo`` = the World Bank economy
code (scheme ``wb``; ICP regional aggregates kept unmapped). Year 2017 cells are ICP 2017 results *as revised in
the ICP 2021 release* (``flags.icp_release = "ICP 2021"``).

Licence: World Bank datasets are CC BY 4.0 (https://datacatalog.worldbank.org/public-licenses#cc-by) unless the
database states otherwise; ICP results © World Bank, attribution "World Bank, International Comparison Program
(ICP) 2021".
"""

from __future__ import annotations

import json
import sys
from typing import Any

import polars as pl

from reva.adapters.connectors import atlas_fiscal_common as fc
from reva.atlas.sources import AtlasSourceSpec
from reva.atlas.tables import ATLAS_SCHEMA, country_iso3
from reva.kernel.errors import SourceResponseError

CONNECTOR_NAME = "atlas.wb_icp"
CONNECTOR_VERSION = "1.0.1"  # 1.0.1: 1.0.0 was registered in the dev DB from an earlier working-tree state
PARSER_VERSION = "atlas_wb_icp/1"
SOURCE = "wb"
DATASET = "icp2021"
SOURCE_KEY = "worldbank.atlas"
HOST = "api.worldbank.org"
SOURCE_ID = "90"
CLASSIFICATIONS = {
    "PX.WL": "Price level index (World = 100)",
    "PPPGlob": "Purchasing power parity (PPP) (US$ = 1)",
}
YEARS = ("YR2021", "YR2017")
GEO_SCHEME = "wb"
MAX_BYTES = 40_000_000
LICENCE = "CC-BY-4.0 (World Bank datasets, https://datacatalog.worldbank.org/public-licenses#cc-by)"
RIGHTS: dict[str, Any] = {
    "licence": LICENCE,
    "licence_url": "https://datacatalog.worldbank.org/public-licenses#cc-by",
    "attribution": "World Bank, International Comparison Program (ICP) 2021",
    "licence_checked": "2026-10-17",
    "source_key": SOURCE_KEY,
}
FIXTURE_COUNTRIES = ("FRA", "IND", "NGA", "USA", "WLD")


def url(fixture: bool = False) -> str:
    countries = ";".join(FIXTURE_COUNTRIES) if fixture else "all"
    return (
        f"https://{HOST}/v2/sources/{SOURCE_ID}/country/{countries}/series/all/classification/"
        f"{';'.join(CLASSIFICATIONS)}/time/{';'.join(YEARS)}?format=json&per_page=50000"
    )


class IcpError(SourceResponseError):
    def __init__(self, msg: str, **ctx: Any) -> None:
        super().__init__(msg, retryable=False, kind="parse_error", **ctx)


def parse(body: bytes, sha: str) -> tuple[pl.DataFrame, dict[str, Any]]:
    """The API v2 JSON (one page) -> atlas frame + report."""
    try:
        doc = json.loads(body.decode("utf-8"), parse_float=str, parse_int=str)
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise IcpError("ICP: not JSON") from e
    if not isinstance(doc, dict) or "source" not in doc:
        raise IcpError("ICP: unexpected API response", head=str(doc)[:200])
    if str(doc.get("pages")) != "1":
        raise IcpError("ICP: response is paginated (raise per_page)", pages=doc.get("pages"))
    src = doc["source"]
    if str(src.get("id")) != SOURCE_ID:
        raise IcpError("ICP: response for another source", got=src.get("id"))
    data = src.get("data") or []
    if int(doc.get("total", -1)) != len(data):
        raise IcpError("ICP: record count differs from 'total'", total=doc.get("total"), got=len(data))
    rep: dict[str, Any] = {"records": len(data), "null_values": 0, "zeros": 0, "geos": {}, "series": {}}
    cols: dict[str, list[Any]] = {c: [] for c in (*fc._COLS, "iso3")}
    for i, rec in enumerate(data):
        var = {v.get("concept"): v for v in rec.get("variable") or []}
        if set(var) != {"Country", "Time", "Series", "Classification"}:
            raise IcpError("ICP: unexpected record concepts", row=i, concepts=sorted(var))
        cls = str(var["Classification"]["id"])
        if cls not in CLASSIFICATIONS or var["Classification"].get("value") != CLASSIFICATIONS[cls]:
            raise IcpError("ICP: classification code/label changed", code=cls, label=var["Classification"].get("value"))
        time_id = str(var["Time"]["id"])
        if time_id not in YEARS:
            raise IcpError("ICP: unrequested year", time=time_id)
        v = rec.get("value")
        if v is None or v == "":
            rep["null_values"] += 1
            continue
        value = fc.strict_decimal(str(v))
        year = int(time_id[2:])
        series = str(var["Series"]["id"])
        geo = str(var["Country"]["id"])
        rep["geos"].setdefault(geo, var["Country"].get("value", ""))
        rep["series"].setdefault(series, var["Series"].get("value", ""))
        if float(value) == 0:
            rep["zeros"] += 1
        flags = {
            "series_label": str(var["Series"].get("value", ""))[:300],
            "classification_label": CLASSIFICATIONS[cls],
            "country_label": str(var["Country"].get("value", "")),
            "icp_release": "ICP 2021",
            "api_lastupdated": str(doc.get("lastupdated", "")),
        }
        if year == 2017:
            flags["note"] = "ICP 2017 results as revised in the ICP 2021 release"
        iso3 = country_iso3(geo, GEO_SCHEME)
        if iso3 is None and country_iso3(geo, "iso3") is not None:
            iso3 = country_iso3(geo, "iso3")  # ISO 3166 territories without a World Bank economy (AIA, MSR, TWN)
            flags["geo_via"] = "iso3"
        start, end = fc.sdmx_period("A", str(year))
        cols["indicator"].append(f"{series}|classification={cls}")
        cols["provider_geo"].append(geo)
        cols["iso3"].append(iso3)
        cols["period_start"].append(start)
        cols["period_end"].append(end)
        cols["value"].append(value)
        cols["unit"].append(CLASSIFICATIONS[cls])
        cols["flags"].append(fc.canon(flags))
        cols["raw_sha256"].append(sha)
        cols["selector"].append(fc.canon({"record": i, "series": series, "classification": cls, "time": time_id}))
    if not cols["indicator"]:
        raise IcpError("ICP: no cell with a value")
    df = pl.DataFrame(
        cols,
        schema={
            "indicator": pl.String,
            "provider_geo": pl.String,
            "period_start": pl.Date,
            "period_end": pl.Date,
            "value": pl.String,
            "unit": pl.String,
            "flags": pl.String,
            "raw_sha256": pl.String,
            "selector": pl.String,
            "iso3": pl.String,
        },
    ).select(
        pl.lit(SOURCE).alias("source"),
        pl.lit(DATASET).alias("dataset"),
        "indicator",
        pl.lit(None, dtype=pl.String).alias("concept_id"),
        "iso3",
        "provider_geo",
        pl.lit(None, dtype=pl.String).alias("entity_qid"),
        "period_start",
        "period_end",
        "value",
        pl.lit(None, dtype=pl.String).alias("value_text"),
        "unit",
        "flags",
        "raw_sha256",
        "selector",
    )
    df = df.cast(dict(ATLAS_SCHEMA))  # type: ignore[arg-type]
    rep["cells"] = df.height
    rep["unmapped"] = sorted(
        g for g in rep["geos"] if country_iso3(g, GEO_SCHEME) is None and country_iso3(g, "iso3") is None
    )
    return df, rep


def coverage_note(rep: dict[str, Any]) -> str:
    unm = rep["unmapped"]
    return (
        f"World Bank ICP 2021 (API v2 source {SOURCE_ID}); request {url()}; {rep['records']} records, {rep['cells']}"
        f" cells ({rep['null_values']} null values are not cells), {len(rep['series'])} ICP headings,"
        f" {len(rep['geos'])} geographies; classifications {', '.join(f'{k} {v!r}' for k, v in CLASSIFICATIONS.items())};"
        f" years 2021 and 2017 (revised). Unmapped (ICP regional aggregates, kept): {', '.join(unm) or 'none'}."
        f" Licence: {LICENCE}."
    )[:4000]


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=CONNECTOR_NAME,
        version=CONNECTOR_VERSION,
        source_key=SOURCE_KEY,
        budget_key=SOURCE_KEY,
        atlas_source=SOURCE,
        provider="World Bank, International Comparison Program (ICP 2021) via the Indicators API v2",
        licence=LICENCE,
        allowed_hosts=(HOST,),
        datasets=(DATASET,),
        dataset_ids=(f"WB:source{SOURCE_ID}",),
        cadence_seconds=90 * 86_400,
        planned_requests=1,
        max_response_bytes=MAX_BYTES,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        code_digest=fc.module_digest(sys.modules[__name__]),
        field_mappings={"geo_scheme": GEO_SCHEME, "classifications": CLASSIFICATIONS, "years": list(YEARS)},
        notes="One JSON request (~12 MB measured 2026-10-17).",
    )


async def atlas_ingest(run: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"fetched": [], "errors": [], "tables": []}
    if not run.wants(DATASET):
        return {**out, "ok": True}
    raw = await run.get(
        url(run.origin == "test_fixture"),
        key=f"data:icp2021:{'+'.join(CLASSIFICATIONS)}",
        media_types=("application/json",),
        max_bytes=MAX_BYTES,
    )
    if not raw.body:
        raise SourceResponseError("ICP: empty response body", retryable=True)
    sha = run.archive(raw)
    out["fetched"].append({"item": "data:icp2021", "bytes": len(raw.body), "sha256": sha})
    frame, rep = parse(raw.body, sha)
    reg = run.register(
        dataset=DATASET,
        frame=frame,
        raw_sha256s=[sha],
        coverage_note=coverage_note(rep),
        geo_scheme=GEO_SCHEME,
        rights=dict(RIGHTS),
    )
    out["report"] = {k: (v if not isinstance(v, dict) else len(v)) for k, v in rep.items()}
    out["tables"].append(
        {
            "table": f"{SOURCE}.{DATASET}",
            "dataset_key": reg.get("dataset_key"),
            "created": reg.get("created"),
            "rows": reg.get("row_count"),
            "countries": (reg.get("coverage") or {}).get("countries"),
            "artifact_sha256": reg.get("artifact_sha256"),
        }
    )
    out["ok"] = True
    return out


__all__ = ["CLASSIFICATIONS", "CONNECTOR_NAME", "DATASET", "atlas_ingest", "atlas_source", "parse", "url"]
