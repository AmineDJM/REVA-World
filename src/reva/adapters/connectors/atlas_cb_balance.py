"""Central bank balance sheets and sovereign yields -> atlas tables (V6, agent V6FINDATA).

Three sources, one module (dispatch on ``run.spec.name``):

``atlas.ecb_fin`` (source key ``ecb.stats``, ECB Data Portal SDMX-CSV ``format=csvdata``; licence: ECB website
copyright, free use with citation):

==============  ======================================================================================  ======
table           content                                                                                 rows
==============  ======================================================================================  ======
``ecb.wfs_w``   ILM: consolidated weekly financial statement of the Eurosystem, every published asset /   ~48 k
                liability item (43 series), millions of euro; period = the ISO week of the statement
``ecb.lty_m``   IRS: long-term interest rates for convergence purposes (10-year government bond yields,   ~12 k
                secondary market), monthly, every EU member state (+ euro area aggregates)
``ecb.yc_d``    YC: euro area government yield curve (Svensson spot rates, 2- and 10-year), AAA-rated      ~23 k
                issuers and all issuers, every TARGET business day
==============  ======================================================================================  ======

``atlas.frb_h41`` (new source key ``frb.releases``; Federal Reserve Board H.4.1 release data file
``/releases/h41/data/FRB_h41_xml.zip``, SDMX-ML 1.0 compact; public domain, cite the Board):

``frb.h41_w`` — "Factors Affecting Reserve Balances" and the condition statement of the Federal Reserve Banks:
every **Wednesday level** (day precision) and **week average** (the 7 days ending that Wednesday) series of the
consolidated release (all Reserve Banks; maturity-distribution and eliminations variants included), millions of
US dollars, from 2002-12-18. Per-district balance sheets (Reserve Banks 1-12) and the derived change series are
not taken. ``OBS_STATUS = NA`` (value ``-999999``) is no row.

``atlas.boe_balance`` (source key ``boe.iadb``; Bank of England Database CSV export; UK Open Government Licence):

``boe.weekly_w`` — Bank of England Weekly Report series (positions at the close of Wednesday, from 2014) and
``boe.bs_q`` — consolidated balance sheet, quarterly, published with a five-quarter lag; sterling millions.

Rules: values verbatim (strict decimals); a missing / flagged-missing cell is no row (counted); ``UNIT_MULT`` kept,
never applied. ``flags.precision`` = day / week / month; ``flags.as_of`` = fetch date (table versions are the
vintages); publication rule and revision policy per table, with the providers' sentences quoted and re-checked
against archived pages at every run. ``provider_geo``: ``U2`` (euro area, unmapped), ECB/IRS ISO alpha-2 member
codes (-> iso3), ``US`` (the Federal Reserve System), ``GB`` (the Bank of England).
"""

from __future__ import annotations

import csv
import io
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from typing import Any

from reva.adapters.connectors import findata_common as fd
from reva.adapters.connectors import hf_daily_common as hc
from reva.atlas.sources import AtlasSourceSpec

VERSION = "1.0.0"
PARSER_VERSION = "atlas_cb_balance/1"
ECB_NAME, FRB_NAME, BOE_NAME = "atlas.ecb_fin", "atlas.frb_h41", "atlas.boe_balance"
ECB_DATASETS = ("wfs_w", "lty_m", "yc_d")
FRB_DATASETS = ("h41_w",)
BOE_DATASETS = ("weekly_w", "bs_q")
HTML = ("text/html",)
CSV_MEDIA = ("text/csv", "application/vnd.sdmx.data+csv")
ZIP_MEDIA = ("application/zip", "application/x-zip-compressed", "application/octet-stream")
BOE_CSV = ("application/csv", "text/csv", "application/octet-stream")

