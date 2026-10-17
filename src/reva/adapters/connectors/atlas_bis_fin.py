"""BIS finance statistics -> atlas tables (V6, agent V6FINDATA): central bank total assets, global liquidity,
locational banking (cross-border positions) summaries, general-government debt securities.

A-FISCAL's :mod:`atlas_bis` already loads credit to the non-financial sector (``bis.tc``), debt service ratios
(``bis.dsr``), credit-to-GDP gaps, property prices, monthly policy rates and EERs; PRED-HF's :mod:`atlas_hf_bis`
the daily policy rates and EERs. This module adds, with no overlap:

Source ``atlas.bis_fin_bulk`` (source key ``bis.bulk``, BIS Data Portal bulk "flat" files, read as a stream):

==============  =======================================================================================  ======
table           content                                                                                  rows
==============  =======================================================================================  ======
``bis.cbta``    WS_CBTA central bank total assets, BIS-spliced, monthly / quarterly / annual, domestic   ~140 k
                currency (break-adjusted and not), US dollars, % of GDP (53 areas, from 1914 for the US)
``bis.gli``     WS_GLI global liquidity indicators: credit to non-banks by currency of denomination      ~24 k
                (USD, EUR, JPY), bank claims, % of GDP / growth (quarterly)
==============  =======================================================================================  ======

Source ``atlas.bis_fin`` (source key ``bis.stats``, SDMX REST API ``?format=csv``: one row per observation, codes
only; the labels of every code used are quoted below and a code not quoted fails the table):

================  =====================================================================================  ======
table             content                                                                                rows
================  =====================================================================================  ======
``bis.lbs_rep``   WS_LBS_D_PUB cross-border claims and liabilities of banks located in each reporting   ~26 k
                  country (all instruments, currencies, sectors, counterparties), amounts outstanding
                  and FX/break-adjusted changes, US dollars, quarterly
``bis.lbs_cp``    WS_LBS_D_PUB cross-border claims on / liabilities to each counterparty country of     ~84 k
                  banks in all BIS reporting countries, amounts outstanding, quarterly
``bis.dsec_gov``  WS_NA_SEC_DSS debt securities issued by the general government (S13) and the central   ~12 k
                  government (S1311), all markets, all maturities and currencies, market and nominal
                  value, US dollars, quarterly (sovereign debt securities outstanding)
================  =====================================================================================  ======

Rules: values verbatim (strict decimals); ``NaN`` / empty -> no row; observation status ``M`` (data cannot exist)
and ``H`` (holiday) -> no row; other statuses kept in ``flags.obs_status`` (with the quoted label). ``UNIT_MULT``
in the unit (``x10^6``) and flags, never applied. LBS ``OBS_STATUS = K`` ("Data included in another category") is
kept as a flagged observation (the BIS publishes the number). Geography: BIS ISO 3166 alpha-2 codes -> ``iso3``
(crosswalk ``iso2``); BIS aggregates (``5A`` all reporting countries, ``5J`` all countries, ``XM`` euro area …)
stay unmapped. Period precision = the provider frequency (``flags.precision``); as-of = fetch date (table versions
are the vintages); the BIS states no per-observation publication date (``flags.pub_lag = not_stated``).
Terms: BIS terms of permitted use (unrestricted use with citation), quoted and checked against the archived
``data.bis.org/help/legal`` page at every run.
"""

from __future__ import annotations

import csv
import io
import sys
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from reva.adapters.connectors import findata_common as fd
from reva.adapters.connectors import hf_daily_common as hc
from reva.atlas.sources import AtlasSourceSpec

API_NAME = "atlas.bis_fin"
BULK_NAME = "atlas.bis_fin_bulk"
VERSION = "1.0.0"
PARSER_VERSION = "atlas_bis_fin/1"
ATLAS_SOURCE = "bis"
GEO_SCHEME = "iso2"
API_HOST = "stats.bis.org"
BULK_HOST = "data.bis.org"
API = f"https://{API_HOST}/api/v1/data"
BULK = f"https://{BULK_HOST}/static/bulk"
LEGAL_URL = f"https://{BULK_HOST}/help/legal"
API_DATASETS = ("lbs_rep", "lbs_cp", "dsec_gov")
BULK_DATASETS = ("cbta", "gli")
CSV_MEDIA = ("text/csv",)
ZIP_MEDIA = ("application/zip", "application/x-zip-compressed", "application/octet-stream")
HTML = ("text/html",)

