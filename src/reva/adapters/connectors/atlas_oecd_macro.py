"""OECD short-term indicators and OECD Regional Database (TL2 / TL3) -> atlas tables (V4, agent V4ECON).

Source: the OECD Data Explorer SDMX REST API (``sdmx.oecd.org/public/rest``, no key; source key ``oecd.sdmx``,
shared with A-FISCAL / A-ILO / A-SOCIETY / A-TRADE). Responses are requested as **code-only** SDMX-CSV
(``format=csvfile``): the labelled variant repeats every label on every row and is 2.7x larger (110 MB for the
key short-term indicators alone). Labels are therefore quoted below from the OECD codelists (captured
2026-10-17 from ``format=csvfilewithlabels`` responses); a code the response uses that is not quoted fails the
table (fail closed: a unit or an adjustment is never guessed). Region names, French names, territorial
classification versions and the region hierarchy come from the OECD ``CL_REGIONAL`` codelist (one structure
request, archived like the data).

==========  ======================================  =====================================================
dataset     OECD dataflow                           series
==========  ======================================  =====================================================
kei_mq      OECD.SDD.STES DSD_KEI@DF_KEI            key short-term economic indicators, monthly and
                                                    quarterly: unemployment rate, short- and long-term
                                                    interest rates, current account % of GDP, GDP volume
                                                    growth (q/q, y/y)
reg_gdp     OECD.CFE.EDS DSD_REG_ECO@DF_GDP         regional GDP at current prices: national currency and
                                                    US dollars per person (PPP converted), TL2 / TL3 /
                                                    country, annual
reg_une     OECD.CFE.EDS DSD_REG_LAB@DF_RATES       regional unemployment rate 15-64, TL2 / TL3 / country,
                                                    annual
reg_pop     OECD.CFE.EDS DSD_REG_DEMO@DF_POP_BROAD  regional population, total, TL2 / TL3 / country, annual
==========  ======================================  =====================================================

KEI measures not taken here because other atlas sources hold the same provider series family (CPI, industrial
production, exchange rates: IMF ``atlas.imf_hf``, Eurostat ``atlas.eurostat_macro``) are documented in
``docs/dev/v4_v4econ.md``.

Rules: values verbatim (strict decimal), ``UNIT_MULT`` kept (``x10^n`` in the unit, never applied); observation
status other than ``A`` kept with its SDMX label; rows without a value are not cells. **Seasonal adjustment**
verbatim from the ``ADJUSTMENT`` dimension (``Y`` "Calendar and seasonally adjusted"); ``_Z`` "Not applicable"
on sub-annual series is recorded as such (``seasonal_adjustment = "_Z"``), never re-labelled. Regional rows keep
the OECD region code in ``provider_geo``; ``iso3`` is set only on ``TERRITORIAL_LEVEL = CTRY`` rows;
``flags.territorial_level`` and ``flags.country_iso3`` (the provider's ``COUNTRY`` attribute) on every row.
Region entities (``oecdreg:<code>``), their ``CL_REGIONAL`` parent (``part_of``) and their country
(``located_in``) go to the KG (:mod:`econ_regions_kg`).

Licence: OECD Terms and Conditions; CC BY 4.0 is the OECD default since 2024-07-01 (open-by-default policy).

    uv run reva atlas ingest atlas.oecd_macro [--dataset reg_gdp] [--fixture]
"""

from __future__ import annotations

import csv
import io
import json
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import polars as pl

from reva.adapters.connectors import atlas_fiscal_common as fc
from reva.adapters.connectors import econ_regions_kg as rkg
from reva.atlas.sources import AtlasSourceSpec
from reva.atlas.tables import ATLAS_SCHEMA, country_iso3
from reva.kernel.errors import SourceResponseError