# --------------------------------------------------------------------------- ECB
ECB_API = "https://data-api.ecb.europa.eu/service/data"
ECB_WWW = "https://www.ecb.europa.eu"
#: every Eurosystem weekly financial statement item published in ILM (W.U2.C), retrieved 2026-10-17
WFS_ITEMS = (
    "A010000", "A020000", "A020100", "A020200", "A030000", "A040000", "A040100", "A040200", "A050000", "A050100",
    "A050200", "A050300", "A050400", "A050500", "A050600", "A060000", "A070000", "A070100", "A070200", "A080000",
    "A110000", "L010000", "L020000", "L020100", "L020200", "L020300", "L020400", "L020500", "L030000", "L040000",
    "L050000", "L050100", "L050200", "L060000", "L070000", "L080000", "L080100", "L080200", "L090000", "L120000",
    "L140000", "L150000", "T000000",
)  # fmt: skip
WFS_KEY = "W.U2.C." + "+".join(WFS_ITEMS) + ".."  # the ECB firewall refuses three consecutive dots
LTY_KEY = "M..L.L40.CI.0000..N.Z"
YC_KEY = "B.U2.EUR.4F.G_N_A+G_N_C.SV_C_YM.SR_2Y+SR_10Y"
ECB_UNITS = {"PC": "Percent", "PCPA": "Percent per annum", "EUR": "Euro"}
ECB_MULT = {"0": "Units", "6": "Millions"}
ECB_STATUS = {"A": "Normal value", "E": "Estimated value", "P": "Provisional value", "B": "Break"}
ECB_NO_ROW = {"M", "Q", "L", "H"}  # missing (data cannot exist / suppressed / not collected / holiday)
DISCLAIMER = f"{ECB_WWW}/services/using-our-site/disclaimer/html/index.en.html"
ECB_FILES: tuple[hc.FileSpec, ...] = (
    hc.FileSpec("wfs_csv", f"{ECB_API}/ILM/{WFS_KEY}?format=csvdata", ("wfs_w",), CSV_MEDIA, 40_000_000),
    hc.FileSpec("lty_csv", f"{ECB_API}/IRS/{LTY_KEY}?format=csvdata", ("lty_m",), CSV_MEDIA, 20_000_000),
    hc.FileSpec("yc_csv", f"{ECB_API}/YC/{YC_KEY}?format=csvdata", ("yc_d",), CSV_MEDIA, 40_000_000),
    hc.FileSpec("wfs_page", f"{ECB_WWW}/press/wfs/html/index.en.html", ("wfs_w",), HTML, 2_000_000, role="policy"),
    hc.FileSpec("lty_page", f"{ECB_WWW}/stats/financial_markets_and_interest_rates/long_term_interest_rates/html/"
                "index.en.html", ("lty_m",), HTML, 2_000_000, role="policy"),
    hc.FileSpec("yc_page", f"{ECB_WWW}/stats/financial_markets_and_interest_rates/euro_area_yield_curves/html/"
                "index.en.html", ("yc_d",), HTML, 2_000_000, role="policy"),
    hc.FileSpec("disclaimer", DISCLAIMER, ECB_DATASETS, HTML, 2_000_000, role="policy"),
)  # fmt: skip
ECB_LICENCE = (
    "ECB website copyright: free use provided the information appears accurately and the ECB is cited as the source"
    " (modifications stated explicitly)"
)
_ECB_COPY = (
    "disclaimer",
    "users of this website may make free use of the information obtained directly from it subject to the following "
    "conditions: When such information is distributed or reproduced, it must appear accurately and the ECB must be "
    "cited as the source.",
)
_ECB_BASE: dict[str, Any] = {
    "licence": ECB_LICENCE,
    "licence_url": DISCLAIMER,
    "terms_quote": _ECB_COPY[1],
    "attribution": "Source: European Central Bank (ECB)",
    "licence_checked": "2026-10-17",
    "provider": "European Central Bank",
}
_WFS_PUB = (
    "The weekly financial statements of the Eurosystem are published on a Tuesday, and they relate to the preceding "
    "Friday."
)
_WFS_QTR = (
    "The publication day for the first financial statement of each quarter will normally be a Wednesday (instead of "
    "Tuesday) in order to allow more time to complete the quarterly revaluation of assets and liabilities, which is "
    "reflected in these statements."
)
_YC_PUB = "It is updated every TARGET business day at noon (12:00 CET)."
_LTY_PROXY = (
    "Where no harmonised long-term government bond yields are available, proxies derived from private sector bond "
    "yields or interest rate indicators are presented, where available."
)

