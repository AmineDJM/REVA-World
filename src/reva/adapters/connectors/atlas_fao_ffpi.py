"""FAO Food Price Index (FFPI) and its five commodity sub-indices, monthly -> atlas table (V4, agent V4ECON).

Source (researched 2026-10-17): the FAO "World Food Situation" page
``https://www.fao.org/worldfoodsituation/foodpricesindex/en/`` (archived with every run: it carries the index
definition quoted below and the release date) and its "CSV: Nominal indices from 1990 onwards (monthly)" file
``https://www.fao.org/media/docs/worldfoodsituationlibraries/wfs-library/food_price_indices_data.csv``.
Two GETs, ~110 KB. Budget key ``fao.ffpi`` (new).

Definition (quoted from the page): "FAO Food Price Index (FFPI) is a measure of the monthly change in
international prices of a basket of food commodities. It consists of the average of five commodity group price
indices weighted by the average export shares of each of the groups over 2014-2016."

Cells: one per (month, column) of the CSV: ``Food Price Index``, ``Meat``, ``Dairy``, ``Cereals``, ``Oils``,
``Sugar`` (column headers verbatim as indicators), unit = the file's own second line ``2014-2016=100`` (nominal
index). ``provider_geo = "world"`` (an index of international export prices, not a country value; never mapped to a
country). Empty cells are not cells. The real (deflated) indices are in the Excel workbook, not taken.

Licence: the FFPI files are FAO website content, not one of the FAO corporate statistical databases listed under
the CC BY 4.0 database terms; the FAO website terms (https://www.fao.org/contact-us/terms/en/) say: "All other
content on the FAO website (except where otherwise indicated), may be copied, printed and downloaded for private
study, research and teaching purposes, and for use in non-commercial products or services, provided that
appropriate acknowledgement of FAO as the source and copyright holder is given". Every cell is therefore flagged
``licence_restriction = "non_commercial"`` (owner decision pending; see docs/dev/v4_v4econ.md).
"""

from __future__ import annotations

import csv
import html
import io
import re
import sys
from typing import Any

import polars as pl

from reva.adapters.connectors import atlas_fiscal_common as fc
from reva.atlas.sources import AtlasSourceSpec
from reva.atlas.tables import ATLAS_SCHEMA
from reva.kernel.errors import SourceResponseError

CONNECTOR_NAME = "atlas.fao_ffpi"
CONNECTOR_VERSION = "1.0.0"
PARSER_VERSION = "atlas_fao_ffpi/1"
SOURCE = "fao_ffpi"
DATASET = "ffpi_m"
SOURCE_KEY = "fao.ffpi"
HOST = "www.fao.org"
PAGE_URL = f"https://{HOST}/worldfoodsituation/foodpricesindex/en/"
CSV_URL = f"https://{HOST}/media/docs/worldfoodsituationlibraries/wfs-library/food_price_indices_data.csv"
PROVIDER_GEO = "world"
COLUMNS = ("Food Price Index", "Meat", "Dairy", "Cereals", "Oils", "Sugar")
DEFINITION_START = "FAO Food Price Index (FFPI) is a measure of the monthly change in international prices"
LICENCE = (
    "FAO website terms: copy/download for private study, research and teaching, and for use in non-commercial"
    " products or services, with acknowledgement of FAO as source and copyright holder (non-commercial)"
)
RIGHTS: dict[str, Any] = {
    "licence": LICENCE,
    "licence_url": "https://www.fao.org/contact-us/terms/en/",
    "licence_restriction": "non_commercial",
    "attribution": "FAO. FAO Food Price Index. https://www.fao.org/worldfoodsituation/foodpricesindex/en/",
    "licence_checked": "2026-10-17",
    "source_key": SOURCE_KEY,
}
_MONTH = re.compile(r"^([0-9]{4})-(0[1-9]|1[0-2])$")


class FfpiError(SourceResponseError):
    def __init__(self, msg: str, **ctx: Any) -> None:
        super().__init__(msg, retryable=False, kind="parse_error", **ctx)


def page_facts(body: bytes) -> dict[str, str]:
    """The definition sentence(s) and the release date quoted from the archived page."""
    t = body.decode("utf-8", errors="replace")
    t = re.sub(r"<script.*?</script>|<style.*?</style>", " ", t, flags=re.S)
    t = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", t)))
    i = t.find(DEFINITION_START)
    if i < 0:
        raise FfpiError("FFPI page: the index definition is no longer on the page")
    end = t.find("2014-2016.", i)
    definition = t[i : end + len("2014-2016.")] if end > 0 else t[i : i + 400]
    m = re.search(r"Release date:\s*([0-9]{2}/[0-9]{2}/[0-9]{4})", t)
    return {"definition": definition.strip(), "release_date": m.group(1) if m else ""}