CONNECTOR_NAME = "atlas.oecd_macro"
CONNECTOR_VERSION = "1.0.0"
PARSER_VERSION = "atlas_oecd_macro/1"
SOURCE = "oecd"
SOURCE_KEY = "oecd.sdmx"
HOST = "sdmx.oecd.org"
BASE = f"https://{HOST}/public/rest"
MEDIA = ("application/vnd.sdmx.data+csv", "text/csv", "text/plain")
STRUCT_MEDIA = ("application/vnd.sdmx.structure+xml", "application/xml", "text/xml")
MAX_BYTES = 80_000_000
LICENCE = (
    "OECD Terms and Conditions (Data Explorer: '© OECD Terms & conditions'); CC BY 4.0 is the OECD default since"
    " 2024-07-01 (open-by-default policy); no dataset-level licence in the SDMX metadata"
)
RIGHTS: dict[str, Any] = {
    "licence": LICENCE,
    "licence_url": "https://www.oecd.org/en/about/oecd-open-by-default-policy.html",
    "terms_url": "https://www.oecd.org/en/about/terms-conditions.html",
    "attribution": "OECD Data Explorer (sdmx.oecd.org); cite the OECD dataset title and year",
    "licence_checked": "2026-10-17",
    "source_key": SOURCE_KEY,
}
CL_REGIONAL_URL = f"{BASE}/codelist/OECD/CL_REGIONAL/latest"

#: SDMX CL_OBS_STATUS labels (quoted; the codes seen in these flows plus F forecast)
OBS_STATUS = {
    "A": "Normal value",
    "B": "Time series break",
    "D": "Definition differs",
    "E": "Estimated value",
    "F": "Forecast value",
    "O": "Missing value",
    "P": "Provisional value",
    "U": "Low reliability",
}
UNIT_MULT = {"0": "Units", "3": "Thousands", "6": "Millions", "9": "Billions"}
_TL = {"TL2": "TL2", "TL3": "TL3", "CTRY": "Country"}
_Z = {"_Z": "Not applicable"}


@dataclass(frozen=True)
class OFlow:
    dataset: str
    agency: str
    flow: str
    title: str
    dims: tuple[str, ...]  # SDMX key order, validated against the CSV header
    key: str
    fixture_key: str
    fold: tuple[str, ...]  # folded into the indicator (first = prefix)
    labels: Mapping[str, Mapping[str, str]]  # every code accepted per dimension (quoted)
    unit_dim: str = "UNIT_MEASURE"
    freqs: tuple[str, ...] = ("A",)
    regional: bool = False
    start: str | None = None
    fixture_start: str = "2022"
    notes: str = ""

    def url(self, fixture: bool = False) -> str:
        key = self.fixture_key if fixture else self.key
        u = f"{BASE}/data/{self.agency},{self.flow},/{key}?format=csvfile"
        s = self.fixture_start if fixture else self.start
        return f"{u}&startPeriod={s}" if s else u


_REG_DIMS_ECO = (
    "FREQ",
    "TERRITORIAL_LEVEL",
    "REF_AREA",
    "TERRITORIAL_TYPE",
    "MEASURE",
    "ACTIVITY",
    "PRICES",
    "UNIT_MEASURE",
)
_REG_DIMS_LAB = ("FREQ", "TERRITORIAL_LEVEL", "REF_AREA", "TERRITORIAL_TYPE", "MEASURE", "AGE", "SEX", "UNIT_MEASURE")
_FIX_REG = "FRA+FR1+FR10+FR101+FRL+FRL04+USA+US06+JPN+JPD+JPD13+MEX+ME09"
#: fixture mode reads only these CL_REGIONAL codes (SDMX item query), live mode the whole codelist
FIXTURE_CL_CODES = tuple(_FIX_REG.split("+"))


def cl_regional_url(fixture: bool = False) -> str:
    return f"{CL_REGIONAL_URL}/{'+'.join(FIXTURE_CL_CODES)}" if fixture else CL_REGIONAL_URL