# --------------------------------------------------------------------------- Federal Reserve Board
FRB_HOST = "www.federalreserve.gov"
FRB_ZIP = f"https://{FRB_HOST}/releases/h41/data/FRB_h41_xml.zip"
FRB_MEMBER = "H41_data.xml"
FRB_FILES: tuple[hc.FileSpec, ...] = (
    hc.FileSpec("h41_zip", FRB_ZIP, FRB_DATASETS, ZIP_MEDIA, 40_000_000),
    hc.FileSpec("about", f"https://{FRB_HOST}/releases/h41/about.htm", FRB_DATASETS, HTML, 2_000_000, role="policy"),
    hc.FileSpec("disclaimer", f"https://{FRB_HOST}/disclaimer.htm", FRB_DATASETS, HTML, 2_000_000, role="policy"),
)  # fmt: skip
FRB_LICENCE = "Public domain (Board of Governors of the Federal Reserve System website information); cite the Board"
_FRB_PD = (
    "Unless otherwise indicated, information on Board's website is in the public domain and may be copied and "
    "distributed without permission. Please cite to the Board as the source of the information."
)
_FRB_PUB = "is typically published on Thursday afternoon around 4:30 p.m."
H41_TYPES = {"L": "Wednesday level", "A": "week average"}
H41_DISTRICTS = frozenset(str(i) for i in range(1, 13))

# --------------------------------------------------------------------------- Bank of England
BOE_HOST = "www.bankofengland.co.uk"
BOE_WEEKLY = ("RPWB55A", "RPWB56A", "RPWB59A", "RPWB67A", "RPWZ4TJ", "RPWZ4TK", "RPWZOQ4", "RPWZ4TL", "RPWZ4TM",
              "RPWZOI7", "RPWZ4TN")  # fmt: skip
BOE_QUARTERLY = ("RPQB56A", "RPQB58A", "RPQB9R8", "RPQBV79", "RPQB55A", "RPQB62A", "RPQZ6MO", "RPQZ6MS", "RPQB59A",
                 "RPQZ6MT", "RPQB75A", "RPQB68A", "RPQB67A", "RPQBL59", "RPQZ4TJ", "RPQZ4TK", "RPQZ4TL", "RPQZP2P",
                 "RPQZOQ4", "RPQZ4TM", "RPQZOM3", "RPQZOQ3", "RPQB72A", "RPQZ6MW", "RPQZ4TN", "RPQZ6MX")  # fmt: skip


def _iadb(codes: tuple[str, ...], start: str) -> str:
    return (f"https://{BOE_HOST}/boeapps/database/_iadb-fromshowcolumns.asp?csv.x=yes&Datefrom={start}&Dateto=now"
            f"&SeriesCodes={','.join(codes)}&CSVF=TT&UsingCodes=Y&VPD=Y&VFD=N")  # fmt: skip


BOE_FILES: tuple[hc.FileSpec, ...] = (
    hc.FileSpec("weekly_csv", _iadb(BOE_WEEKLY, "01/Jan/2014"), ("weekly_w",), BOE_CSV, 5_000_000),
    hc.FileSpec("bs_csv", _iadb(BOE_QUARTERLY, "01/Jan/2014"), ("bs_q",), BOE_CSV, 5_000_000),
    hc.FileSpec("legal", f"https://{BOE_HOST}/legal", BOE_DATASETS, HTML, 2_000_000, role="policy"),
    hc.FileSpec("bs_page", f"https://{BOE_HOST}/weekly-report/balance-sheet-and-weekly-report", BOE_DATASETS, HTML,
                2_000_000, role="policy"),
)  # fmt: skip
BOE_LICENCE = "UK Open Government Licence v3.0 (Bank of England Database)"
_BOE_OGL = (
    "Reproduction of data in the Database is subject to the terms of the UK Open Government Licence, allowing and "
    "encouraging free and flexible data reuse."
)
_BOE_WEEKLY_PUB = "This is published every Thursday and shows positions as at the close of Wednesday."
_BOE_BS_PUB = "This is published every quarter and includes data for five quarters earlier."
_BOE_BASE: dict[str, Any] = {
    "licence": BOE_LICENCE,
    "licence_url": "https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/",
    "terms_quote": _BOE_OGL,
    "attribution": "Source: Bank of England",
    "licence_checked": "2026-10-17",
    "provider": "Bank of England",
}