LICENCE = (
    "BIS terms of permitted use of BIS statistics: use unrestricted provided the BIS is cited as the source,"
    " translations marked unofficial, no misleading use, no extra charge in commercial products"
)
_TERMS = (
    "The use of the statistics is unrestricted, provided that: if the statistics are reproduced, the BIS must be cited "
    "in your publication or product as the source of the statistics;"
)
_BASE: dict[str, Any] = {
    "licence": LICENCE,
    "licence_url": LEGAL_URL + "#terms-of-permitted-use-of-bis-statistics",
    "terms_quote": _TERMS,
    "attribution": "Source: Bank for International Settlements (BIS) statistics",
    "licence_checked": "2026-10-17",
    "provider": "Bank for International Settlements",
    "publication": "BIS statistical release (quarterly for LBS / debt securities / GLI; CBTA updated with national "
    "sources); per-observation publication date not stated",
    "pub_lag": "not_stated",
    "revision_policy": "not stated per observation; BIS series are revised at each release (break-adjusted series "
    "recomputed); value as fetched (as_of), earlier vintages kept as table versions",
    "policy_quotes": {
        "bis_terms": ("legal", _TERMS),
        "bis_no_other_use": ("legal", "No other use is permissible."),
        "bis_national_sources": ("legal", "Most of the data sets rely on national statistics as communicated to the "
                                 "BIS by central banks and other national authorities and are compiled in cooperation "
                                 "with them."),
    },
}  # fmt: skip
RIGHTS: dict[str, dict[str, Any]] = {ds: dict(_BASE) for ds in (*API_DATASETS, *BULK_DATASETS)}

#: BIS code labels (quoted from the codelists of WS_LBS_D_PUB / WS_NA_SEC_DSS, retrieved 2026-10-17)
LABELS: dict[str, dict[str, str]] = {
    "L_MEASURE": {"S": "Amounts outstanding / Stocks", "F": "FX and break adjusted change (BIS calculated)"},
    "L_POSITION": {"C": "Total claims", "L": "Total liabilities"},
    "REF_SECTOR": {"S13": "General government", "S1311": "Central government excluding social security"},
    "VALUATION": {"M": "Market value", "N": "Nominal value", "F": "Face value"},
    "UNIT_MEASURE": {"USD": "US dollar"},
    "UNIT_MULT": {"6": "Millions", "9": "Billions", "0": "Units"},
    "COLLECTION": {
        "A": "Average of observations through period",
        "B": "Beginning of period",
        "E": "End of period",
        "H": "Highest in period",
        "M": "Middle of period",
        "S": "Summed through period",
        "U": "Unknown",
    },
}
OBS_STATUS = {
    "A": "Normal value", "B": "Break", "D": "Definition differs", "E": "Estimated value", "F": "Forecast value",
    "I": "Imputed value (CCSA definition)", "K": "Data included in another category", "N": "Non significant",
    "P": "Provisional value", "R": "Revised value", "S": "Strike", "U": "Low reliability",
}  # fmt: skip
NO_ROW_STATUS = {"M", "H", "L", "Q"}
NO_VALUE = {"", "NaN", "nan", "NA"}


@dataclass(frozen=True)
class BisFlow:
    dataset: str
    flow: str
    file: str  # FileSpec key
    dims: tuple[str, ...]  # dimension codes (SDMX key order), FREQ first
    area_dim: str
    fold: tuple[str, ...]  # dimensions folded into the indicator (besides FREQ when several frequencies)
    prefix: str
    freqs: tuple[str, ...]
    text_attrs: tuple[str, ...] = ()  # verbatim series texts kept in flags
    key: str = ""  # API key (API flows)
    member: str = ""  # zip member (bulk flows)
    quoted: tuple[str, ...] = ()  # dimensions whose codes must be in LABELS (API flows: no labels in the file)


