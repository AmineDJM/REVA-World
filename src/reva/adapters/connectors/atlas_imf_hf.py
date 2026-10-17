"""IMF monthly and quarterly macro series -> atlas tables (V4, agent V4ECON).

Extends :mod:`atlas_imf` (V3 A-IMF, **annual** WEO/BOP/GFS/ER/MFS_IR/IL/IMTS) with the sub-annual series of the
same IMF data portal SDMX 2.1 API (``https://api.imf.org/external/sdmx/2.1``, open, no key; the successors of the
retired IFS). Same source key ``imf.sdmx`` and budget; same SDMX-ML structure-specific parser
(:func:`atlas_imf.parse_sdmx`) and geography scheme ``imf`` (``registry/atlas/geo_aliases/a_imf.yaml``).

=========  =================  =======================================================================
dataset    IMF dataflow       content (frequency as published, never averaged)
=========  =================  =======================================================================
cpi_m      IMF.STA:CPI        national consumer price index, all items (COICOP 1999 ``_T``): index and
                              year-over-year % change, monthly
qnea_q     IMF.STA:QNEA       quarterly GDP (``B1GQ``) at constant and current prices, domestic currency,
                              y/y and q/q % change, every seasonal-adjustment variant published
ls_mq      IMF.STA:LS         unemployment rate (% ) and unemployed persons, monthly and quarterly
pi_m       IMF.STA:PI         industrial production and manufacturing indexes, unadjusted and
                              seasonally adjusted, monthly
er_m       IMF.STA:ER         national currency per US dollar, period average, monthly
mfs_ir_m   IMF.STA:MFS_IR     monetary-policy-related rate, money-market rate, Treasury-bill rate,
                              government bond yield (% per annum), monthly
bop_q      IMF.STA:BOP        current account balance and goods & services balance (net, US dollars),
                              quarterly (BPM6)
qgfs_q     IMF.STA:QGFS       general and central government (excl. social security): revenue, expense,
                              net lending/borrowing (transactions) and gross debt at market value (stock
                              positions), domestic currency, quarterly
=========  =================  =======================================================================

Honesty rules:

* ``OBS_VALUE`` verbatim as a strict decimal; absent -> no cell (counted). The series ``SCALE`` attribute is the
  IMF display scale, kept in ``flags.display_scale`` and never applied (values are in full units).
* Periods at the provider's precision: ``2025-M01`` -> ``[2025-01-01, 2025-02-01)``, ``2025-Q1`` -> one quarter;
  ``flags.frequency`` = the IMF ``FREQUENCY`` code.
* **Seasonal adjustment verbatim**: when the IMF publishes it (QNEA ``S_ADJUSTMENT`` dimension; PI ``SA_*``
  transformation codes) ``flags.seasonal_adjustment`` is the code and ``flags.seasonal_adjustment_label`` the IMF
  codelist label; otherwise ``flags.seasonal_adjustment = "not_stated_by_provider"`` (never assumed NSA).
* ``DERIVATION_TYPE`` (O reported official data, SE staff estimate, SP staff projection …) and ``STATUS`` kept
  with their IMF labels; SP / F -> ``estimate_kind = projection``, staff estimates -> ``estimate``.
* Index reference periods (``REFERENCE_PERIOD``, ``COMMON_REFERENCE_PERIOD``, ``REFERENCE_PERIOD_REPORTED``) and
  ``METHODOLOGY`` are kept in flags: two index series with different reference periods are different series.
* Vintages: the IMF publishes dated vintages of each flow (``CPI_2026_MAY_VINTAGE`` …). This connector reads the
  current flow; its dataflow version and the dataset ``UPDATE_DATE`` go into the coverage note, and a changed
  release becomes a new version of the atlas table (the atlas revision mechanism keeps every earlier vintage).

Labels below are quoted from the IMF codelists (``dataflow/IMF.STA/<flow>/latest?references=all``, retrieved
2026-10-17). A code the response uses that is not listed here fails the table (fail closed: a unit is never
guessed).

    uv run reva atlas ingest atlas.imf_hf [--dataset cpi_m] [--fixture]
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import polars as pl

from reva.adapters.connectors import atlas_fiscal_common as fc
from reva.adapters.connectors import atlas_imf as imf
from reva.atlas.sources import AtlasSourceSpec
from reva.atlas.tables import ATLAS_SCHEMA, country_iso3
from reva.kernel.errors import SourceResponseError

CONNECTOR_NAME = "atlas.imf_hf"
CONNECTOR_VERSION = "1.0.0"
PARSER_VERSION = "atlas_imf_hf/1"
SOURCE = "imf"
SOURCE_KEY = imf.SOURCE_KEY
HOST = imf.HOST
BASE = imf.BASE
GEO_SCHEME = imf.GEO_SCHEME
MEDIA = ("application/vnd.sdmx.structurespecificdata+xml", "application/xml")
MAX_BYTES = 60_000_000
LICENCE = imf.LICENCE
RIGHTS: dict[str, Any] = {
    "licence": LICENCE,
    "licence_url": "https://www.imf.org/external/terms.htm",
    "attribution": "International Monetary Fund, <dataset>, https://data.imf.org/en/datasets/IMF.STA:<flow>",
    "provider_license_attribute": "© International Monetary Fund Copyright. All Rights Reserved.",
    "licence_review": "pending maintainer review (IMF terms; datasets PUBLIC_OPEN, citation requested)",
    "source_key": SOURCE_KEY,
}

#: IMF CL_DERIVATION_TYPE (quoted)
DERIVATION_LABELS = {
    "SEME": "Staff estimates using group mean imputation methods",
    "SEHI": "Staff estimates using historical imputation methods",
    "R": "Raw data (no derivation)",
    "SC": "IMF Staff calculations (derived value)",
    "SCC": "IMF Staff calculations (derived from counterpart data)",
    "SE": "IMF Staff estimates",
    "SP": "IMF Staff projections",
    "M": "Mixed-type data",
    "O": "Reported official data",
    "OU": "Reported official data converted to US dollar",
    "FA": "Adjusted using Fund records",
    "TPD": "Third party data",
}
#: IMF CL_FREQ codes this connector accepts
FREQ_LABELS = {"M": "Monthly", "Q": "Quarterly", "A": "Annual"}
#: QNEA CL_S_ADJUSTMENT (quoted)
S_ADJUSTMENT_LABELS = {
    "SA": "Seasonally adjusted (SA)",
    "SAF": "Seasonally adjusted factors",
    "NSA": "Not seasonally adjusted (NSA)",
    "TC": "Trend cycle data",
    "WD": "Working day adjusted",
    "_Z": "Not applicable",
    "T": "Trend",
    "C": "Trend-cycle data, calendar adjusted",
    "R": "Trend-cycle data, not calendar adjusted",
    "K": "Calendar component",
    "X": "Seasonal component",
    "M": "Seasonal and calendar components",
    "I": "Irregular component",
    "N": "Neither seasonally adjusted nor calendar adjusted data",
    "S": "Seasonally adjusted data, not calendar adjusted",
    "W": "Calendar adjusted data, not seasonally adjusted",
    "Y": "Calendar and seasonally adjusted data",
}


@dataclass(frozen=True)
class HfFlow:
    """One IMF dataflow request -> one atlas table."""

    dataset: str
    flow: str
    title: str
    dims: tuple[str, ...]  # series dimensions in SDMX key order (COUNTRY first, FREQUENCY last)
    key: str  # SDMX key without the COUNTRY part (prefixed with '{geo}.')
    fold: tuple[str, ...]  # dimensions folded into the indicator (first one = the indicator prefix)
    labels: Mapping[str, Mapping[str, str]]  # dimension -> code -> IMF label (every requested code)
    unit_dims: tuple[str, ...]  # dimensions whose labels make the unit (joined with ", ")
    freqs: tuple[str, ...]
    sa_dim: str | None = None  # dimension stating seasonal adjustment
    sa_codes: Mapping[str, str] = field(default_factory=dict)  # codes of sa_dim that state an adjustment
    fixed_unit: str = ""
    notes: str = ""

    def url(self, geos: str = "", start: str | None = None) -> str:
        u = f"{BASE}/data/IMF.STA,{self.flow}/{geos}.{self.key}"
        return f"{u}?startPeriod={start}" if start else u


_PI_SA = {
    "SA_IX": "Seasonally adjusted (SA) Index",
    "SA_POP_PCH_PT": "Seasonally adjusted (SA) Period-over-preceding period percent change",
    "SA_YOY_PCH_PT": "Seasonally adjusted (SA) Year-over-year (YOY) percent change",
}

FLOWS: tuple[HfFlow, ...] = (
    HfFlow(
        dataset="cpi_m",
        flow="CPI",
        title="Consumer Price Index (CPI)",
        dims=("COUNTRY", "INDEX_TYPE", "COICOP_1999", "TYPE_OF_TRANSFORMATION", "FREQUENCY"),
        key="CPI._T.IX+YOY_PCH_PA_PT.M",
        fold=("INDEX_TYPE", "COICOP_1999", "TYPE_OF_TRANSFORMATION"),
        labels={
            "INDEX_TYPE": {"CPI": "Consumer price index (CPI)"},
            "COICOP_1999": {"_T": "All Items"},
            "TYPE_OF_TRANSFORMATION": {
                "IX": "Index",
                "YOY_PCH_PA_PT": "Period average, Year-over-year (YOY) percent change",
            },
        },
        unit_dims=("TYPE_OF_TRANSFORMATION",),
        freqs=("M",),
        notes="National CPI only (the EU HICP comes from Eurostat, atlas.eurostat_macro hicp_m); COICOP divisions"
        " and weights not taken. Index reference period in flags (reference_period / common_reference_period).",
    ),
    HfFlow(
        dataset="qnea_q",
        flow="QNEA",
        title="National Economic Accounts (NEA), Quarterly Data",
        dims=("COUNTRY", "INDICATOR", "PRICE_TYPE", "S_ADJUSTMENT", "TYPE_OF_TRANSFORMATION", "FREQUENCY"),
        key="B1GQ.Q+V..XDC+YOY_PCH_PT+POP_PCH_PT.Q",
        fold=("INDICATOR", "PRICE_TYPE", "S_ADJUSTMENT", "TYPE_OF_TRANSFORMATION"),
        labels={
            "INDICATOR": {"B1GQ": "Gross domestic product (GDP)"},
            "PRICE_TYPE": {"Q": "Constant prices", "V": "Current prices"},
            "S_ADJUSTMENT": S_ADJUSTMENT_LABELS,
            "TYPE_OF_TRANSFORMATION": {
                "XDC": "Domestic currency",
                "YOY_PCH_PT": "Year-over-year (YOY) percent change",
                "POP_PCH_PT": "Period-over-preceding period percent change",
            },
        },
        unit_dims=("TYPE_OF_TRANSFORMATION", "PRICE_TYPE"),
        freqs=("Q",),
        sa_dim="S_ADJUSTMENT",
        sa_codes=S_ADJUSTMENT_LABELS,
        notes="Every seasonal-adjustment variant the IMF publishes is kept as its own series"
        " (flags.seasonal_adjustment). Constant-price chain-linking type in flags.vindex_type.",
    ),
    HfFlow(
        dataset="ls_mq",
        flow="LS",
        title="Labor Statistics (LS)",
        dims=("COUNTRY", "INDICATOR", "TYPE_OF_TRANSFORMATION", "FREQUENCY"),
        key="U+UP.PT+PE.M+Q",
        fold=("INDICATOR", "TYPE_OF_TRANSFORMATION"),
        labels={
            "INDICATOR": {"U": "Unemployment rate", "UP": "Unemployed persons"},
            "TYPE_OF_TRANSFORMATION": {"PT": "Percent", "PE": "Persons"},
        },
        unit_dims=("TYPE_OF_TRANSFORMATION",),
        freqs=("M", "Q"),
        notes="The IMF LS dataflow has no seasonal-adjustment dimension or attribute: not stated by the provider.",
    ),
    HfFlow(
        dataset="pi_m",
        flow="PI",
        title="Production Indexes (PI)",
        dims=("COUNTRY", "PRODUCTION_INDEX", "TYPE_OF_TRANSFORMATION", "FREQUENCY"),
        key="IND+C.IX+SA_IX.M",
        fold=("PRODUCTION_INDEX", "TYPE_OF_TRANSFORMATION"),
        labels={
            "PRODUCTION_INDEX": {"IND": "Industrial Production", "C": "Manufacturing"},
            "TYPE_OF_TRANSFORMATION": {"IX": "Index", "SA_IX": "Seasonally adjusted (SA) Index"},
        },
        unit_dims=("TYPE_OF_TRANSFORMATION",),
        freqs=("M",),
        sa_dim="TYPE_OF_TRANSFORMATION",
        sa_codes=_PI_SA,
        notes="Seasonal adjustment is stated only by the SA_* transformation codes; 'IX' states none.",
    ),
    HfFlow(
        dataset="er_m",
        flow="ER",
        title="Exchange Rates (ER)",
        dims=("COUNTRY", "INDICATOR", "TYPE_OF_TRANSFORMATION", "FREQUENCY"),
        key="XDC_USD.PA_RT.M",
        fold=("INDICATOR", "TYPE_OF_TRANSFORMATION"),
        labels={
            "INDICATOR": {"XDC_USD": "Domestic currency per US Dollar"},
            "TYPE_OF_TRANSFORMATION": {"PA_RT": "Period average"},
        },
        unit_dims=("INDICATOR",),
        freqs=("M",),
        notes="Period averages only; end-of-period monthly rates (EOP_RT) are published but not taken (disk"
        " budget). Annual rates are in atlas.imf er.",
    ),
    HfFlow(
        dataset="mfs_ir_m",
        flow="MFS_IR",
        title="Monetary and Financial Statistics (MFS), Interest Rate",
        dims=("COUNTRY", "INDICATOR", "FREQUENCY"),
        key="MFS166_RT_PT_A_PT+MMRT_RT_PT_A_PT+IRTBILL_RT_PT_A_PT+S13BONDS_RT_PT_A_PT.M",
        fold=("INDICATOR",),
        labels={
            "INDICATOR": {
                "MFS166_RT_PT_A_PT": "Monetary policy-related, Rate, Percent per annum",
                "MMRT_RT_PT_A_PT": "Money market Rate, Percent per annum",
                "IRTBILL_RT_PT_A_PT": "Treasury bills, Rate, Percent per annum",
                "S13BONDS_RT_PT_A_PT": "Government bonds yields, Rate, Percent per annum",
            }
        },
        unit_dims=(),
        fixed_unit="percent per annum",
        freqs=("M",),
        notes="Monthly central-bank policy rates also come from BIS (atlas.bis cbpol); both kept per source.",
    ),
    HfFlow(
        dataset="bop_q",
        flow="BOP",
        title="Balance of Payments (BOP)",
        dims=("COUNTRY", "BOP_ACCOUNTING_ENTRY", "INDICATOR", "UNIT", "FREQUENCY"),
        key="NETCD_T.CAB+GS.USD.Q",
        fold=("INDICATOR", "BOP_ACCOUNTING_ENTRY", "UNIT"),
        labels={
            "BOP_ACCOUNTING_ENTRY": {"NETCD_T": "Net (credits less debits)"},
            "INDICATOR": {"CAB": "Current account balance (credit less debit)", "GS": "Goods and services"},
            "UNIT": {"USD": "US dollar"},
        },
        unit_dims=("UNIT",),
        freqs=("Q",),
        notes="Quarterly flows as reported (BPM6); the BOP dataflow states no seasonal adjustment.",
    ),
    HfFlow(
        dataset="qgfs_q",
        flow="QGFS",
        title="Quarterly Government Finance Statistics (QGFS)",
        dims=("COUNTRY", "ACCOUNTS", "SECTOR", "INDICATOR", "FREQUENCY"),
        key="SOO+BS.S13+S1311.G1_T_XDC+G2_T_XDC+GNLB_T_XDC+G63GMV_L_SP_XDC.Q",
        fold=("INDICATOR", "ACCOUNTS", "SECTOR"),
        labels={
            "ACCOUNTS": {"SOO": "Statement of operations", "BS": "Balance Sheet Account"},
            "SECTOR": {"S13": "General government", "S1311": "Central government excluding social security"},
            "INDICATOR": {
                "G1_T_XDC": "Revenue, Transactions",
                "G2_T_XDC": "Expense, Transactions",
                "GNLB_T_XDC": "Net lending (+) / net borrowing (-), Transactions",
                "G63GMV_L_SP_XDC": "Gross debt at market value, Liabilities, Stock positions",
            },
        },
        unit_dims=(),
        fixed_unit="domestic currency",
        freqs=("Q",),
        notes="Gross debt is a stock at the end of the quarter (period = the quarter); revenue/expense/net"
        " lending are flows over the quarter. Domestic currency, full units (display SCALE kept in flags).",
    ),
)
FLOWS_BY_DATASET = {f.dataset: f for f in FLOWS}

#: series attributes copied into flags (verbatim) when present
SERIES_ATTRS = (
    "METHODOLOGY",
    "REFERENCE_PERIOD",
    "COMMON_REFERENCE_PERIOD",
    "REFERENCE_PERIOD_REPORTED",
    "VINDEX_TYPE",
    "OVERLAP",
)
OBS_ATTRS = ("REFERENCE_PERIOD",)

#: offline fixtures: small real responses (a few economies, recent periods)
FIXTURE_GEOS = ("BRA", "DEU", "FRA", "IND", "KOS", "USA")
FIXTURE_START = "2024"

_PERIOD = {
    "M": re.compile(r"^([0-9]{4})-M(0[1-9]|1[0-2])$"),
    "Q": re.compile(r"^([0-9]{4})-Q([1-4])$"),
    "A": re.compile(r"^([0-9]{4})$"),
}


class ImfHfError(SourceResponseError):
    def __init__(self, msg: str, **ctx: Any) -> None:
        super().__init__(msg, retryable=False, kind="parse_error", **ctx)


def imf_period(freq: str, tp: str) -> tuple[date, date]:
    """IMF ``TIME_PERIOD`` (``2025-M01``, ``2025-Q1``, ``2025``) -> half-open ``[start, end)``."""
    rx = _PERIOD.get(freq)
    m = rx.match(tp.strip()) if rx else None
    if not m:
        raise ImfHfError("unsupported IMF period", freq=freq[:4], period=tp[:20])
    y = int(m.group(1))
    if freq == "A":
        return date(y, 1, 1), date(y + 1, 1, 1)
    if freq == "Q":
        q = int(m.group(2))
        return date(y, 3 * q - 2, 1), (date(y, 3 * q + 1, 1) if q < 4 else date(y + 1, 1, 1))
    mo = int(m.group(2))
    return date(y, mo, 1), (date(y, mo + 1, 1) if mo < 12 else date(y + 1, 1, 1))


def _kind(o: Mapping[str, str]) -> str | None:
    d, s = o.get("DERIVATION_TYPE", ""), o.get("STATUS", "")
    if d == "SP" or s == "F":
        return "projection"
    if d in ("SE", "SEME", "SEHI") or s in ("E", "e"):
        return "estimate"
    if s == "P":
        return "provisional"
    return None


@dataclass
class HfReport:
    dataset: str
    series: int = 0
    obs: int = 0
    cells: int = 0
    no_value: int = 0
    zeros: int = 0
    kinds: Counter[str] = field(default_factory=Counter)
    freqs: Counter[str] = field(default_factory=Counter)
    sa: Counter[str] = field(default_factory=Counter)
    geos: set[str] = field(default_factory=set)
    periods: tuple[str, str] | None = None
    flow_ref: str = ""
    update_date: str = ""

    def as_dict(self) -> dict[str, Any]:
        unm = sorted(g for g in self.geos if country_iso3(g, GEO_SCHEME) is None)
        return {
            "dataset": self.dataset,
            "flow": self.flow_ref,
            "update_date": self.update_date,
            "series": self.series,
            "obs": self.obs,
            "cells": self.cells,
            "no_value_obs": self.no_value,
            "reported_zeros": self.zeros,
            "estimate_kinds": dict(self.kinds),
            "frequencies": dict(self.freqs),
            "seasonal_adjustment": dict(self.sa),
            "geos": len(self.geos),
            "unmapped": unm,
            "periods": list(self.periods) if self.periods else None,
        }


def parse_flow(fl: HfFlow, body: bytes, sha: str, label: str) -> tuple[pl.DataFrame, HfReport]:
    """One IMF SDMX-ML response -> atlas frame (``ATLAS_SCHEMA``) + report."""
    try:
        msg = imf.parse_sdmx(body)
    except imf.SdmxError as e:
        raise ImfHfError(f"IMF {fl.flow}: {e}") from e
    if msg.structure.get("id") != fl.flow:
        raise ImfHfError(f"IMF {fl.flow}: response is for another dataflow", got=msg.flow_ref)
    rep = HfReport(fl.dataset, flow_ref=msg.flow_ref, update_date=msg.dataset_attrs.get("UPDATE_DATE", ""))
    cols: dict[str, list[Any]] = {c: [] for c in fc._COLS}
    for sattrs, obs in msg.series:
        rep.series += 1
        missing = [d for d in fl.dims if d not in sattrs]
        if missing:
            raise ImfHfError(f"IMF {fl.flow}: series without dimension(s)", missing=missing)
        freq = sattrs["FREQUENCY"]
        if freq not in fl.freqs:
            raise ImfHfError(f"IMF {fl.flow}: unrequested frequency", freq=freq[:4])
        for d, lab in fl.labels.items():
            if sattrs[d] not in lab:
                raise ImfHfError(f"IMF {fl.flow}: code not in the quoted codelist", dimension=d, code=sattrs[d][:40])
        geo = sattrs["COUNTRY"]
        parts = [sattrs[fl.fold[0]]] + [f"{d.lower()}={sattrs[d]}" for d in fl.fold[1:]]
        if len(fl.freqs) > 1:  # monthly and quarterly cells of one measure are distinct series
            parts.append(f"freq={freq}")
        indicator = "|".join(parts)
        unit = fl.fixed_unit or ", ".join(fl.labels[d][sattrs[d]] for d in fl.unit_dims)
        base: dict[str, Any] = {"frequency": freq}
        if fl.sa_dim and sattrs.get(fl.sa_dim) in fl.sa_codes:
            base["seasonal_adjustment"] = sattrs[fl.sa_dim]
            base["seasonal_adjustment_label"] = fl.sa_codes[sattrs[fl.sa_dim]]
        else:
            base["seasonal_adjustment"] = "not_stated_by_provider"
        rep.sa[str(base["seasonal_adjustment"])] += len(obs)
        scale = sattrs.get("SCALE")
        if scale not in (None, "", "0"):
            base["display_scale"] = scale
        if sattrs.get("IFS_FLAG") == "true":
            base["ifs_series"] = True
        for a in SERIES_ATTRS:
            if sattrs.get(a):
                base[a.lower()] = sattrs[a]
        for a, v in sattrs.items():
            if "ADJ" in a and a != fl.sa_dim:
                base[a.lower()] = v
        skey = ".".join(sattrs[d] for d in fl.dims)
        for o in obs:
            rep.obs += 1
            tp = o.get("TIME_PERIOD", "")
            val = imf.decimal_text(o.get("OBS_VALUE"))
            if val is None:
                rep.no_value += 1
                continue
            start, end = imf_period(freq, tp)
            flags = dict(base)
            dt = o.get("DERIVATION_TYPE")
            if dt:
                flags["derivation_type"] = dt
                flags["derivation_type_label"] = DERIVATION_LABELS.get(dt, "(not in the quoted IMF codelist)")
            if o.get("STATUS"):
                flags["provider_status"] = o["STATUS"]
            for a in OBS_ATTRS:
                if o.get(a):
                    flags[a.lower()] = o[a]
            if o.get("CONFIDENTIALITY_STATUS") not in (None, "", "F"):
                flags["confidentiality_status"] = o["CONFIDENTIALITY_STATUS"]
            k = _kind(o)
            if k:
                flags["estimate_kind"] = k
                rep.kinds[k] += 1
            if float(val) == 0:
                rep.zeros += 1
            rep.freqs[freq] += 1
            rep.geos.add(geo)
            cols["indicator"].append(indicator)
            cols["provider_geo"].append(geo)
            cols["period_start"].append(start)
            cols["period_end"].append(end)
            cols["value"].append(val)
            cols["unit"].append(unit)
            cols["flags"].append(fc.canon(flags))
            cols["raw_sha256"].append(sha)
            cols["selector"].append(fc.canon({"file": label, "flow": msg.flow_ref, "series": skey, "time_period": tp}))
    if not cols["indicator"]:
        raise ImfHfError(f"IMF {fl.flow}: no cell with a value")
    rep.cells = len(cols["indicator"])
    starts = cols["period_start"]
    rep.periods = (min(starts).isoformat(), max(starts).isoformat())
    df = _frame(cols, fl.dataset)
    dup = df.select(pl.struct("indicator", "provider_geo", "period_start", "period_end").is_duplicated().sum()).item()
    if dup:
        raise ImfHfError(f"IMF {fl.flow}: {dup} duplicate cells (an unfolded dimension)")
    return df, rep


def _frame(cols: Mapping[str, list[Any]], dataset: str) -> pl.DataFrame:
    df = pl.DataFrame(
        dict(cols),
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
    )
    return df.select(
        pl.lit(SOURCE).alias("source"),
        pl.lit(dataset).alias("dataset"),
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
    ).cast(dict(ATLAS_SCHEMA))  # type: ignore[arg-type]


def coverage_note(fl: HfFlow, rep: HfReport) -> str:
    d = rep.as_dict()
    return (
        f"IMF IMF.STA:{fl.flow} '{fl.title}' ({rep.flow_ref}; dataset UPDATE_DATE {rep.update_date}); request"
        f" {fl.url()}; {rep.series} series, {rep.cells} cells ({rep.no_value} observations without a value are not"
        f" cells), {len(rep.geos)} economies/areas, periods {d['periods']}, frequencies {dict(rep.freqs)}."
        f" Seasonal adjustment as stated by the provider: {dict(rep.sa)}. Estimate kinds: {dict(rep.kinds)}."
        f" Unmapped IMF areas (aggregates, kept): {', '.join(d['unmapped']) or 'none'}. {fl.notes}"
        f" Licence: {LICENCE}."
    )[:4000]


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=CONNECTOR_NAME,
        version=CONNECTOR_VERSION,
        source_key=SOURCE_KEY,
        budget_key=SOURCE_KEY,
        atlas_source=SOURCE,
        provider="International Monetary Fund (data portal SDMX 2.1 API), monthly and quarterly series",
        licence=LICENCE,
        allowed_hosts=(HOST,),
        datasets=tuple(f.dataset for f in FLOWS),
        dataset_ids=tuple(f"IMF.STA:{f.flow}" for f in FLOWS),
        cadence_seconds=30 * 86_400,
        planned_requests=len(FLOWS),
        max_response_bytes=MAX_BYTES,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        code_digest=fc.module_digest(imf, sys.modules[__name__]),
        field_mappings={
            "geo_scheme": GEO_SCHEME,
            "tables": {f.flow: f"{SOURCE}.{f.dataset}" for f in FLOWS},
            "time_semantics": "monthly / quarterly periods as published (no averaging)",
        },
        notes="Eight SDMX-ML requests per full run (~60 MB measured 2026-10-17).",
    )


async def atlas_ingest(run: Any) -> dict[str, Any]:
    from reva.operations.budgets import BudgetExhausted

    fixture = run.origin == "test_fixture"
    out: dict[str, Any] = {"fetched": [], "errors": [], "tables": [], "reports": {}}
    for fl in FLOWS:
        if not run.wants(fl.dataset):
            continue
        url = fl.url("+".join(FIXTURE_GEOS), FIXTURE_START) if fixture else fl.url()
        try:
            raw = await run.get(
                url,
                key=f"data:{fl.flow}",
                media_types=MEDIA,
                headers={"Accept": imf.ACCEPT},
                max_bytes=MAX_BYTES,
                params={"kind": "sdmx_data", "flow": f"IMF.STA:{fl.flow}"},
            )
            if not raw.body:
                raise SourceResponseError(f"IMF {fl.flow}: empty response body", retryable=True)
        except BudgetExhausted:
            raise
        except Exception as e:
            out["errors"].append({"dataset": fl.dataset, "error": type(e).__name__, "message": str(e)[:300]})
            continue
        sha = run.archive(raw)
        out["fetched"].append({"item": f"data:{fl.flow}", "bytes": len(raw.body), "sha256": sha})
        try:
            frame, rep = parse_flow(fl, raw.body, sha, f"IMF.STA_{fl.flow}.xml")
            reg = run.register(
                dataset=fl.dataset,
                frame=frame,
                raw_sha256s=[sha],
                coverage_note=coverage_note(fl, rep),
                geo_scheme=GEO_SCHEME,
                rights=dict(RIGHTS),
            )
        except BudgetExhausted:
            raise
        except Exception as e:
            details = getattr(e, "details", None)
            out["errors"].append(
                {
                    "dataset": fl.dataset,
                    "stage": "parse/register",
                    "error": type(e).__name__,
                    "message": str(e)[:500],
                    "details": json.loads(json.dumps(details, default=str)) if details else None,
                }
            )
            continue
        out["reports"][fl.dataset] = rep.as_dict()
        out["tables"].append(
            {
                "table": f"{SOURCE}.{fl.dataset}",
                "dataset_key": reg.get("dataset_key"),
                "created": reg.get("created"),
                "rows": reg.get("row_count"),
                "countries": (reg.get("coverage") or {}).get("countries"),
                "artifact_sha256": reg.get("artifact_sha256"),
            }
        )
    out["ok"] = not out["errors"]
    return out


__all__ = [
    "CONNECTOR_NAME",
    "FLOWS",
    "FLOWS_BY_DATASET",
    "HfFlow",
    "atlas_ingest",
    "atlas_source",
    "imf_period",
    "parse_flow",
]