RIGHTS: dict[str, dict[str, Any]] = {
    "wfs_w": {**_ECB_BASE, "publication": "Tuesday after the reference Friday (first statement of a quarter: "
              "normally Wednesday)", "pub_lag": "weekly_release",
              "revision_policy": "not stated; quarter-end revaluation reflected in the first statement of each "
              "quarter; value as fetched (as_of)",
              "policy_quotes": {"wfs_publication": ("wfs_page", _WFS_PUB), "wfs_quarter": ("wfs_page", _WFS_QTR),
                                "ecb_copyright": _ECB_COPY}},
    "lty_m": {**_ECB_BASE, "publication": "monthly (ECB long-term interest rate statistics release calendar)",
              "pub_lag": "not_stated", "revision_policy": "not stated; value as fetched (as_of)",
              "policy_quotes": {"lty_proxy": ("lty_page", _LTY_PROXY), "ecb_copyright": _ECB_COPY}},
    "yc_d": {**_ECB_BASE, "publication": "every TARGET business day at noon (12:00 CET)", "pub_lag": "same_day",
             "revision_policy": "not stated; value as fetched (as_of)",
             "data_licence_note": "ECB-estimated curve; bond and price information provided by MTS Markets, ratings "
             "by Fitch Ratings (ECB yield-curve page)",
             "policy_quotes": {"yc_publication": ("yc_page", _YC_PUB),
                               "yc_mts": ("yc_page", "Bond and price information provided by MTS Markets."),
                               "ecb_copyright": _ECB_COPY}},
    "h41_w": {"licence": FRB_LICENCE, "licence_url": f"https://{FRB_HOST}/disclaimer.htm", "terms_quote": _FRB_PD,
              "attribution": "Source: Board of Governors of the Federal Reserve System, H.4.1",
              "licence_checked": "2026-10-17", "provider": "Board of Governors of the Federal Reserve System",
              "publication": "Thursday around 4:30 p.m. ET for the week ended Wednesday", "pub_lag": "next_business_day",
              "revision_policy": "not stated; value as fetched (as_of)",
              "policy_quotes": {"h41_publication": ("about", _FRB_PUB), "frb_public_domain": ("disclaimer", _FRB_PD)}},
    "weekly_w": {**_BOE_BASE, "publication": "every Thursday, positions at the close of Wednesday",
                 "pub_lag": "next_business_day", "revision_policy": "not stated; value as fetched (as_of)",
                 "policy_quotes": {"boe_weekly": ("bs_page", _BOE_WEEKLY_PUB), "boe_ogl": ("legal", _BOE_OGL)}},
    "bs_q": {**_BOE_BASE, "publication": "quarterly, five quarters after the reference quarter",
             "pub_lag": "five_quarters", "revision_policy": "not stated; value as fetched (as_of)",
             "policy_quotes": {"boe_bs": ("bs_page", _BOE_BS_PUB), "boe_ogl": ("legal", _BOE_OGL)}},
}  # fmt: skip


def code_digest() -> str:
    return hc.code_digest(fd, sys.modules[__name__])