FLOWS: tuple[BisFlow, ...] = (
    BisFlow("lbs_rep", "WS_LBS_D_PUB", "lbs_rep_csv",
            ("FREQ", "L_MEASURE", "L_POSITION", "L_INSTR", "L_DENOM", "L_CURR_TYPE", "L_PARENT_CTY", "L_REP_BANK_TYPE",
             "L_REP_CTY", "L_CP_SECTOR", "L_CP_COUNTRY", "L_POS_TYPE"),
            "L_REP_CTY", ("L_MEASURE", "L_POSITION"), "LBS_REP", ("Q",),
            key="Q.S+F.C+L.A.TO1.A.5J.A..A.5J.N", quoted=("L_MEASURE", "L_POSITION")),
    BisFlow("lbs_cp", "WS_LBS_D_PUB", "lbs_cp_csv",
            ("FREQ", "L_MEASURE", "L_POSITION", "L_INSTR", "L_DENOM", "L_CURR_TYPE", "L_PARENT_CTY", "L_REP_BANK_TYPE",
             "L_REP_CTY", "L_CP_SECTOR", "L_CP_COUNTRY", "L_POS_TYPE"),
            "L_CP_COUNTRY", ("L_MEASURE", "L_POSITION"), "LBS_CP", ("Q",),
            key="Q.S.C+L.A.TO1.A.5J.A.5A.A..N", quoted=("L_MEASURE", "L_POSITION")),
    BisFlow("dsec_gov", "WS_NA_SEC_DSS", "dsec_csv",
            ("FREQ", "ADJUSTMENT", "REF_AREA", "COUNTERPART_AREA", "REF_SECTOR", "COUNTERPART_SECTOR", "CONSOLIDATION",
             "ACCOUNTING_ENTRY", "STO", "INSTR_ASSET", "MATURITY", "EXPENDITURE", "UNIT_MEASURE", "CURRENCY_DENOM",
             "VALUATION", "PRICES", "TRANSFORMATION", "CUST_BREAKDOWN"),
            "REF_AREA", ("REF_SECTOR", "VALUATION"), "DSEC", ("Q",), text_attrs=("TITLE", "CUST_BREAKDOWN_LB"),
            key="Q.N..XW.S13+S1311.S1.N.L.LE.F3.T._Z.USD._T..V.N._T", quoted=("REF_SECTOR", "VALUATION")),
    BisFlow("cbta", "WS_CBTA", "cbta_zip",
            ("FREQ", "REF_AREA", "COMP_METHOD", "UNIT_MEASURE", "CURRENCY", "TRANSFORMATION"),
            "REF_AREA", ("UNIT_MEASURE", "CURRENCY", "TRANSFORMATION", "COMP_METHOD"), "CBTA", ("M", "Q", "A"),
            text_attrs=("TITLE", "DATA_COMP"), member="WS_CBTA_csv_flat.csv"),
    BisFlow("gli", "WS_GLI", "gli_zip",
            ("FREQ", "CURR_DENOM", "BORROWERS_CTY", "BORROWERS_SECTOR", "LENDERS_SECTOR", "L_POS_TYPE", "L_INSTR",
             "UNIT_MEASURE"),
            "BORROWERS_CTY", ("CURR_DENOM", "BORROWERS_SECTOR", "LENDERS_SECTOR", "L_POS_TYPE", "L_INSTR",
                              "UNIT_MEASURE"), "GLI", ("Q",), text_attrs=("TITLE",), member="WS_GLI_csv_flat.csv"),
)  # fmt: skip
FLOWS_BY_DATASET = {f.dataset: f for f in FLOWS}

API_FILES: tuple[hc.FileSpec, ...] = (
    *(hc.FileSpec(f.file, f"{API}/{f.flow}/{f.key}?format=csv", (f.dataset,), CSV_MEDIA, 60_000_000)
      for f in FLOWS if f.key),
    hc.FileSpec("legal", LEGAL_URL, API_DATASETS, HTML, 3_000_000, role="policy"),
)  # fmt: skip
BULK_FILES: tuple[hc.FileSpec, ...] = (
    hc.FileSpec("cbta_zip", f"{BULK}/WS_CBTA_csv_flat.zip", ("cbta",), ZIP_MEDIA, 30_000_000),
    hc.FileSpec("gli_zip", f"{BULK}/WS_GLI_csv_flat.zip", ("gli",), ZIP_MEDIA, 10_000_000),
    hc.FileSpec("legal", LEGAL_URL, BULK_DATASETS, HTML, 3_000_000, role="policy"),
)