FLOWS: tuple[OFlow, ...] = (
    OFlow(
        dataset="kei_mq",
        agency="OECD.SDD.STES",
        flow="DSD_KEI@DF_KEI",
        title="Key short-term economic indicators",
        dims=("REF_AREA", "FREQ", "MEASURE", "UNIT_MEASURE", "ACTIVITY", "ADJUSTMENT", "TRANSFORMATION"),
        key=".M+Q.UNEMP+IR3TIB+IRLT+CA_GDP+B1GQ_Q.....",
        fixture_key="FRA+USA+JPN+EA20.M+Q.UNEMP+IR3TIB+IRLT+CA_GDP+B1GQ_Q.....",
        fold=("MEASURE", "UNIT_MEASURE", "ACTIVITY", "ADJUSTMENT", "TRANSFORMATION"),
        labels={
            "FREQ": {"M": "Monthly", "Q": "Quarterly"},
            "MEASURE": {
                "UNEMP": "Unemployment",
                "IR3TIB": "Short-term interest rates",
                "IRLT": "Long-term interest rates",
                "CA_GDP": "Current account balance as percentage of GDP",
                "B1GQ_Q": "Gross domestic product, volume",
            },
            "UNIT_MEASURE": {
                "PA": "Percent per annum",
                "GR": "Growth rate",
                "PT_B1GQ": "Percentage of GDP",
                "PT_LF": "Percentage of labour force",
            },
            "ACTIVITY": {"_Z": "Not applicable", "_T": "Total - all activities"},
            "ADJUSTMENT": {"_Z": "Not applicable", "Y": "Calendar and seasonally adjusted"},
            "TRANSFORMATION": {
                "_Z": "Not applicable",
                "GY": "Growth rate, over 1 year",
                "G1": "Growth rate, period on period",
            },
        },
        freqs=("M", "Q"),
        start="1990",
        fixture_start="2025",
        notes="From 1990. Unemployment is the OECD harmonised rate (seasonally adjusted where ADJUSTMENT=Y).",
    ),
    OFlow(
        dataset="reg_gdp",
        agency="OECD.CFE.EDS",
        flow="DSD_REG_ECO@DF_GDP",
        title="Gross domestic product - Regions",
        dims=_REG_DIMS_ECO,
        key="A.TL2+TL3+CTRY.._Z.GDP._T.V.USD_PPP_PS+XDC",
        fixture_key=f"A.TL2+TL3+CTRY.{_FIX_REG}._Z.GDP._T.V.USD_PPP_PS+XDC",
        fold=("MEASURE", "ACTIVITY", "PRICES", "UNIT_MEASURE", "TERRITORIAL_TYPE"),
        labels={
            "FREQ": {"A": "Annual"},
            "TERRITORIAL_LEVEL": _TL,
            "TERRITORIAL_TYPE": _Z,
            "MEASURE": {"GDP": "Gross domestic product"},
            "ACTIVITY": {"_T": "Total - all activities"},
            "PRICES": {"V": "Current prices"},
            "UNIT_MEASURE": {"USD_PPP_PS": "US dollars per person, PPP converted", "XDC": "National currency"},
        },
        regional=True,
        notes="National-currency GDP carries the OECD unit multiplier (x10^6, never applied) and the CURRENCY in flags.",
    ),
    OFlow(
        dataset="reg_une",
        agency="OECD.CFE.EDS",
        flow="DSD_REG_LAB@DF_RATES",
        title="Labour indicators rates - Regions",
        dims=_REG_DIMS_LAB,
        key="A.TL2+TL3+CTRY.._Z.UNE_RATE.Y15T64._T.PT_LF_SUB",
        fixture_key=f"A.TL2+TL3+CTRY.{_FIX_REG}._Z.UNE_RATE.Y15T64._T.PT_LF_SUB",
        fold=("MEASURE", "AGE", "SEX", "UNIT_MEASURE", "TERRITORIAL_TYPE"),
        labels={
            "FREQ": {"A": "Annual"},
            "TERRITORIAL_LEVEL": _TL,
            "TERRITORIAL_TYPE": _Z,
            "MEASURE": {"UNE_RATE": "Unemployment rate"},
            "AGE": {"Y15T64": "From 15 to 64 years"},
            "SEX": {"_T": "Total"},
            "UNIT_MEASURE": {"PT_LF_SUB": "Percentage of labour force in the same subgroup"},
        },
        regional=True,
    ),
    OFlow(
        dataset="reg_pop",
        agency="OECD.CFE.EDS",
        flow="DSD_REG_DEMO@DF_POP_BROAD",
        title="Population by broad age groups - Regions",
        dims=_REG_DIMS_LAB,
        key="A.TL2+TL3+CTRY.._Z.POP._T._T.PS",
        fixture_key=f"A.TL2+TL3+CTRY.{_FIX_REG}._Z.POP._T._T.PS",
        fold=("MEASURE", "AGE", "SEX", "UNIT_MEASURE", "TERRITORIAL_TYPE"),
        labels={
            "FREQ": {"A": "Annual"},
            "TERRITORIAL_LEVEL": _TL,
            "TERRITORIAL_TYPE": _Z,
            "MEASURE": {"POP": "Population"},
            "AGE": {"_T": "Total"},
            "SEX": {"_T": "Total"},
            "UNIT_MEASURE": {"PS": "Persons"},
        },
        regional=True,
        notes="The OECD regional population definition (reference date) varies by country; see the OECD"
        " Regional Database metadata.",
    ),
)
FLOWS_BY_DATASET = {f.dataset: f for f in FLOWS}


