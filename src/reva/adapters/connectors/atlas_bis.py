"""BIS statistics -> atlas tables (V3, agent A-FISCAL): central bank policy rates, credit to the non-financial
sector, residential property prices, debt service ratios, effective exchange rates, credit-to-GDP gaps.

Source: the BIS SDMX RESTful API (``stats.bis.org/api/v1``, no key; the BIS Data Portal ``data.bis.org`` serves
the same dataflows as bulk ZIPs). Each table is one data request with
``Accept: application/vnd.sdmx.data+csv;version=2.0.0;labels=both``: SDMX-CSV whose coded cells read
``"<code>: <label>"`` (the bulk "flat" CSV layout), so no structure request is needed. Requests are restricted
to one frequency (the bulk files also hold daily series: 476 MB inflated for policy rates alone).

Atlas tables (``source = "bis"``, ``provider_geo`` = BIS ISO 3166 alpha-2 codes; aggregates such as ``XM`` euro
area, ``XW`` world, ``5R`` advanced economies, ``4T`` emerging market economies, ``G2`` G20 are kept unmapped):

* ``cbpol`` — WS_CBPOL central bank policy rates, monthly, end of period (series ``COMPILATION`` text = the
  BIS description of which rate is used when, kept in flags);
* ``tc`` — WS_TC total credit to the non-financial sector (households, corporations, government) by lender,
  % of GDP, domestic currency and USD, adjusted for breaks, quarterly;
* ``spp`` — WS_SPP selected residential property prices, nominal and real, index and year-on-year change,
  quarterly;
* ``dsr`` — WS_DSR debt service ratios (households, non-financial corporations, private non-financial sector),
  quarterly;
* ``eer`` — WS_EER nominal and real effective exchange rates, narrow and broad baskets, monthly averages;
* ``credit_gap`` — WS_CREDIT_GAP credit-to-GDP ratios, trends and gaps (BIS one-sided HP filter), quarterly.

Sub-annual cells are kept at the provider's frequency (no averaging into years): the atlas year view does not
attach them, per-source series show them. Indicators: ``<prefix>|<dimension>=<code>…``. Flags: observation
status (when not "A: Normal value"), confidentiality (when not "F: Free"), pre-break value, ``collection``
(E end of period / A average of observations…), series title. Units are the BIS ``UNIT_MEASURE`` labels;
``UNIT_MULT`` is kept and never applied.

Terms ("Terms of permitted use of BIS statistics", https://data.bis.org/help/legal, checked 2026-10-16): "The use
of the statistics is unrestricted, provided that: if the statistics are reproduced, the BIS must be cited ...
as the source"; translations must say they are not official; no misleading use or implied endorsement; no
additional charge in commercial products; no warranty. Not a Creative Commons licence; reuse with citation.
"""

from __future__ import annotations

import sys
from typing import Any

import polars as pl

from reva.adapters.connectors import atlas_fiscal_common as fc
from reva.adapters.connectors.atlas_fiscal_common import Flow, FlowReport, Request
from reva.atlas.sources import AtlasSourceSpec

CONNECTOR_NAME = "atlas.bis"
CONNECTOR_VERSION = "1.0.0"
PARSER_VERSION = "atlas_bis/1"
SOURCE_KEY = "bis.stats"
SOURCE = "bis"
HOST = "stats.bis.org"
BASE = f"https://{HOST}/api/v1/data"
GEO_SCHEME = "iso2"
MEDIA = ("text/csv", "application/vnd.sdmx.data+csv")
ACCEPT = {"Accept": "application/vnd.sdmx.data+csv;version=2.0.0;labels=both"}
TERMS_URL = "https://data.bis.org/help/legal#terms-of-permitted-use-of-bis-statistics"
LICENCE = (
    "BIS terms of permitted use of BIS statistics: use unrestricted provided the BIS is cited as the source,"
    " translations marked unofficial, no misleading use, no extra charge in commercial products"
)
RIGHTS: dict[str, Any] = {
    "licence": LICENCE,
    "licence_url": TERMS_URL,
    "terms_quote": "The use of the statistics is unrestricted, provided that: if the statistics are reproduced, the"
    " BIS must be cited in your publication or product as the source of the statistics; ...",
    "attribution": "Source: Bank for International Settlements (BIS) statistics",
    "licence_checked": "2026-10-16",
    "source_key": SOURCE_KEY,
}
_TITLE = ("TITLE_TS",)