def code_digest() -> str:
    return hc.code_digest(fd, sys.modules[__name__])


def atlas_source() -> list[AtlasSourceSpec]:
    common: dict[str, Any] = {
        "version": VERSION,
        "atlas_source": ATLAS_SOURCE,
        "licence": LICENCE,
        "cadence_seconds": 30 * hc.DAY,
        "min_interval_seconds": 2.0,
        "parser_version": PARSER_VERSION,
        "code_digest": code_digest(),
    }
    return [
        AtlasSourceSpec(
            name=API_NAME,
            source_key="bis.stats",
            provider="Bank for International Settlements (BIS statistics, SDMX API, compact CSV)",
            allowed_hosts=(API_HOST, BULK_HOST),
            datasets=API_DATASETS,
            dataset_ids=("BIS:WS_LBS_D_PUB(reporting countries)", "BIS:WS_LBS_D_PUB(counterparty countries)",
                         "BIS:WS_NA_SEC_DSS(general government)"),
            planned_requests=len(API_FILES),
            max_response_bytes=60_000_000,
            field_mappings={"geo_scheme": GEO_SCHEME, "tables": [f"bis.{d}" for d in API_DATASETS]},
            notes="3 compact-CSV data requests (~11 MB) + the BIS terms page per full run",
            **common,
        ),
        AtlasSourceSpec(
            name=BULK_NAME,
            source_key="bis.bulk",
            provider="Bank for International Settlements (BIS Data Portal bulk downloads)",
            allowed_hosts=(BULK_HOST,),
            datasets=BULK_DATASETS,
            dataset_ids=("BIS:WS_CBTA", "BIS:WS_GLI"),
            planned_requests=len(BULK_FILES),
            max_response_bytes=30_000_000,
            field_mappings={"geo_scheme": GEO_SCHEME, "tables": [f"bis.{d}" for d in BULK_DATASETS]},
            notes="2 flat bulk zips (~2.7 MB) + the BIS terms page per full run",
            **common,
        ),
    ]  # fmt: skip


def _unit(
    fl: BisFlow, attrs: Mapping[str, str], dims: Mapping[str, str], unit_label: str = ""
) -> tuple[str, dict[str, Any]]:
    """Unit text (label of UNIT_MEASURE, multiplier appended) + flags; the value is never rescaled."""
    um = dims.get("UNIT_MEASURE") or attrs.get("UNIT_MEASURE", "")
    mult = fd.code(attrs.get("UNIT_MULT", ""))
    if not um:
        raise hc.err(f"{fl.dataset}: series without UNIT_MEASURE", kind="unit")
    if fl.key:  # API compact CSV: codes only -> quoted labels, fail closed
        if um not in LABELS["UNIT_MEASURE"] or (mult and mult not in LABELS["UNIT_MULT"]):
            raise hc.err(f"{fl.dataset}: unit {um!r} / multiplier {mult!r} not quoted", kind="unit")
        ulab, mlab = LABELS["UNIT_MEASURE"][um], LABELS["UNIT_MULT"].get(mult, "")
    else:  # bulk flat file: "<code>: <label>" cells
        ulab, mlab = unit_label or fd.label(um) or fd.code(um), fd.label(attrs.get("UNIT_MULT", ""))
    unit = f"{ulab} ({fd.code(um)})" if len(ulab) <= 60 else fd.code(um)
    extra: dict[str, Any] = {}
    if mult and mult != "0":
        unit += f" x10^{mult}"
        extra["unit_mult"] = f"{mult}: {mlab}" if mlab else mult
    return unit[:100], extra