def atlas_source() -> list[AtlasSourceSpec]:
    common: dict[str, Any] = {"version": VERSION, "parser_version": PARSER_VERSION, "code_digest": code_digest(),
                              "cadence_seconds": 7 * hc.DAY, "min_interval_seconds": 2.0}  # fmt: skip
    return [
        AtlasSourceSpec(
            name=ECB_NAME, source_key="ecb.stats", atlas_source="ecb",
            provider="European Central Bank (ECB Data Portal SDMX-CSV): weekly financial statement, long-term "
            "interest rates, euro area yield curve",
            licence=ECB_LICENCE, allowed_hosts=("data-api.ecb.europa.eu", "www.ecb.europa.eu"),
            datasets=ECB_DATASETS, dataset_ids=("ECB:ILM(weekly, Eurosystem)", "ECB:IRS", "ECB:YC(spot 2Y/10Y)"),
            planned_requests=len(ECB_FILES), max_response_bytes=40_000_000,
            field_mappings={"geo_scheme": "iso2", "tables": [f"ecb.{d}" for d in ECB_DATASETS]},
            notes="3 SDMX-CSV requests (~36 MB) + 4 policy pages per full run", **common,
        ),
        AtlasSourceSpec(
            name=FRB_NAME, source_key="frb.releases", atlas_source="frb",
            provider="Board of Governors of the Federal Reserve System (H.4.1 release data file)",
            licence=FRB_LICENCE, allowed_hosts=(FRB_HOST,), datasets=FRB_DATASETS, dataset_ids=("FRB:H41",),
            planned_requests=len(FRB_FILES), max_response_bytes=40_000_000,
            field_mappings={"geo_scheme": "iso2", "tables": ["frb.h41_w"]},
            notes="1 zip (~9 MB, 127 MB SDMX-ML inflated, streamed) + 2 policy pages per full run", **common,
        ),
        AtlasSourceSpec(
            name=BOE_NAME, source_key="boe.iadb", atlas_source="boe",
            provider="Bank of England Database (Weekly Report and consolidated balance sheet series)",
            licence=BOE_LICENCE, allowed_hosts=(BOE_HOST,), datasets=BOE_DATASETS,
            dataset_ids=("BoE:Weekly Report (RPW*)", "BoE:consolidated balance sheet (RPQ*)"),
            planned_requests=len(BOE_FILES), max_response_bytes=5_000_000,
            field_mappings={"geo_scheme": "iso2", "tables": [f"boe.{d}" for d in BOE_DATASETS]},
            notes="2 IADB CSV exports (~0.1 MB) + 2 policy pages per full run", **common,
        ),
    ]  # fmt: skip


# --------------------------------------------------------------------------- ECB parsing
def _ecb_csv(
    ds: str, f: hc.Fetched, rows: fd.PeriodRows, *, freq: str, prefix: str, fold: tuple[str, ...], extra_rule: str = ""
) -> dict[str, Any]:
    text = f.text("utf-8-sig")
    if text.lstrip()[:1] == "<":
        raise hc.err(f"{ds}: HTML body where SDMX-CSV was expected (ECB firewall page?)")
    reader = csv.DictReader(io.StringIO(text))
    need = {"KEY", "FREQ", "REF_AREA", "TIME_PERIOD", "OBS_VALUE", "OBS_STATUS", "TITLE", "UNIT", "UNIT_MULT", *fold}
    if not reader.fieldnames or not need <= set(reader.fieldnames):
        raise hc.err(f"{ds}: SDMX-CSV columns missing {sorted(need - set(reader.fieldnames or ()))}")
    rep: Counter[str] = Counter()
    series: set[str] = set()
    for line, r in enumerate(reader, start=2):
        if r["FREQ"] != freq:
            raise hc.err(f"{ds}: line {line}: frequency {r['FREQ']!r}, expected {freq!r}")
        status = (r.get("OBS_STATUS") or "A").strip() or "A"
        v, normalised = hc.number(r["OBS_VALUE"])
        if status in ECB_NO_ROW or v is None:
            rep[f"no_row_status_{status}" if status in ECB_NO_ROW else "no_value"] += 1
            continue
        unit, mult = r["UNIT"].strip(), r["UNIT_MULT"].strip()
        if unit not in ECB_UNITS or mult not in ECB_MULT:
            raise hc.err(f"{ds}: unit {unit!r} / multiplier {mult!r} not in the quoted ECB codelists", kind="unit")
        start, end = fd.period_of(freq, r["TIME_PERIOD"])
        extra: dict[str, Any] = {"series_key": r["KEY"].strip(), "series_title": r["TITLE"].strip()[:500],
                                 "frequency": freq, "precision": fd.PRECISION[freq],
                                 "provider_period": r["TIME_PERIOD"].strip()}  # fmt: skip
        if r.get("TITLE_COMPL", "").strip():
            extra["series_title_compl"] = r["TITLE_COMPL"].strip()[:1000]
        if mult != "0":
            extra["unit_mult"] = f"{mult}: {ECB_MULT[mult]}"
        if extra_rule:
            extra["period_rule"] = extra_rule
        if status != "A":
            extra["obs_status"] = f"{status}: {ECB_STATUS.get(status, '(label not quoted)')}"
            rep[f"flagged_status_{status}"] += 1
        conf = (r.get("OBS_CONF") or "").strip()
        if conf and conf != "F":
            extra["obs_conf"] = conf
        if normalised:
            extra["value_normalised"] = True
        if ds == "yc_d" and RIGHTS[ds].get("data_licence_note"):
            extra["data_licence_note"] = RIGHTS[ds]["data_licence_note"]
        ind = "|".join([prefix, *(f"{d.lower()}={r[d].strip()}" for d in fold)])
        u = f"{ECB_UNITS[unit]} ({unit})" + (f" x10^{mult}" if mult != "0" else "")
        rows.add_period(indicator=ind, provider_geo=r["REF_AREA"].strip(), start=start, end=end, value=v, unit=u,
                        flags=rows.flags(**hc.base_flags(RIGHTS[ds], f, **extra)), raw_sha256=f.sha256,
                        selector={"file": f.key, "line": line, "key": r["KEY"].strip()})  # fmt: skip
        series.add(r["KEY"].strip())
        rep["rows"] += 1
    return {"series": len(series), **fd.counter_report(rep)}


