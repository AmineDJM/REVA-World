"""OECD taxes, social and public spending, pensions -> atlas tables (V3, agent A-FISCAL).

Source: the OECD Data Explorer SDMX REST API (``sdmx.oecd.org``, no key; source key ``oecd.sdmx`` shared with
A-ILO / A-TRADE / A-SOCIETY), ``format=csvfilewithlabels`` (code and English label of every component on every
row, so no structure request is needed). Dataflows are requested at their latest version; the CSV header must
show the expected dimension order, else the table is refused (the SDMX keys below depend on it).

Atlas tables (``source = "oecd"``):

* ``rs_global`` — **Global Revenue Statistics**, comparative tax revenues (OECD.CTP.TPS
  ``DSD_REV_COMP_GLOBAL@DF_RSGLOBAL``; ~130 countries + regional averages, 1990–latest): general government (S13)
  tax revenue by OECD tax category (total, 1000 income/profits … 6000 other) as % of GDP and as % of total
  taxation; total tax revenue by level of government (central, state, local, social security funds, EU) as % of
  GDP and % of total; total tax revenue in national currency and US dollars (UNIT_MULT kept, never applied).
* ``cts_cit`` — **Corporate Tax Statistics**, statutory corporate income tax rates (``DSD_TAX_CIT@DF_CIT``;
  ~145 jurisdictions, 2000–latest): central, sub-central and combined rates, small-business rates.
* ``tw_comp`` — **Taxing Wages** comparative indicators (``DSD_TAX_WAGES_COMP@DF_TW_COMP``; OECD members,
  2000–latest): average and marginal tax wedges, income tax and SSC rates by household type and earnings level;
  gross earnings, labour costs and net income (national currency and USD PPP).
* ``socx_agg`` — **Social Expenditure (SOCX)** aggregates (OECD.ELS.SPD ``DSD_SOCX_AGG@DF_SOCX_AGG``; OECD
  members + a few, 1980–latest): public, mandatory private, voluntary private, net public and net total social
  expenditure by social policy area, % of GDP.
* ``pag`` — **Pensions at a Glance** key indicators (``DSD_PAG@DF_PAG``; OECD + G20, 1950–2084): current and
  future normal retirement age, effective labour-market exit age, gross/net replacement rates, pension wealth,
  public pension spending (and projections), old-age poverty, old-age to working-age ratio…
* ``gov_cofog`` — **Government at a Glance** general government expenditure by function (COFOG, yearly
  updates; OECD.GOV.GIP ``DSD_GOV_COFOG@DF_GOV_COFOG_YU``; ~38 countries, 2007–latest), % of GDP and % of total
  general government expenditure.

Indicators: the measure code then ``|<dimension>=<code>`` for the other folded dimensions, e.g.
``TAX_REV|sector=S13|standard_revenue=T_5111|unit_measure=PT_B1GQ`` (VAT, % of GDP). OECD regional averages
and the OECD/EU aggregates are kept unmapped (``iso3`` null). Licence: OECD Terms and Conditions — the Data
Explorer shows "© OECD Terms & conditions" on every table and the SDMX metadata carries no dataset-level
licence; the OECD's open-by-default policy makes CC BY 4.0 the default since 2024-07-01 (third-party content
excepted). Attribution: "OECD (year), <dataset title>, OECD Data Explorer".
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import polars as pl

from reva.adapters.connectors import atlas_fiscal_common as fc
from reva.adapters.connectors.atlas_fiscal_common import Flow, FlowReport, Request
from reva.atlas.sources import AtlasSourceSpec

CONNECTOR_NAME = "atlas.oecd.fiscal"
CONNECTOR_VERSION = "1.0.0"
PARSER_VERSION = "atlas_oecd_fiscal/1"
SOURCE_KEY = "oecd.sdmx"
SOURCE = "oecd"
HOST = "sdmx.oecd.org"
BASE = f"https://{HOST}/public/rest/data"
GEO_SCHEME = "iso3"
MEDIA = ("application/vnd.sdmx.data+csv", "text/csv", "text/plain")
LICENCE = (
    "OECD Terms and Conditions (Data Explorer: '© OECD Terms & conditions'); CC BY 4.0 is the OECD default since"
    " 2024-07-01 (open-by-default policy); no dataset-level licence in the SDMX metadata"
)
RIGHTS: dict[str, Any] = {
    "licence": LICENCE,
    "licence_url": "https://www.oecd.org/en/about/oecd-open-by-default-policy.html",
    "terms_url": "https://www.oecd.org/en/about/terms-conditions.html",
    "attribution": "OECD Data Explorer (sdmx.oecd.org); cite the OECD dataset title and year",
    "licence_checked": "2026-10-16",
    "source_key": SOURCE_KEY,
}
_RS_SECTORS = "S1311+S1312+S1313+S1314+S1315"

FLOWS: tuple[Flow, ...] = (
    Flow(
        dataset="rs_global",
        flow="DSD_REV_COMP_GLOBAL@DF_RSGLOBAL",
        agency="OECD.CTP.TPS",
        title="Global Revenue Statistics - Comparative tax revenues",
        dims=("REF_AREA", "MEASURE", "SECTOR", "STANDARD_REVENUE", "CTRY_SPECIFIC_REVENUE", "UNIT_MEASURE", "FREQ"),
        area_dim="REF_AREA",
        fold=("MEASURE", "SECTOR", "STANDARD_REVENUE", "UNIT_MEASURE"),
        keys=(
            "{areas}.TAX_REV.S13.._T.PT_B1GQ+PT_OTR_REV_CAT.A",
            "{areas}.TAX_REV." + _RS_SECTORS + "._T._T.PT_B1GQ+PT_OTR_SECTOR.A",
            "{areas}.TAX_REV.S13._T._T.XDC+USD.A",
        ),
        fixture_keys=(
            "FRA+KEN+A9.TAX_REV.S13._T+T_1000+T_5111._T.PT_B1GQ+PT_OTR_REV_CAT.A",
            "FRA+KEN+A9.TAX_REV.S1311+S1313._T._T.PT_B1GQ+PT_OTR_SECTOR.A",
            "FRA+KEN+A9.TAX_REV.S13._T._T.XDC+USD.A",
        ),
        notes="Requests: S13 by tax category (% GDP, % of total taxation); total tax by sub-sector (% GDP, % of"
        " total); S13 total in national currency (CURRENCY in flags) and USD. Country-specific categories not taken.",
    ),
    Flow(
        dataset="cts_cit",
        flow="DSD_TAX_CIT@DF_CIT",
        agency="OECD.CTP.TPS",
        title="Corporate income tax (CIT) - statutory and targeted small business rates",
        dims=(
            "REF_AREA",
            "FREQ",
            "MEASURE",
            "TARGETING",
            "UNIT_MEASURE",
            "SECTOR",
            "DIV_TREATMENT",
            "RATE_STRUCTURE",
            "TAX_BASE",
        ),
        area_dim="REF_AREA",
        fold=("MEASURE", "TARGETING", "SECTOR", "UNIT_MEASURE", "DIV_TREATMENT", "RATE_STRUCTURE", "TAX_BASE"),
        keys=("{areas}........",),
        fixture_keys=("FRA+KEN+CHL+GGY........",),
    ),
    Flow(
        dataset="tw_comp",
        flow="DSD_TAX_WAGES_COMP@DF_TW_COMP",
        agency="OECD.CTP.TPS",
        title="Labour taxation (Taxing Wages) - OECD comparative country indicators",
        dims=("REF_AREA", "MEASURE", "UNIT_MEASURE", "HOUSEHOLD_TYPE", "INCOME_PRINCIPAL", "INCOME_SPOUSE", "FREQ"),
        area_dim="REF_AREA",
        fold=("MEASURE", "UNIT_MEASURE", "HOUSEHOLD_TYPE", "INCOME_PRINCIPAL", "INCOME_SPOUSE"),
        keys=("{areas}......",),
        fixture_keys=("FRA+CHL+OECD_REP.AV_TW+AV_ITR+GEBT..S_C0+C_C2.AW100..A",),
        attrs=("CIVIL_STATUS",),
    ),
    Flow(
        dataset="socx_agg",
        flow="DSD_SOCX_AGG@DF_SOCX_AGG",
        agency="OECD.ELS.SPD",
        title="Social expenditure aggregates (SOCX)",
        dims=(
            "REF_AREA",
            "FREQ",
            "MEASURE",
            "UNIT_MEASURE",
            "EXPEND_SOURCE",
            "SPENDING_TYPE",
            "PROGRAMME_TYPE",
            "PRICE_BASE",
        ),
        area_dim="REF_AREA",
        fold=("MEASURE", "UNIT_MEASURE", "EXPEND_SOURCE", "SPENDING_TYPE", "PROGRAMME_TYPE", "PRICE_BASE"),
        keys=("{areas}.A.SOCX.PT_B1GQ.ES10+ES20+ES30+ES40+ES50._T.._Z",),
        fixture_keys=("FRA+CHL+OECD.A.SOCX.PT_B1GQ.ES10+ES50._T._T+TP01+TP41._Z",),
        attrs=("PRICE_BASE",),
        text_attrs=("BASE_PER",),
        notes="Only % of GDP, total (cash + in kind), all social policy areas; amounts in national currency, USD"
        " PPP per head and % of government expenditure not taken.",
    ),
    Flow(
        dataset="pag",
        flow="DSD_PAG@DF_PAG",
        agency="OECD.ELS.SPD",
        title="Pensions at a Glance",
        dims=("REF_AREA", "FREQ", "MEASURE", "UNIT_MEASURE", "SEX", "AGE", "OPTIONALITY"),
        area_dim="REF_AREA",
        fold=("MEASURE", "UNIT_MEASURE", "SEX", "AGE", "OPTIONALITY"),
        keys=("{areas}......",),
        fixture_keys=("FRA+CHN+OECD_REP.A.CRPLF22+FRPLF22+NPRR100+PEP+PPEP....",),
        notes="Measure PPEP holds OECD projections of public pension expenditure (future years by definition);"
        " FRPLF22 is the future normal retirement age under current legislation.",
    ),
    Flow(
        dataset="gov_cofog",
        flow="DSD_GOV_COFOG@DF_GOV_COFOG_YU",
        agency="OECD.GOV.GIP",
        title="Public finance by function - Government at a Glance indicators, yearly updates",
        dims=("FREQ", "REF_AREA", "MEASURE", "UNIT_MEASURE", "SECTOR", "EXPENDITURE", "EDITION", "CATEGORY"),
        area_dim="REF_AREA",
        fold=("MEASURE", "UNIT_MEASURE", "SECTOR", "EXPENDITURE"),
        keys=("A.{areas}.GE.PT_B1GQ+PT_OTE_S13.S13...",),
        fixture_keys=("A.FRA+CHL+OECD.GE.PT_B1GQ+PT_OTE_S13.S13._T+GF02+GF07+GF09..",),
        attrs=("PRICE_BASE",),
        text_attrs=("BASE_PER",),
        notes="Edition 'YU' (yearly updates), category 'PF' (public finance); % of GDP and % of total general"
        " government expenditure only.",
    ),
)
FLOWS_BY_DATASET = {f.dataset: f for f in FLOWS}


def flow_url(flow: Flow, key: str, areas: Sequence[str] = ()) -> str:
    return f"{BASE}/{flow.agency},{flow.flow},/{key.format(areas='+'.join(areas))}?format=csvfilewithlabels"


def oecd_requests(flow: Flow, fixture: bool) -> list[Request]:
    keys = flow.fixture_keys if fixture else flow.keys
    return [
        Request(
            key=f"data:{flow.flow}:{i}",
            url=flow_url(flow, k),
            label=f"{flow.flow}.{i}.csv",
            media_types=MEDIA,
            max_bytes=flow.max_bytes,
        )
        for i, k in enumerate(keys)
    ]


def oecd_build(flow: Flow, files: list[tuple[str, bytes, str]]) -> tuple[pl.DataFrame, FlowReport, str]:
    frame, rep = fc.parse_flow(flow, files, source=SOURCE, geo_scheme=GEO_SCHEME)
    note = fc.coverage_note(
        flow, rep, provider="OECD", url=flow_url(flow, flow.keys[0]), scheme=GEO_SCHEME, licence=LICENCE
    )
    return frame, rep, note


def atlas_source() -> AtlasSourceSpec:
    import sys

    return AtlasSourceSpec(
        name=CONNECTOR_NAME,
        version=CONNECTOR_VERSION,
        source_key=SOURCE_KEY,
        budget_key=SOURCE_KEY,
        atlas_source=SOURCE,
        provider="OECD (Centre for Tax Policy and Administration; Employment, Labour and Social Affairs; Public"
        " Governance) via the OECD Data Explorer SDMX API",
        licence=LICENCE,
        allowed_hosts=(HOST,),
        datasets=tuple(f.dataset for f in FLOWS),
        dataset_ids=tuple(f"{f.agency}:{f.flow}" for f in FLOWS),
        cadence_seconds=30 * 86_400,
        planned_requests=sum(len(f.keys) for f in FLOWS),
        max_response_bytes=200_000_000,
        min_interval_seconds=2.0,
        parser_version=PARSER_VERSION,
        code_digest=fc.module_digest(sys.modules[__name__]),
        field_mappings={"geo_scheme": GEO_SCHEME, "tables": {f.flow: f"{SOURCE}.{f.dataset}" for f in FLOWS}},
        notes="One SDMX-CSV (with labels) request per key; 8 requests per full run (~180 MB).",
    )


async def atlas_ingest(run: Any) -> dict[str, Any]:
    return await fc.ingest_flows(run, FLOWS, oecd_requests, oecd_build, geo_scheme=GEO_SCHEME, rights=RIGHTS)


__all__ = [
    "CONNECTOR_NAME",
    "FLOWS",
    "FLOWS_BY_DATASET",
    "RIGHTS",
    "atlas_ingest",
    "atlas_source",
    "flow_url",
    "oecd_build",
    "oecd_requests",
]