def _row(
    fl: BisFlow,
    rows: fd.PeriodRows,
    f: hc.Fetched,
    line: int,
    dims: Mapping[str, str],
    attrs: Mapping[str, str],
    obs: Mapping[str, str],
    rep: Counter[str],
    series: set[str],
    dim_labels: Mapping[str, str] | None = None,
) -> None:
    status = fd.code(obs.get("OBS_STATUS", "")) or "A"
    raw = obs.get("OBS_VALUE", "").strip()
    if status in NO_ROW_STATUS:
        rep[f"no_row_status_{status}"] += 1
        return
    if raw in NO_VALUE:
        rep["no_value"] += 1
        return
    v, normalised = hc.number(raw)
    if v is None:
        rep["not_a_number"] += 1
        return
    freq = dims["FREQ"]
    if freq not in fl.freqs:
        raise hc.err(f"{fl.dataset}: unexpected frequency {freq!r}")
    for d in fl.quoted:
        if dims[d] not in LABELS[d]:
            raise hc.err(f"{fl.dataset}: {d} code {dims[d]!r} not in the quoted BIS codelist", kind="schema_drift")
    start, end = fd.period_of(freq, obs["TIME_PERIOD"])
    parts = [fl.prefix] + ([f"freq={freq}"] if len(fl.freqs) > 1 else [])
    parts += [f"{d.lower()}={dims[d]}" for d in fl.fold if dims[d] not in ("", "_Z")]
    dl = dim_labels or {}
    unit, extra = _unit(fl, attrs, dims, dl.get("UNIT_MEASURE", ""))
    extra.update({"frequency": freq, "precision": fd.PRECISION[freq], "series_key": ".".join(dims[d] for d in fl.dims)})
    for d in fl.quoted:
        extra[f"{d.lower()}_label"] = LABELS[d][dims[d]]
    for d in fl.fold:
        if dl.get(d) and d not in fl.quoted:
            extra[f"{d.lower()}_label"] = dl[d]
    for a in fl.text_attrs:
        t = attrs.get(a, "").strip()
        if t:
            extra[a.lower()] = t[:1000]
    coll = attrs.get("COLLECTION", "")
    if coll:
        c = fd.code(coll)
        extra["collection"] = coll if ":" in coll else f"{c}: {LABELS['COLLECTION'].get(c, '(label not quoted)')}"
    if status != "A":
        extra["obs_status"] = f"{status}: {OBS_STATUS.get(status, '(label not quoted)')}"
        rep[f"flagged_status_{status}"] += 1
    conf = fd.code(obs.get("OBS_CONF", ""))
    if conf and conf != "F":
        extra["obs_conf"] = obs["OBS_CONF"]
    pre = obs.get("OBS_PRE_BREAK", "").strip()
    if pre and pre not in NO_VALUE:
        pv, _ = hc.number(pre)
        if pv is not None:
            extra["pre_break_value"] = pv
    if normalised:
        extra["value_normalised"] = True
    rows.add_period(indicator="|".join(parts), provider_geo=dims[fl.area_dim], start=start, end=end, value=v,
                    unit=unit, flags=rows.flags(**hc.base_flags(RIGHTS[fl.dataset], f, **extra)), raw_sha256=f.sha256,
                    selector={"file": fl.member or f.key, "record": line})  # fmt: skip
    series.add(extra["series_key"])
    rep["rows"] += 1


def parse_api(fl: BisFlow, f: hc.Fetched, rows: fd.PeriodRows) -> dict[str, Any]:
    """BIS SDMX REST ``?format=csv``: header of component ids, one row per observation (codes only)."""
    text = f.text("utf-8-sig")
    if text.lstrip()[:1] == "<":
        raise hc.err(f"{fl.dataset}: HTML/XML body where CSV was expected")
    reader = csv.DictReader(io.StringIO(text))
    need = {*fl.dims, "TIME_PERIOD", "OBS_VALUE"}
    if not reader.fieldnames or not need <= set(reader.fieldnames):
        raise hc.err(f"{fl.dataset}: CSV columns missing {sorted(need - set(reader.fieldnames or ()))}")
    rep: Counter[str] = Counter()
    series: set[str] = set()
    obs_cols = ("TIME_PERIOD", "OBS_VALUE", "OBS_STATUS", "OBS_CONF", "OBS_PRE_BREAK")
    for line, r in enumerate(reader, start=2):
        dims = {d: (r.get(d) or "").strip() for d in fl.dims}
        attrs = {k: (v or "").strip() for k, v in r.items() if k not in fl.dims and k not in obs_cols and k}
        obs = {c: (r.get(c) or "").strip() for c in obs_cols}
        _row(fl, rows, f, line, dims, attrs, obs, rep, series)
    return {"series": len(series), **fd.counter_report(rep)}