class OecdMacroError(SourceResponseError):
    def __init__(self, msg: str, **ctx: Any) -> None:
        super().__init__(msg, retryable=False, kind="parse_error", **ctx)


@dataclass
class OReport:
    dataset: str
    rows: int = 0
    cells: int = 0
    no_value: Counter[str] = field(default_factory=Counter)
    statuses: Counter[str] = field(default_factory=Counter)
    sa: Counter[str] = field(default_factory=Counter)
    levels: Counter[str] = field(default_factory=Counter)
    zeros: int = 0
    geos: set[str] = field(default_factory=set)
    periods: tuple[str, str] | None = None
    structure: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "structure": self.structure,
            "rows_read": self.rows,
            "cells": self.cells,
            "no_value_by_status": dict(self.no_value),
            "flagged_status": dict(self.statuses),
            "seasonal_adjustment": dict(self.sa),
            "territorial_levels": dict(self.levels),
            "reported_zeros": self.zeros,
            "geos": len(self.geos),
            "periods": list(self.periods) if self.periods else None,
        }


@dataclass
class RegionInfo:
    """One ``CL_REGIONAL`` code."""

    code: str
    name_en: str
    name_fr: str
    parent: str | None
    level: str  # COMPLEMENT annotation (TL2 / TL3 / ...)
    family: str  # FAMILY_LABEL annotation (classification versions, e.g. TL2024+TL2021)


def parse_cl_regional(body: bytes, min_codes: int = 100) -> dict[str, RegionInfo]:
    """``CL_REGIONAL`` codelist (SDMX-ML 2.1 structure) -> code -> :class:`RegionInfo`."""
    if b"<!DOCTYPE" in body[:4096] or b"<!ENTITY" in body:
        raise OecdMacroError("CL_REGIONAL: DTD/entity declarations are refused")
    try:
        root = ET.fromstring(body)  # noqa: S314 - DTDs/entities refused above; expat fetches no external entity
    except ET.ParseError as e:
        raise OecdMacroError(f"CL_REGIONAL: malformed XML: {e}") from e
    lang = "{http://www.w3.org/XML/1998/namespace}lang"
    out: dict[str, RegionInfo] = {}
    for c in root.iter():
        if not c.tag.endswith("}Code") or not c.get("id"):
            continue
        names = {n.get(lang): (n.text or "") for n in c if n.tag.endswith("}Name")}
        parent = next((x.get("id") for p in c if p.tag.endswith("}Parent") for x in p), None)
        level = family = ""
        for a in c.iter():
            if a.tag.endswith("}Annotation"):
                typ = next((x.text for x in a if x.tag.endswith("}AnnotationType")), "")
                title = next((x.text for x in a if x.tag.endswith("}AnnotationTitle")), "") or ""
                if typ == "COMPLEMENT":
                    level = title
                elif typ == "FAMILY_LABEL":
                    family = title
        code = str(c.get("id"))
        out[code] = RegionInfo(code, names.get("en", ""), names.get("fr", ""), parent, level, family)
    if len(out) < min_codes:
        raise OecdMacroError("CL_REGIONAL: too few codes", codes=len(out))
    return out


