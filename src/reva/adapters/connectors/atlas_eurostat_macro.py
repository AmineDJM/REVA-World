"""Eurostat monthly / quarterly macro series and NUTS regional series -> atlas tables (V4, agent V4ECON).

Source: the Eurostat statistics API (JSON-stat 2.0, GET, no key;
``https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/<dataset>?<filters>&format=JSON&lang=EN``),
budget key ``eurostat.dissemination`` (shared with A-ILO's ``earn_mw_cur`` and GAPHUNT's NUTS gap fill). One
request per dataset, filtered to the series below (the API refuses extractions above 5 M cells).

Country-level tables (geo = Eurostat country codes, scheme ``eurostat``; EU/euro-area aggregates kept unmapped):

=============  =====================  ===========================================================================
dataset        Eurostat dataset       series
=============  =====================  ===========================================================================
hicp_m         prc_hicp_minr          HICP all items (ECOICOP ver. 2 ``TOTAL``): index 2025=100, monthly and
                                      annual rates of change, monthly
une_m          une_rt_m               unemployment rate, total, % of the labour force; every published
                                      adjustment (NSA / SA / trend cycle), monthly
gdp_q          namq_10_gdp            GDP at market prices: current prices (million euro), chain-linked volume
                                      q/q and y/y % change; every published adjustment, quarterly
gov_debt_q     gov_10q_ggdebt         general government consolidated gross debt (million euro, % of GDP),
                                      quarterly (end-of-quarter stock)
ip_m           sts_inpr_m             production in industry (B-D), volume index 2021=100, seasonally and
                                      calendar adjusted and unadjusted, monthly
=============  =====================  ===========================================================================

Regional tables (NUTS 0-3 codes verbatim in ``provider_geo``; ``iso3`` only on the NUTS 0 = country rows;
``flags.nuts_level`` and ``flags.country_iso3`` from the code; region entities in the KG, see
:mod:`econ_regions_kg`):

=============  =====================  ===========================================================================
nuts_gdp       nama_10r_3gdp          GDP at current market prices by NUTS 3 region: million euro, euro per
                                      inhabitant, PPS (EU27 from 2020) per inhabitant; annual
nuts_une       lfst_r_lfu3rt          unemployment rate 15-74, all education levels, % (LFS), NUTS 2; annual
nuts_pop       demo_r_pjanaggr3       population on 1 January, total, NUTS 3; annual (stock on 1 January)
=============  =====================  ===========================================================================

Rules: values are the JSON numbers' literal text (``parse_float=str``), never rescaled (euro stays euro, million
stays million: the unit says so); every dimension except ``geo``/``time``/``freq`` is folded into the indicator
(``namq_10_gdp|unit=CLV_PCH_PRE|s_adj=SCA|na_item=B1GQ``) with its label in flags; Eurostat status codes
(``p`` provisional, ``e`` estimated, ``b`` break in series, ``c`` confidential …) kept with the label the response
gives; cells without a value are not stored (counted by status). **Seasonal adjustment** verbatim: the ``s_adj``
code and its Eurostat label (``flags.seasonal_adjustment`` / ``seasonal_adjustment_label``); sub-annual datasets
without an ``s_adj`` dimension (HICP) carry ``"not_stated_by_provider"``. The dataset ``updated`` timestamp is in
the coverage note; a new Eurostat release becomes a new version of the atlas table (vintages kept by the atlas).
Licence: Eurostat reuse policy (Commission Decision 2011/833/EU; CC BY 4.0), acknowledgement required.

    uv run reva atlas ingest atlas.eurostat_macro [--dataset nuts_gdp] [--fixture]
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import polars as pl

from reva.adapters.connectors import atlas_fiscal_common as fc
from reva.adapters.connectors import econ_regions_kg as rkg
from reva.atlas.sources import AtlasSourceSpec
from reva.atlas.tables import ATLAS_SCHEMA, country_iso3
from reva.kernel.errors import SourceResponseError

CONNECTOR_NAME = "atlas.eurostat_macro"
CONNECTOR_VERSION = "1.0.1"  # 1.0.1: 1.0.0 was registered in the dev DB from an earlier working-tree state
PARSER_VERSION = "atlas_eurostat_macro/1"
SOURCE = "eurostat"
SOURCE_KEY = "eurostat.dissemination"
HOST = "ec.europa.eu"
BASE = f"https://{HOST}/eurostat/api/dissemination/statistics/1.0/data"
GEO_SCHEME = "eurostat"
MAX_BYTES = 20_000_000
LICENCE = "Eurostat reuse policy (Commission Decision 2011/833/EU; CC BY 4.0), acknowledgement required"
RIGHTS: dict[str, Any] = {
    "licence": LICENCE,
    "licence_url": "https://ec.europa.eu/eurostat/about-us/policies/copyright",
    "attribution": "Source: Eurostat (<dataset code>)",
    "licence_checked": "2026-10-17",
    "source_key": SOURCE_KEY,
}


@dataclass(frozen=True)
class EsQuery:
    dataset: str  # atlas dataset key
    code: str  # Eurostat dataset code
    title: str
    filters: str  # query string (fixed dimensions)
    freq: str  # expected frequency code (A / Q / M)
    regional: bool = False
    reference: str = ""  # what a period means (kept in flags.reference)
    fixture_geos: tuple[str, ...] = ("DE", "EL", "FR", "EU27_2020")
    fixture_since: str = "2024"
    notes: str = ""

    def url(self, fixture: bool = False) -> str:
        u = f"{BASE}/{self.code}?{self.filters}&format=JSON&lang=EN"
        if fixture:
            u += "".join(f"&geo={g}" for g in self.fixture_geos) + f"&sinceTimePeriod={self.fixture_since}"
        return u


_NUTS_FIX = ("FR", "FR1", "FR10", "FR101", "FRY", "FRY1", "FRY10", "EL", "EL3", "EL30", "EL303", "EU27_2020")

QUERIES: tuple[EsQuery, ...] = (
    EsQuery(
        "hicp_m",
        "prc_hicp_minr",
        "Harmonised index of consumer prices (HICP) - ECOICOP ver.2 - monthly data",
        "coicop18=TOTAL&unit=I25&unit=RCH_M&unit=RCH_A",
        "M",
        reference="monthly index / rate of change",
        notes="ECOICOP ver. 2 dataset (the COICOP 1999 dataset prc_hicp_manr stops in December 2025).",
    ),
    EsQuery(
        "une_m",
        "une_rt_m",
        "Unemployment by sex and age - monthly data",
        "sex=T&age=TOTAL&unit=PC_ACT",
        "M",
        reference="monthly unemployment rate (ILO definition, LFS-based monthly estimates)",
    ),
    EsQuery(
        "gdp_q",
        "namq_10_gdp",
        "GDP and main components (output, expenditure and income) - quarterly data",
        "na_item=B1GQ&unit=CP_MEUR&unit=CLV_PCH_PRE&unit=CLV_PCH_SM",
        "Q",
        reference="quarterly flow",
    ),
    EsQuery(
        "gov_debt_q",
        "gov_10q_ggdebt",
        "Quarterly government debt",
        "sector=S13&na_item=GD&unit=MIO_EUR&unit=PC_GDP",
        "Q",
        reference="stock at the end of the quarter",
    ),
    EsQuery(
        "ip_m",
        "sts_inpr_m",
        "Production in industry - monthly data",
        "nace_r2=B-D&indic_bt=PRD&unit=I21&s_adj=SCA&s_adj=NSA",
        "M",
        reference="monthly volume index",
    ),
    EsQuery(
        "nuts_gdp",
        "nama_10r_3gdp",
        "Gross domestic product (GDP) at current market prices by NUTS 3 region",
        "unit=MIO_EUR&unit=EUR_HAB&unit=PPS_EU27_2020_HAB",
        "A",
        regional=True,
        reference="calendar-year flow, current prices",
        fixture_geos=_NUTS_FIX,
        fixture_since="2021",
    ),
    EsQuery(
        "nuts_une",
        "lfst_r_lfu3rt",
        "Unemployment rates by sex, age, educational attainment level and NUTS 2 region (%)",
        "sex=T&age=Y15-74&isced11=TOTAL&unit=PC",
        "A",
        regional=True,
        reference="annual average, Labour Force Survey, ILO definition, ages 15-74",
        fixture_geos=_NUTS_FIX,
        fixture_since="2022",
    ),
    EsQuery(
        "nuts_pop",
        "demo_r_pjanaggr3",
        "Population on 1 January by broad age group, sex and NUTS 3 region",
        "sex=T&age=TOTAL&unit=NR",
        "A",
        regional=True,
        reference="stock on 1 January of the year",
        fixture_geos=_NUTS_FIX,
        fixture_since="2023",
    ),
)
QUERIES_BY_DATASET = {q.dataset: q for q in QUERIES}
_FOLD_SKIP = frozenset({"geo", "time", "freq"})


class EurostatMacroError(SourceResponseError):
    def __init__(self, msg: str, **ctx: Any) -> None:
        super().__init__(msg, retryable=False, kind="parse_error", **ctx)


@dataclass
class EsReport:
    dataset: str
    code: str
    updated: str = ""
    label: str = ""
    cells: int = 0
    no_value_by_status: Counter[str] = field(default_factory=Counter)
    statuses: Counter[str] = field(default_factory=Counter)
    sa: Counter[str] = field(default_factory=Counter)
    zeros: int = 0
    geos: dict[str, str] = field(default_factory=dict)
    levels: Counter[str] = field(default_factory=Counter)
    periods: tuple[str, str] | None = None

    def unmapped(self) -> list[str]:
        return sorted(g for g in self.geos if len(g) != 2 or country_iso3(g, GEO_SCHEME) is None)

    def as_dict(self) -> dict[str, Any]:
        unm = [g for g in self.unmapped() if not rkg.is_nuts_region(g)]
        return {
            "dataset": self.dataset,
            "eurostat_dataset": self.code,
            "updated": self.updated,
            "cells": self.cells,
            "no_value_by_status": dict(self.no_value_by_status),
            "flagged_status": dict(self.statuses),
            "seasonal_adjustment": dict(self.sa),
            "reported_zeros": self.zeros,
            "geos": len(self.geos),
            "nuts_levels": dict(self.levels),
            "unmapped_non_nuts": unm,
            "periods": list(self.periods) if self.periods else None,
        }


def _categories(d: Mapping[str, Any], dim: str) -> tuple[list[str], dict[str, str]]:
    c = d["dimension"][dim]["category"]
    idx = c.get("index")
    if isinstance(idx, dict):
        order = sorted(idx, key=lambda k: int(idx[k]))
    elif isinstance(idx, list):
        order = list(idx)
    else:  # a single-category dimension may omit the index
        order = list((c.get("label") or {}).keys())
    return order, {str(k): str(v) for k, v in (c.get("label") or {}).items()}


def parse_jsonstat(q: EsQuery, body: bytes, sha: str) -> tuple[pl.DataFrame, EsReport, dict[str, str]]:
    """One Eurostat JSON-stat 2.0 response -> atlas frame, report, and the ``geo`` labels of the response."""
    try:
        d = json.loads(body.decode("utf-8"), parse_float=str, parse_int=str)
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise EurostatMacroError(f"Eurostat {q.code}: not JSON") from e
    if "error" in d:
        raise EurostatMacroError(f"Eurostat {q.code}: provider error", detail=str(d["error"])[:300])
    ids, size = d.get("id"), d.get("size")
    if not isinstance(ids, list) or not isinstance(size, list) or "geo" not in ids or "time" not in ids:
        raise EurostatMacroError(f"Eurostat {q.code}: not a JSON-stat dataset")
    size = [int(s) for s in size]
    cats: list[list[str]] = []
    labels: dict[str, dict[str, str]] = {}
    for dim in ids:
        order, lab = _categories(d, dim)
        cats.append(order)
        labels[dim] = lab
    if "freq" in ids and set(cats[ids.index("freq")]) != {q.freq}:
        raise EurostatMacroError(f"Eurostat {q.code}: unexpected frequency", got=cats[ids.index("freq")])
    status_labels = ((d.get("extension") or {}).get("status") or {}).get("label") or {}
    status = d.get("status") or {}
    if isinstance(status, list):
        status = {str(i): s for i, s in enumerate(status) if s}
    values = d.get("value") or {}
    if isinstance(values, list):
        values = {str(i): v for i, v in enumerate(values) if v is not None}
    stride = [1] * len(size)
    for i in range(len(size) - 2, -1, -1):
        stride[i] = stride[i + 1] * size[i + 1]
    folded = [dim for dim in ids if dim not in _FOLD_SKIP]
    rep = EsReport(q.dataset, q.code, updated=str(d.get("updated", "")), label=str(d.get("label", ""))[:300])
    cols: dict[str, list[Any]] = {c: [] for c in (*fc._COLS, "iso3")}
    starts: list[Any] = []

    def code_at(pos: int) -> dict[str, str]:
        return {dim: cats[i][(pos // stride[i]) % size[i]] for i, dim in enumerate(ids)}

    for k, st in status.items():  # cells that only carry a status (no value): not cells, counted
        if k not in values:
            rep.no_value_by_status[str(st)] += 1
    for k, v in values.items():
        pos = int(k)
        code = code_at(pos)
        value = fc.strict_decimal(str(v)) if v not in (None, "") else None
        if value is None:
            rep.no_value_by_status[str(status.get(k, "(none)"))] += 1
            continue
        start, end = fc.sdmx_period(q.freq, code["time"])
        geo = code["geo"]
        indicator = q.code + "".join(f"|{dim}={code[dim]}" for dim in folded)
        unit = labels["unit"].get(code["unit"], code["unit"]) if "unit" in code else "not stated by provider"
        flags: dict[str, Any] = {"geo_label": labels["geo"].get(geo, "")}
        if q.reference:
            flags["reference"] = q.reference
        for dim in folded:
            flags[f"{dim}_label"] = labels[dim].get(code[dim], "")
        if q.freq in ("M", "Q"):
            if "s_adj" in code:
                flags["seasonal_adjustment"] = code["s_adj"]
                flags["seasonal_adjustment_label"] = labels["s_adj"].get(code["s_adj"], "")
            else:
                flags["seasonal_adjustment"] = "not_stated_by_provider"
            rep.sa[str(flags["seasonal_adjustment"])] += 1
        st = status.get(k)
        if st:
            flags["provider_flag"] = st
            flags["provider_flag_label"] = status_labels.get(st, "(not labelled in the response)")
            rep.statuses[str(st)] += 1
            if "e" in str(st):
                flags["estimate_kind"] = "estimate"
            elif "p" in str(st):
                flags["estimate_kind"] = "provisional"
        iso3: str | None = None
        if q.regional:
            if len(geo) == 2:
                iso3 = country_iso3(geo, GEO_SCHEME)
                flags["nuts_level"] = "0"
            elif rkg.is_nuts_region(geo):
                flags["nuts_level"] = str(len(geo) - 2)
                flags["country_iso3"] = rkg.nuts_country(geo)
                rep.levels[f"NUTS {len(geo) - 2}"] += 1
        if float(value) == 0:
            rep.zeros += 1
        rep.geos.setdefault(geo, labels["geo"].get(geo, ""))
        starts.append(start)
        cols["indicator"].append(indicator)
        cols["provider_geo"].append(geo)
        cols["iso3"].append(iso3)
        cols["period_start"].append(start)
        cols["period_end"].append(end)
        cols["value"].append(value)
        cols["unit"].append(unit[:100])
        cols["flags"].append(fc.canon(flags))
        cols["raw_sha256"].append(sha)
        cols["selector"].append(fc.canon({"dataset": q.code, "value_index": pos, **{x: code[x] for x in ids}}))
    if not cols["indicator"]:
        raise EurostatMacroError(f"Eurostat {q.code}: no cell with a value")
    rep.cells = len(cols["indicator"])
    rep.periods = (min(starts).isoformat(), max(starts).isoformat())
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
        pl.lit(q.dataset).alias("dataset"),
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
    dup = df.select(pl.struct("indicator", "provider_geo", "period_start", "period_end").is_duplicated().sum()).item()
    if dup:
        raise EurostatMacroError(f"Eurostat {q.code}: {dup} duplicate cells")
    return df, rep, labels["geo"]


def coverage_note(q: EsQuery, rep: EsReport) -> str:
    d = rep.as_dict()
    return (
        f"Eurostat {q.code} '{rep.label or q.title}' (updated {rep.updated}); request {q.url()}; {rep.cells} cells,"
        f" {len(rep.geos)} geographies (NUTS levels {dict(rep.levels)}), periods {d['periods']}. Cells without a"
        f" value are not stored: {dict(rep.no_value_by_status)} (by Eurostat status). Flagged statuses kept with"
        f" their labels: {dict(rep.statuses)}. Seasonal adjustment as stated: {dict(rep.sa)}. Reported zeros kept"
        f" ({rep.zeros}). Non-NUTS aggregates kept unmapped: {', '.join(d['unmapped_non_nuts']) or 'none'}."
        f" Values verbatim (no rescaling). {q.notes} Licence: {LICENCE}."
    )[:4000]


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=CONNECTOR_NAME,
        version=CONNECTOR_VERSION,
        source_key=SOURCE_KEY,
        budget_key=SOURCE_KEY,
        atlas_source=SOURCE,
        provider="Eurostat (statistics API, JSON-stat 2.0): short-term macro series and NUTS regional series",
        licence=LICENCE,
        allowed_hosts=(HOST,),
        datasets=tuple(q.dataset for q in QUERIES),
        dataset_ids=tuple(q.code for q in QUERIES),
        cadence_seconds=30 * 86_400,
        planned_requests=len(QUERIES),
        max_response_bytes=MAX_BYTES,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        code_digest=fc.module_digest(rkg, sys.modules[__name__]),
        field_mappings={
            "geo_scheme": GEO_SCHEME,
            "tables": {q.code: f"{SOURCE}.{q.dataset}" for q in QUERIES},
            "regional_tables": [q.dataset for q in QUERIES if q.regional],
        },
        notes="Eight JSON-stat requests per full run (~5 MB measured 2026-10-17); NUTS regions also loaded into"
        " the KG (econ_regions_kg).",
    )


async def atlas_ingest(run: Any) -> dict[str, Any]:
    from reva.operations.budgets import BudgetExhausted

    fixture = run.origin == "test_fixture"
    out: dict[str, Any] = {"fetched": [], "errors": [], "tables": [], "reports": {}}
    regions: list[dict[str, rkg.Region]] = []
    kg_raws: list[str] = []
    retrieved = None
    for q in QUERIES:
        if not run.wants(q.dataset):
            continue
        try:
            raw = await run.get(
                q.url(fixture), key=f"data:{q.code}", media_types=("application/json",), max_bytes=MAX_BYTES
            )
            if not raw.body:
                raise SourceResponseError(f"Eurostat {q.code}: empty response body", retryable=True)
        except BudgetExhausted:
            raise
        except Exception as e:
            out["errors"].append({"dataset": q.dataset, "error": type(e).__name__, "message": str(e)[:300]})
            continue
        sha = run.archive(raw)
        out["fetched"].append({"item": f"data:{q.code}", "bytes": len(raw.body), "sha256": sha})
        try:
            frame, rep, geo_labels = parse_jsonstat(q, raw.body, sha)
            reg = run.register(
                dataset=q.dataset,
                frame=frame,
                raw_sha256s=[sha],
                coverage_note=coverage_note(q, rep),
                geo_scheme=None if q.regional else GEO_SCHEME,
                rights=dict(RIGHTS),
            )
        except BudgetExhausted:
            raise
        except Exception as e:
            details = getattr(e, "details", None)
            out["errors"].append(
                {
                    "dataset": q.dataset,
                    "stage": "parse/register",
                    "error": type(e).__name__,
                    "message": str(e)[:500],
                    "details": json.loads(json.dumps(details, default=str)) if details else None,
                }
            )
            continue
        out["reports"][q.dataset] = rep.as_dict()
        out["tables"].append(
            {
                "table": f"{SOURCE}.{q.dataset}",
                "dataset_key": reg.get("dataset_key"),
                "created": reg.get("created"),
                "rows": reg.get("row_count"),
                "countries": (reg.get("coverage") or {}).get("countries"),
                "artifact_sha256": reg.get("artifact_sha256"),
            }
        )
        if q.regional:
            present = set(frame["provider_geo"].unique().to_list())
            regions.append(
                rkg.nuts_regions(
                    {g: lab for g, lab in geo_labels.items() if g in present},
                    table=f"{SOURCE}.{q.dataset}",
                    described_by=f"Eurostat {q.code}, updated {rep.updated}",
                    raw_sha256=sha,
                    dataset_code=q.code,
                )
            )
            kg_raws.append(sha)
            retrieved = raw.retrieved_at
    if regions and retrieved is not None:
        out["kg"] = rkg.load(
            run.engine,
            run.store,
            rkg.merge(regions),
            source_key=SOURCE_KEY,
            raw_sha256s=kg_raws,
            retrieved_at=retrieved,
            origin=run.origin,
            licence=LICENCE,
            table_titles={f"{SOURCE}.{q.dataset}": f"Eurostat {q.code} - {q.title}" for q in QUERIES},
            connector=CONNECTOR_NAME,
        )
    out["ok"] = not out["errors"]
    return out


__all__ = [
    "CONNECTOR_NAME",
    "QUERIES",
    "QUERIES_BY_DATASET",
    "EsQuery",
    "atlas_ingest",
    "atlas_source",
    "parse_jsonstat",
]