def parse_bulk(fl: BisFlow, f: hc.Fetched, rows: fd.PeriodRows) -> dict[str, Any]:
    rep: Counter[str] = Counter()
    series: set[str] = set()
    for line, d, labels, attrs, obs in fd.stream_bis_flat(f, fl.member, fl.dims):
        _row(
            fl,
            rows,
            f,
            line,
            dict(zip(fl.dims, d, strict=True)),
            attrs,
            obs,
            rep,
            series,
            dict(zip(fl.dims, labels, strict=True)),
        )
    return {"series": len(series), **fd.counter_report(rep)}


def build(files: Mapping[str, hc.Fetched], wanted: tuple[str, ...]) -> tuple[dict[str, hc.TableBuild], dict[str, Any]]:
    out: dict[str, hc.TableBuild] = {}
    report: dict[str, Any] = {}
    for ds in wanted:
        fl = FLOWS_BY_DATASET[ds]
        rows = fd.PeriodRows(ATLAS_SOURCE, ds)
        report[ds] = (parse_api if fl.key else parse_bulk)(fl, files[fl.file], rows)
        out[ds] = hc.TableBuild(rows)
    specs = API_FILES if set(wanted) <= set(API_DATASETS) else BULK_FILES
    report["policy_quotes_found"] = hc.check_policy_pages(files, specs, RIGHTS, wanted)
    return out, report


_LBS = (
    "Cross-border positions (L_POS_TYPE N) of banks, all instruments (A), all currencies (TO1/A), all counterparty "
    "sectors (A), all reporting bank types (A), all parent countries (5J); l_measure S = amounts outstanding, F = FX "
    "and break adjusted change (BIS calculated); l_position C = total claims, L = total liabilities; US dollars "
    "x10^6 (millions, not applied). Status K 'Data included in another category' and B 'Break' kept as flags."
)
NOTES = {
    "lbs_rep": "BIS locational banking statistics by reporting country (provider_geo = L_REP_CTY, country where the "
    "banks are located), counterparty = all countries (5J). " + _LBS,
    "lbs_cp": "BIS locational banking statistics by counterparty country (provider_geo = L_CP_COUNTRY, residence of "
    "the counterparty), reporting = all BIS reporting countries (5A): cross-border bank claims on / liabilities to "
    "residents of each country. " + _LBS,
    "dsec_gov": "BIS debt securities statistics (WS_NA_SEC_DSS): debt securities (F3) issued (liabilities, closing "
    "positions LE) by the general government (S13) and central government excluding social security (S1311) of each "
    "country, all markets (counterpart XW), all original maturities, all currencies, current prices, US dollars "
    "x10^9 (billions, not applied); valuation M market value / N nominal value / F face value.",
    "cbta": "BIS central bank total assets (WS_CBTA), BIS-spliced long series: freq M/Q/A are distinct indicators; "
    "unit_measure XDC domestic currency, USD US dollars, XDF_R_B1GQ % of GDP; transformation B adjusted for breaks / "
    "N non transformed; the national source of each series is in flags.data_comp (e.g. US: H.4.1 since 1989).",
    "gli": "BIS global liquidity indicators (WS_GLI): credit to non-bank borrowers by currency of denomination (USD, "
    "EUR, JPY; TO1 all currencies), lender sector, position type, instrument, unit (770 % of GDP, 771 annual growth, "
    "or the currency amount); series titles verbatim in flags.title. Quarterly.",
}


async def atlas_ingest(run: Any) -> dict[str, Any]:
    files: tuple[hc.FileSpec, ...]
    datasets: tuple[str, ...]
    if run.spec.name == BULK_NAME:
        files, datasets, key = BULK_FILES, BULK_DATASETS, "bis.bulk"
    else:
        files, datasets, key = API_FILES, API_DATASETS, "bis.stats"
    return await fd.ingest_files(run, files, datasets, build, rights=RIGHTS, notes=NOTES, source_key=key,
                                 geo_scheme=GEO_SCHEME)  # fmt: skip


__all__ = ["API_FILES", "BULK_FILES", "FLOWS", "RIGHTS", "atlas_ingest", "atlas_source", "build"]