# --------------------------------------------------------------------------- H.4.1 parsing
def parse_h41(f: hc.Fetched, rows: fd.PeriodRows) -> dict[str, Any]:
    """Stream the SDMX-ML 1.0 compact H.4.1 file; keep Wednesday levels and week averages (no districts)."""
    z = fd.zip_member(f, FRB_MEMBER)
    rep: Counter[str] = Counter()
    rights = RIGHTS["h41_w"]
    cur: dict[str, str] | None = None
    keep = False
    desc = ""
    n_series = 0
    with z.open(FRB_MEMBER) as raw:
        for ev, el in ET.iterparse(raw, events=("start", "end")):  # noqa: S314 - zip member from the archived bytes
            tag = el.tag.rsplit("}", 1)[-1]
            if ev == "start" and tag == "Series":
                cur = dict(el.attrib)
                st, dist = cur.get("SERIESTYPE", ""), cur.get("DISTRIBUTION", "")
                keep = st in H41_TYPES and dist not in H41_DISTRICTS
                rep[f"series_type_{st}{'' if keep else '_skipped'}"] += 1
                desc = ""
                if keep and (cur.get("UNIT") != "Currency" or cur.get("UNIT_MULT") != "1000000"
                             or cur.get("CURRENCY") != "USD" or cur.get("FREQ") != "19"):  # fmt: skip
                    raise hc.err(f"h41_w: unexpected unit/frequency attributes {cur}", kind="unit")
            elif ev == "end" and tag == "AnnotationText" and keep and cur is not None and not desc:
                desc = (el.text or "").strip()  # the first annotation is the Short Description
            elif ev == "end" and tag == "Obs" and keep and cur is not None:
                st = cur["SERIESTYPE"]
                status = el.get("OBS_STATUS", "")
                tp = el.get("TIME_PERIOD", "")
                if status != "A":
                    rep[f"no_row_status_{status or 'none'}"] += 1
                    el.clear()
                    continue
                v, normalised = hc.number(el.get("OBS_VALUE"))
                if v is None:
                    rep["not_a_number"] += 1
                    el.clear()
                    continue
                try:
                    d = date.fromisoformat(tp)
                except ValueError as e:
                    raise hc.err(f"h41_w: bad TIME_PERIOD {tp!r}") from e
                if d.weekday() != 2:
                    rep["not_a_wednesday"] += 1
                start, end = (d, d + timedelta(days=1)) if st == "L" else (d - timedelta(days=6), d + timedelta(days=1))
                extra: dict[str, Any] = {
                    "series_description": desc[:1000],
                    "series_type": f"{st}: {H41_TYPES[st]}",
                    "distribution": cur.get("DISTRIBUTION", ""),
                    "category": cur.get("CATEGORY", ""),
                    "precision": "day" if st == "L" else "week",
                    "frequency": "W",
                    "unit_mult": "1000000 (millions; not applied)",
                }
                if st == "A":
                    extra["period_rule"] = "average of daily figures over the week ended Wednesday (7 days)"
                if desc.startswith("Discontinued"):
                    extra["discontinued_series"] = True
                if normalised:
                    extra["value_normalised"] = True
                rows.add_period(indicator=cur["SERIES_NAME"], provider_geo="US", start=start, end=end, value=v,
                                unit="US dollars (USD) x10^6", flags=rows.flags(**hc.base_flags(rights, f, **extra)),
                                raw_sha256=f.sha256,
                                selector={"file": FRB_MEMBER, "series": cur["SERIES_NAME"], "time_period": tp})  # fmt: skip
                rep["rows"] += 1
                el.clear()
            elif ev == "end" and tag == "Series":
                if keep:
                    n_series += 1
                cur, keep = None, False
                el.clear()
    return {"series": n_series, **fd.counter_report(rep)}