def parse_csvfile(
    fl: OFlow, body: bytes, sha: str, label: str
) -> tuple[pl.DataFrame, OReport, dict[str, tuple[str, str]]]:
    """Code-only OECD SDMX-CSV -> atlas frame, report and ``{region code: (level, country ISO3)}``."""
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise OecdMacroError(f"{fl.flow}: not UTF-8") from e
    if text.lstrip()[:1] == "<":
        raise OecdMacroError(f"{fl.flow}: XML/HTML body where SDMX-CSV was expected")
    rd = csv.reader(io.StringIO(text))
    try:
        header = next(rd)
    except StopIteration as e:
        raise OecdMacroError(f"{fl.flow}: empty CSV") from e
    if not header or header[0] != "DATAFLOW":
        raise OecdMacroError(f"{fl.flow}: not a code-only SDMX-CSV", header=header[:5])
    ix = {h: i for i, h in enumerate(header)}
    order = [h for h in header if h in fl.dims]
    if tuple(order) != fl.dims:
        raise OecdMacroError(f"{fl.flow}: dimension order changed", got=order, expected=list(fl.dims))
    for need in ("TIME_PERIOD", "OBS_VALUE"):
        if need not in ix:
            raise OecdMacroError(f"{fl.flow}: missing column {need}")
    rep = OReport(fl.dataset)
    cols: dict[str, list[Any]] = {c: [] for c in (*fc._COLS, "iso3")}
    regions: dict[str, tuple[str, str]] = {}

    def get(r: Sequence[str], c: str) -> str:
        i = ix.get(c)
        return r[i] if i is not None and i < len(r) else ""

    starts = []
    for n, r in enumerate(rd, start=1):
        if not r:
            continue
        rep.rows += 1
        if len(r) != len(header):
            raise OecdMacroError(f"{fl.flow}: ragged CSV row", row=n)
        if fl.flow not in r[0]:
            raise OecdMacroError(f"{fl.flow}: row of another structure", row=n, structure=r[0][:120])
        rep.structure = r[0]
        for d, lab in fl.labels.items():
            if get(r, d) not in lab:
                raise OecdMacroError(f"{fl.flow}: code not in the quoted codelist", dimension=d, code=get(r, d)[:40])
        status = get(r, "OBS_STATUS")
        raw_value = get(r, "OBS_VALUE").strip()
        if raw_value in fc.NO_VALUE:
            rep.no_value[status or "(none)"] += 1
            continue
        value = fc.strict_decimal(raw_value)
        freq = get(r, "FREQ")
        if freq not in fl.freqs:
            raise OecdMacroError(f"{fl.flow}: unexpected frequency", freq=freq[:4])
        start, end = fc.sdmx_period(freq, get(r, "TIME_PERIOD"))
        geo = get(r, "REF_AREA")
        parts = [get(r, fl.fold[0])] + [f"{d.lower()}={get(r, d)}" for d in fl.fold[1:] if get(r, d) not in ("", "_Z")]
        indicator = "|".join(parts)
        unit = fl.labels[fl.unit_dim][get(r, fl.unit_dim)]
        flags: dict[str, Any] = {}
        mult = get(r, "UNIT_MULT")
        if mult not in ("", "0"):
            unit = f"{unit} x10^{mult}"
            flags["unit_mult"] = mult
            flags["unit_mult_label"] = UNIT_MULT.get(mult, "(not in the quoted SDMX CL_UNIT_MULT subset)")
        if status not in ("", "A"):
            flags["obs_status"] = status
            flags["obs_status_label"] = OBS_STATUS.get(status, "(not in the quoted SDMX CL_OBS_STATUS subset)")
            rep.statuses[status] += 1
            if status == "E":
                flags["estimate_kind"] = "estimate"
            elif status == "P":
                flags["estimate_kind"] = "provisional"
            elif status == "F":
                flags["estimate_kind"] = "projection"
        if get(r, "CURRENCY") not in ("", "_Z"):
            flags["currency"] = get(r, "CURRENCY")
        if get(r, "BASE_PER"):
            flags["base_period"] = get(r, "BASE_PER")
        if freq in ("M", "Q"):
            adj = get(r, "ADJUSTMENT")
            if adj:
                flags["seasonal_adjustment"] = adj
                flags["seasonal_adjustment_label"] = fl.labels["ADJUSTMENT"][adj]
            else:
                flags["seasonal_adjustment"] = "not_stated_by_provider"
            rep.sa[str(flags["seasonal_adjustment"])] += 1
            flags["frequency"] = freq
        iso3: str | None
        if fl.regional:
            level = get(r, "TERRITORIAL_LEVEL")
            ctry = get(r, "COUNTRY")
            flags["territorial_level"] = level
            if ctry:
                flags["country_iso3"] = ctry
            rep.levels[level] += 1
            iso3 = country_iso3(geo, "iso3") if level == "CTRY" else None
            if level in ("TL2", "TL3"):
                regions.setdefault(geo, (level, ctry))
        else:
            iso3 = country_iso3(geo, "iso3")
        if float(value) == 0:
            rep.zeros += 1
        rep.geos.add(geo)
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
        cols["selector"].append(fc.canon({"file": label, "row": n}))
    if not cols["indicator"]:
        raise OecdMacroError(f"{fl.flow}: no cell with a value")
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
        pl.lit(fl.dataset).alias("dataset"),
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
        raise OecdMacroError(f"{fl.flow}: {dup} duplicate cells (an unfolded dimension)")
    return df, rep, regions