FLOWS: tuple[Flow, ...] = (
    Flow(
        dataset="cbpol",
        flow="WS_CBPOL",
        agency="BIS",
        title="Central bank policy rates",
        dims=("FREQ", "REF_AREA"),
        area_dim="REF_AREA",
        fold=(),
        prefix="CBPOL",
        keys=("M.{areas}",),
        fixture_keys=("M.US+XM",),
        freqs=("M",),
        text_attrs=("TITLE", "COMPILATION", "SOURCE_REF", "SUPP_INFO_BREAKS"),
        max_bytes=60_000_000,
        notes="Monthly series (end of period); the daily series are not taken.",
    ),
    Flow(
        dataset="tc",
        flow="WS_TC",
        agency="BIS",
        title="Credit to the non-financial sector",
        dims=("FREQ", "BORROWERS_CTY", "TC_BORROWERS", "TC_LENDERS", "VALUATION", "UNIT_TYPE", "TC_ADJUST"),
        area_dim="BORROWERS_CTY",
        fold=("TC_BORROWERS", "TC_LENDERS", "VALUATION", "UNIT_TYPE", "TC_ADJUST"),
        prefix="TC",
        keys=("Q.{areas}.....",),
        fixture_keys=("Q.FR+CN+XM.P+H+G.A.M.770.A",),
        freqs=("Q",),
        attrs=("COLLECTION",),
        text_attrs=_TITLE,
        max_bytes=200_000_000,
    ),
    Flow(
        dataset="spp",
        flow="WS_SPP",
        agency="BIS",
        title="Residential property prices: selected series",
        dims=("FREQ", "REF_AREA", "VALUE", "UNIT_MEASURE"),
        area_dim="REF_AREA",
        fold=("VALUE", "UNIT_MEASURE"),
        prefix="SPP",
        keys=("Q.{areas}..",),
        fixture_keys=("Q.FR+XM..",),
        freqs=("Q",),
        text_attrs=("TITLE_TS", "BREAKS", "COVERAGE"),
        max_bytes=50_000_000,
    ),
    Flow(
        dataset="dsr",
        flow="WS_DSR",
        agency="BIS",
        title="Debt service ratios for the private non-financial sector",
        dims=("FREQ", "BORROWERS_CTY", "DSR_BORROWERS"),
        area_dim="BORROWERS_CTY",
        fold=("DSR_BORROWERS",),
        prefix="DSR",
        keys=("Q.{areas}.",),
        fixture_keys=("Q.FR+CN.",),
        freqs=("Q",),
        attrs=("COLLECTION",),
        text_attrs=_TITLE,
        max_bytes=20_000_000,
    ),
    Flow(
        dataset="eer",
        flow="WS_EER",
        agency="BIS",
        title="Effective exchange rates",
        dims=("FREQ", "EER_TYPE", "EER_BASKET", "REF_AREA"),
        area_dim="REF_AREA",
        fold=("EER_TYPE", "EER_BASKET"),
        prefix="EER",
        keys=("M...{areas}",),
        fixture_keys=("M.R+N.B.FR+CN+XM",),
        freqs=("M",),
        attrs=("COLLECTION",),
        text_attrs=_TITLE,
        max_bytes=100_000_000,
        notes="Monthly averages; the daily series are not taken.",
    ),
    Flow(
        dataset="credit_gap",
        flow="WS_CREDIT_GAP",
        agency="BIS",
        title="Credit-to-GDP gaps",
        dims=("FREQ", "BORROWERS_CTY", "TC_BORROWERS", "TC_LENDERS", "CG_DTYPE"),
        area_dim="BORROWERS_CTY",
        fold=("TC_BORROWERS", "TC_LENDERS", "CG_DTYPE"),
        prefix="CG",
        keys=("Q.{areas}...",),
        fixture_keys=("Q.FR+CN...",),
        freqs=("Q",),
        attrs=("COLLECTION",),
        text_attrs=_TITLE,
        max_bytes=50_000_000,
    ),
)
FLOWS_BY_DATASET = {f.dataset: f for f in FLOWS}


#: recorded offline fixtures: restricted keys and periods (small real responses)
FIXTURE_START = "2023"


def flow_url(flow: Flow, key: str, start: str | None = None) -> str:
    url = f"{BASE}/{flow.flow}/{key.format(areas='')}"
    return f"{url}?startPeriod={start}" if start else url


def bis_requests(flow: Flow, fixture: bool) -> list[Request]:
    keys = flow.fixture_keys if fixture else flow.keys
    return [
        Request(
            key=f"data:{flow.flow}:{i}",
            url=flow_url(flow, k, start=FIXTURE_START if fixture else None),
            label=f"{flow.flow}.{i}.csv",
            media_types=MEDIA,
            headers=ACCEPT,
            max_bytes=flow.max_bytes,
        )
        for i, k in enumerate(keys)
    ]


def bis_build(flow: Flow, files: list[tuple[str, bytes, str]]) -> tuple[pl.DataFrame, FlowReport, str]:
    frame, rep = fc.parse_flow(flow, files, source=SOURCE, geo_scheme=GEO_SCHEME)
    note = fc.coverage_note(
        flow, rep, provider="BIS", url=flow_url(flow, flow.keys[0]), scheme=GEO_SCHEME, licence=LICENCE
    )
    return frame, rep, note


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=CONNECTOR_NAME,
        version=CONNECTOR_VERSION,
        source_key=SOURCE_KEY,
        budget_key=SOURCE_KEY,
        atlas_source=SOURCE,
        provider="Bank for International Settlements (BIS statistics, SDMX API)",
        licence=LICENCE,
        allowed_hosts=(HOST,),
        datasets=tuple(f.dataset for f in FLOWS),
        dataset_ids=tuple(f"BIS:{f.flow}" for f in FLOWS),
        cadence_seconds=30 * 86_400,
        planned_requests=sum(len(f.keys) for f in FLOWS),
        max_response_bytes=200_000_000,
        min_interval_seconds=2.0,
        parser_version=PARSER_VERSION,
        code_digest=fc.module_digest(sys.modules[__name__]),
        field_mappings={"geo_scheme": GEO_SCHEME, "tables": {f.flow: f"{SOURCE}.{f.dataset}" for f in FLOWS}},
        notes="Six SDMX-CSV (labels=both) requests per full run, one frequency each.",
    )


async def atlas_ingest(run: Any) -> dict[str, Any]:
    return await fc.ingest_flows(run, FLOWS, bis_requests, bis_build, geo_scheme=GEO_SCHEME, rights=RIGHTS)


__all__ = ["CONNECTOR_NAME", "FLOWS", "FLOWS_BY_DATASET", "RIGHTS", "atlas_ingest", "atlas_source", "flow_url"]