# --------------------------------------------------------------------------- BoE parsing
_BOE_UNIT = "(in sterling millions)"


def parse_boe(ds: str, f: hc.Fetched, rows: fd.PeriodRows, codes: tuple[str, ...]) -> dict[str, Any]:
    lines = f.text("utf-8-sig").splitlines()
    if not lines or lines[0].strip() != "SERIES,DESCRIPTION":
        raise hc.err(f"{ds}: export must start with the SERIES,DESCRIPTION block (CSVF=TT)")
    desc: dict[str, str] = {}
    i = 1
    while i < len(lines) and not lines[i].startswith("DATE,"):
        if lines[i].strip():
            rec = next(csv.reader([lines[i]]))
            desc[rec[0].strip()] = ",".join(rec[1:]).strip()
        i += 1
    if i >= len(lines):
        raise hc.err(f"{ds}: no DATE header")
    header = [h.strip() for h in next(csv.reader([lines[i]]))]
    if set(header[1:]) != set(codes) or set(desc) != set(codes):
        raise hc.err(f"{ds}: series block / header do not list the requested codes")
    for c in codes:
        if _BOE_UNIT not in desc[c]:
            raise hc.err(f"{ds}: series {c} does not state '(in sterling millions)': {desc[c][:120]}", kind="unit")
    rights = RIGHTS[ds]
    freq = "W" if ds == "weekly_w" else "Q"
    rep: Counter[str] = Counter()
    for n, rec in enumerate(csv.reader(lines[i + 1 :]), start=i + 2):
        if not rec or not rec[0].strip():
            continue
        try:
            d = datetime.strptime(rec[0].strip(), "%d %b %Y").date()
        except ValueError as e:
            raise hc.err(f"{ds}: line {n}: bad date {rec[0]!r}") from e
        for c, cell in zip(header[1:], rec[1:], strict=False):
            v, normalised = hc.number(cell)
            if v is None:
                rep["empty_cell"] += 1
                continue
            extra: dict[str, Any] = {
                "series_title": desc[c][:500],
                "frequency": freq,
                "precision": "day",
                "position_date_rule": "Wednesday close" if freq == "W" else "quarter-end date",
            }
            if normalised:
                extra["value_normalised"] = True
            rows.add_period(indicator=c, provider_geo="GB", start=d, end=d + timedelta(days=1), value=v,
                            unit="Pounds sterling (GBP) x10^6", flags=rows.flags(**hc.base_flags(rights, f, **extra)),
                            raw_sha256=f.sha256, selector={"file": f.key, "line": n, "column": c})  # fmt: skip
            rep["rows"] += 1
    return fd.counter_report(rep)


# --------------------------------------------------------------------------- build / ingest
def build(files: Mapping[str, hc.Fetched], wanted: tuple[str, ...]) -> tuple[dict[str, hc.TableBuild], dict[str, Any]]:
    out: dict[str, hc.TableBuild] = {}
    report: dict[str, Any] = {}
    for ds in wanted:
        src = "ecb" if ds in ECB_DATASETS else ("frb" if ds in FRB_DATASETS else "boe")
        rows = fd.PeriodRows(src, ds)
        if ds == "wfs_w":
            report[ds] = _ecb_csv(ds, files["wfs_csv"], rows, freq="W", prefix="WFS", fold=("BS_ITEM", "COUNT_AREA",
                                  "CURRENCY_TRANS"), extra_rule="ISO week of the statement; the ECB: statements relate "
                                  "to the Friday of that week")  # fmt: skip
        elif ds == "lty_m":
            report[ds] = _ecb_csv(ds, files["lty_csv"], rows, freq="M", prefix="LTIR", fold=("CURRENCY_TRANS",))
        elif ds == "yc_d":
            report[ds] = _ecb_csv(ds, files["yc_csv"], rows, freq="B", prefix="YC", fold=("INSTRUMENT_FM",
                                  "DATA_TYPE_FM"))  # fmt: skip
        elif ds == "h41_w":
            report[ds] = parse_h41(files["h41_zip"], rows)
        elif ds == "weekly_w":
            report[ds] = parse_boe(ds, files["weekly_csv"], rows, BOE_WEEKLY)
        else:
            report[ds] = parse_boe(ds, files["bs_csv"], rows, BOE_QUARTERLY)
        out[ds] = hc.TableBuild(rows)
    specs = ECB_FILES + FRB_FILES + BOE_FILES
    pages = {s.key: s for s in specs}
    report["policy_quotes_found"] = hc.check_policy_pages(
        files, [s for s in pages.values() if s.key in files], RIGHTS, wanted
    )
    return out, report