def oecd_regions(
    seen: Mapping[str, tuple[str, str]],
    cl: Mapping[str, RegionInfo],
    *,
    table: str,
    described_by: str,
    raw_sha256: str,
    codelist_sha256: str,
) -> dict[str, rkg.Region]:
    """Region entities for the TL2/TL3 codes of one response (parents resolved later by :func:`resolve_parents`)."""
    out: dict[str, rkg.Region] = {}
    for code, (level, ctry) in seen.items():
        info = cl.get(code)
        out[code] = rkg.Region(
            ns="oecdreg",
            code=code,
            label_en=(info.name_en if info and info.name_en else code),
            level=level,
            country_iso3=country_iso3(ctry, "iso3") if ctry else None,
            parent=None,
            country_basis="COUNTRY attribute of the OECD data rows (ISO 3166-1 alpha-3)",
            tables={table},
            described_by=f"{described_by}; CL_REGIONAL family {info.family if info else 'n/a'}",
            raw_sha256=raw_sha256,
            selector={"codelist": "OECD:CL_REGIONAL", "code": code, "codelist_raw_sha256": codelist_sha256},
        )
    return out


def resolve_parents(regions: dict[str, rkg.Region], cl: Mapping[str, RegionInfo]) -> dict[str, int]:
    """Walk the ``CL_REGIONAL`` parent chain of each region up to the first code that is itself a loaded region
    (``FR101 -> FR10 -> FR1``: FR10 is an OECD "NOG" grouping, not a TL2/TL3 row, so ``FR101 part_of FR1`` with
    the chain in the basis). A chain that reaches the region's own country (ISO3 = its COUNTRY attribute) first
    adds no part_of (the ``in_country`` edge carries it); a chain that reaches neither is counted, never guessed."""
    info: Counter[str] = Counter()
    codes = {r.code for r in regions.values()}
    for r in regions.values():
        chain = [r.code]
        cur = cl.get(r.code)
        found = None
        while cur is not None and cur.parent and len(chain) < 8:
            p = cur.parent
            chain.append(p)
            if p in codes:
                found = p
                break
            if country_iso3(p, "iso3") is not None:
                break
            cur = cl.get(p)
        if found:
            r.parent = found
            r.parent_basis = "OECD CL_REGIONAL parent chain " + " -> ".join(chain)
            info["parent_region" if len(chain) == 2 else "parent_region_via_grouping"] += 1
        elif len(chain) > 1 and country_iso3(chain[-1], "iso3") == r.country_iso3 and r.country_iso3:
            info["parent_is_country"] += 1
        else:
            info["no_loaded_parent"] += 1
    return dict(info)


def coverage_note(fl: OFlow, rep: OReport) -> str:
    return (
        f"OECD {fl.agency}:{fl.flow} '{fl.title}' ({rep.structure}); request {fl.url()}; {rep.rows} rows read,"
        f" {rep.cells} cells, {len(rep.geos)} geographies (territorial levels {dict(rep.levels)}), periods"
        f" {list(rep.periods) if rep.periods else None}. Rows without a value are not cells: {dict(rep.no_value)}."
        f" Flagged statuses kept with SDMX labels: {dict(rep.statuses)}. Seasonal adjustment as stated:"
        f" {dict(rep.sa)}. Reported zeros kept ({rep.zeros}); unit multiplier never applied. {fl.notes}"
        f" Licence: {LICENCE}."
    )[:4000]


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=CONNECTOR_NAME,
        version=CONNECTOR_VERSION,
        source_key=SOURCE_KEY,
        budget_key=SOURCE_KEY,
        atlas_source=SOURCE,
        provider="OECD (Statistics and Data Directorate; Centre for Entrepreneurship, SMEs, Regions and Cities) via"
        " the OECD Data Explorer SDMX API",
        licence=LICENCE,
        allowed_hosts=(HOST,),
        datasets=tuple(f.dataset for f in FLOWS),
        dataset_ids=tuple(f"{f.agency}:{f.flow}" for f in FLOWS),
        cadence_seconds=30 * 86_400,
        planned_requests=len(FLOWS) + 1,
        max_response_bytes=MAX_BYTES,
        min_interval_seconds=2.0,
        parser_version=PARSER_VERSION,
        code_digest=fc.module_digest(rkg, sys.modules[__name__]),
        field_mappings={
            "geo_scheme": "iso3 (country rows only)",
            "tables": {f.flow: f"{SOURCE}.{f.dataset}" for f in FLOWS},
            "regional_tables": [f.dataset for f in FLOWS if f.regional],
        },
        notes="Four code-only SDMX-CSV requests + the CL_REGIONAL codelist per full run (~45 MB measured"
        " 2026-10-17); OECD regions also loaded into the KG (econ_regions_kg).",
    )