def parse_csv(body: bytes, sha: str, facts: dict[str, str]) -> tuple[pl.DataFrame, dict[str, Any]]:
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise FfpiError("FFPI CSV: not UTF-8") from e
    rows = list(csv.reader(io.StringIO(text)))
    if len(rows) < 5 or rows[0][0].strip() != "FAO Food Price Index":
        raise FfpiError("FFPI CSV: unexpected first line", head=rows[0][:3] if rows else None)
    base = rows[1][0].strip()
    if base != "2014-2016=100":
        raise FfpiError("FFPI CSV: base period line changed", got=base[:40])
    header = [h.strip() for h in rows[2]]
    if header[0] != "Date" or tuple(header[1 : 1 + len(COLUMNS)]) != COLUMNS:
        raise FfpiError("FFPI CSV: columns changed", got=header[:8])
    cols: dict[str, list[Any]] = {c: [] for c in fc._COLS}
    rep: dict[str, Any] = {"months": 0, "empty_cells": 0, "first": None, "last": None}
    for n, r in enumerate(rows[3:], start=4):
        if not r or not r[0].strip():
            continue
        m = _MONTH.match(r[0].strip())
        if not m:
            raise FfpiError("FFPI CSV: unexpected date", row=n, value=r[0][:20])
        start, end = fc.sdmx_period("M", r[0].strip())
        rep["months"] += 1
        rep["first"] = rep["first"] or r[0].strip()
        rep["last"] = r[0].strip()
        for j, name in enumerate(COLUMNS, start=1):
            cell = r[j].strip() if j < len(r) else ""
            if not cell:
                rep["empty_cells"] += 1
                continue
            cols["indicator"].append(name)
            cols["provider_geo"].append(PROVIDER_GEO)
            cols["period_start"].append(start)
            cols["period_end"].append(end)
            cols["value"].append(fc.strict_decimal(cell))
            cols["unit"].append(f"index, {base} (nominal)")
            cols["flags"].append(
                fc.canon(
                    {
                        "frequency": "M",
                        "seasonal_adjustment": "not_stated_by_provider",
                        "geography": "international export prices (not a country)",
                        "licence_restriction": "non_commercial",
                        "release_date": facts.get("release_date", ""),
                    }
                )
            )
            cols["raw_sha256"].append(sha)
            cols["selector"].append(fc.canon({"file": "food_price_indices_data.csv", "row": n, "column": name}))
    if not cols["indicator"]:
        raise FfpiError("FFPI CSV: no cell with a value")
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
        },
    ).select(
        pl.lit(SOURCE).alias("source"),
        pl.lit(DATASET).alias("dataset"),
        "indicator",
        pl.lit(None, dtype=pl.String).alias("concept_id"),
        pl.lit(None, dtype=pl.String).alias("iso3"),
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
    rep["cells"] = df.height
    return df.cast(dict(ATLAS_SCHEMA)), rep  # type: ignore[arg-type]


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=CONNECTOR_NAME,
        version=CONNECTOR_VERSION,
        source_key=SOURCE_KEY,
        budget_key=SOURCE_KEY,
        atlas_source=SOURCE,
        provider="Food and Agriculture Organization of the United Nations (FAO), World Food Situation",
        licence=LICENCE,
        allowed_hosts=(HOST,),
        datasets=(DATASET,),
        dataset_ids=("FAO:FFPI",),
        cadence_seconds=30 * 86_400,
        planned_requests=2,
        max_response_bytes=5_000_000,
        min_interval_seconds=2.0,
        parser_version=PARSER_VERSION,
        code_digest=fc.module_digest(sys.modules[__name__]),
        field_mappings={"provider_geo": PROVIDER_GEO, "columns": list(COLUMNS)},
        notes="Page (definition, release date) + monthly CSV; ~110 KB per run.",
    )


async def atlas_ingest(run: Any) -> dict[str, Any]:
    if not run.wants(DATASET):
        return {"ok": True, "tables": []}
    page = await run.get(PAGE_URL, key="page:ffpi", media_types=("text/html",), max_bytes=3_000_000)
    page_sha = run.archive(page)
    facts = page_facts(page.body)
    raw = await run.get(CSV_URL, key="data:ffpi_csv", media_types=("text/csv", "text/plain"), max_bytes=2_000_000)
    if not raw.body:
        raise SourceResponseError("FFPI: empty CSV", retryable=True)
    sha = run.archive(raw)
    frame, rep = parse_csv(raw.body, sha, facts)
    note = (
        f"FAO Food Price Index, monthly nominal indices ({CSV_URL}); release date {facts['release_date']}; "
        f"{rep['months']} months {rep['first']}..{rep['last']}, {rep['cells']} cells ({rep['empty_cells']} empty"
        f' cells not stored). Definition (FAO page, archived {page_sha[:12]}): "{facts["definition"]}"'
        f" Licence: {LICENCE}."
    )[:4000]
    reg = run.register(
        dataset=DATASET, frame=frame, raw_sha256s=[sha], coverage_note=note, geo_scheme=None, rights=dict(RIGHTS)
    )
    return {
        "ok": True,
        "fetched": [{"item": "page:ffpi", "sha256": page_sha}, {"item": "data:ffpi_csv", "sha256": sha}],
        "report": rep,
        "tables": [
            {
                "table": f"{SOURCE}.{DATASET}",
                "dataset_key": reg.get("dataset_key"),
                "created": reg.get("created"),
                "rows": reg.get("row_count"),
                "artifact_sha256": reg.get("artifact_sha256"),
            }
        ],
    }


__all__ = ["CONNECTOR_NAME", "DATASET", "atlas_ingest", "atlas_source", "page_facts", "parse_csv"]