NOTES = {
    "wfs_w": "Consolidated weekly financial statement of the Eurosystem (ECB ILM, W.U2.C): 43 published asset (A*) "
    "and liability (L*) items and the total (T000000), millions of euro (x10^6, not applied). Period = the ISO week "
    "of the provider's weekly period (e.g. 2026-W41); the ECB states the statements relate to the Friday of that "
    "week. Indicator WFS|bs_item=<item>|count_area=<area>|currency_trans=<currency>; titles verbatim in flags.",
    "lty_m": "Long-term interest rates for convergence purposes (ECB IRS): 10-year government bond yields, monthly "
    "averages as published, percent; every EU member state (provider_geo ISO alpha-2 -> iso3) and euro-area "
    "aggregates (U2, I9, I10, D0, V5 unmapped). Indicator LTIR|currency_trans=<denomination> (BG has EUR and BGN "
    "series). The ECB presents proxies where no harmonised yield exists (quoted).",
    "yc_d": "Euro area government bond yield curve (ECB YC, Svensson model, continuous compounding, spot rates), "
    "2-year and 10-year maturities; G_N_A = AAA-rated central-government issuers, G_N_C = all euro area central "
    "governments; every TARGET business day since 2004-09-06, percent per annum. ECB estimate from MTS Markets "
    "bond prices and Fitch ratings (flags.data_licence_note).",
    "h41_w": "Federal Reserve H.4.1 (Factors Affecting Reserve Balances of Depository Institutions and Condition "
    "Statement of Federal Reserve Banks): every consolidated 'Wednesday level' series (day precision) and 'week "
    "average' series (7 days ended Wednesday), millions of US dollars (x10^6, not applied), weekly since "
    "2002-12-18; discontinued series kept with flags.discontinued_series. Indicator = the Board's series name "
    "(e.g. RESPPMA_N.WW total assets, Wednesday level); description verbatim in flags.series_description.",
    "weekly_w": "Bank of England Weekly Report (Bank of England Database RPW* series): notes in circulation, reserve "
    "balances, short-term and indexed long-term repo, TFSME, bond holdings, loan to the Asset Purchase Facility, "
    "foreign-currency reserve assets; positions at the close of Wednesday, sterling millions; from 2014.",
    "bs_q": "Bank of England consolidated balance sheet (RPQ* series), quarter-end positions, sterling millions, "
    "published quarterly five quarters after the reference quarter (as_of shows when REVA saw it).",
}


async def atlas_ingest(run: Any) -> dict[str, Any]:
    name = run.spec.name
    files: tuple[hc.FileSpec, ...]
    datasets: tuple[str, ...]
    if name == ECB_NAME:
        files, datasets, key = ECB_FILES, ECB_DATASETS, "ecb.stats"
    elif name == FRB_NAME:
        files, datasets, key = FRB_FILES, FRB_DATASETS, "frb.releases"
    else:
        files, datasets, key = BOE_FILES, BOE_DATASETS, "boe.iadb"
    return await fd.ingest_files(run, files, datasets, build, rights=RIGHTS, notes=NOTES, source_key=key,
                                 geo_scheme="iso2")  # fmt: skip


__all__ = ["BOE_FILES", "ECB_FILES", "FRB_FILES", "RIGHTS", "atlas_ingest", "atlas_source", "build"]