async def atlas_ingest(run: Any) -> dict[str, Any]:
    from reva.operations.budgets import BudgetExhausted

    fixture = run.origin == "test_fixture"
    out: dict[str, Any] = {"fetched": [], "errors": [], "tables": [], "reports": {}}
    wanted = [f for f in FLOWS if run.wants(f.dataset)]
    cl: dict[str, RegionInfo] | None = None
    cl_sha = ""
    if any(f.regional for f in wanted):
        try:
            raw = await run.get(
                cl_regional_url(fixture),
                key="codelist:CL_REGIONAL",
                media_types=STRUCT_MEDIA,
                headers={"Accept": "application/vnd.sdmx.structure+xml;version=2.1"},
                max_bytes=20_000_000,
            )
            cl_sha = run.archive(raw)
            out["fetched"].append({"item": "codelist:CL_REGIONAL", "bytes": len(raw.body), "sha256": cl_sha})
            cl = parse_cl_regional(raw.body, min_codes=1 if fixture else 100)
        except BudgetExhausted:
            raise
        except Exception as e:  # tables still load; region names/parents then reported as blocked
            out["errors"].append({"item": "codelist:CL_REGIONAL", "error": type(e).__name__, "message": str(e)[:300]})
    regions: list[dict[str, rkg.Region]] = []
    kg_raws: list[str] = [cl_sha] if cl_sha else []
    retrieved = None
    for fl in wanted:
        try:
            raw = await run.get(fl.url(fixture), key=f"data:{fl.flow}", media_types=MEDIA, max_bytes=MAX_BYTES)
            if not raw.body:
                raise SourceResponseError(f"{fl.flow}: empty response body", retryable=True)
        except BudgetExhausted:
            raise
        except Exception as e:
            out["errors"].append({"dataset": fl.dataset, "error": type(e).__name__, "message": str(e)[:300]})
            continue
        sha = run.archive(raw)
        out["fetched"].append({"item": f"data:{fl.flow}", "bytes": len(raw.body), "sha256": sha})
        try:
            frame, rep, seen = parse_csvfile(fl, raw.body, sha, f"{fl.flow}.csv")
            reg = run.register(
                dataset=fl.dataset,
                frame=frame,
                raw_sha256s=[sha],
                coverage_note=coverage_note(fl, rep),
                geo_scheme=None,
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
        if fl.regional and cl is not None and seen:
            regions.append(
                oecd_regions(
                    seen,
                    cl,
                    table=f"{SOURCE}.{fl.dataset}",
                    described_by=f"OECD {fl.flow} ({rep.structure})",
                    raw_sha256=sha,
                    codelist_sha256=cl_sha,
                )
            )
            kg_raws.append(sha)
            retrieved = raw.retrieved_at
    if regions and retrieved is not None:
        merged = rkg.merge(regions)
        out["kg_parents"] = resolve_parents(merged, cl or {})
        out["kg"] = rkg.load(
            run.engine,
            run.store,
            merged,
            source_key=SOURCE_KEY,
            raw_sha256s=kg_raws,
            retrieved_at=retrieved,
            origin=run.origin,
            licence=LICENCE,
            table_titles={f"{SOURCE}.{f.dataset}": f"OECD {f.flow} - {f.title}" for f in FLOWS},
            connector=CONNECTOR_NAME,
        )
    out["ok"] = not out["errors"]
    return out


__all__ = [
    "CONNECTOR_NAME",
    "FLOWS",
    "FLOWS_BY_DATASET",
    "OFlow",
    "atlas_ingest",
    "atlas_source",
    "oecd_regions",
    "parse_cl_regional",
    "parse_csvfile",
    "resolve_parents",
]
