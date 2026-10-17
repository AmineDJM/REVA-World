"""Bank of England daily rates (Bank Rate, SONIA) -> atlas table (V5, agent PRED-HF).

Source: the Bank of England Interactive Statistical Database CSV export (no key):
``https://www.bankofengland.co.uk/boeapps/database/_iadb-fromshowcolumns.asp?csv.x=yes&Datefrom=01/Jan/1975&Dateto=now
&SeriesCodes=<codes>&CSVF=TT&UsingCodes=Y&VPD=Y&VFD=N`` (``CSVF=TT``: a ``SERIES,DESCRIPTION`` block naming each
series, a blank line, then ``DATE,<code>…`` rows with dates such as ``02 Jan 1975``).

Table ``boe.rates_d``: indicator = the BoE series code (``IUDBEDR`` Official Bank Rate, ``IUDSOIA`` SONIA, its
10th/25th/75th/90th volume percentiles ``IUDZLS6``-``IUDZLS9``), description verbatim in ``flags.series_title``;
an empty cell is no row. ``provider_geo`` = ``GB`` (``iso3`` GBR through the ISO alpha-2 crosswalk). Unit: the CSV
states none; these are interest rates published in percent (``flags.unit_basis``). SONIA's total nominal value
(IUDZLT2) is not taken (its unit is not stated in the export).

Publication (quoted, checked against the archived SONIA page): "SONIA for the previous London business day is
published by authorised distributors at 9am. It is made freely available, via our Database by 10am on the business
day after it is first published." — the database copy REVA reads is therefore available two London business days
after the reference day (``flags.pub_lag``). The page also states the April 2018 reform of the benchmark (no day
given; ``flags.benchmark_note`` on SONIA rows). Licence: UK Open Government Licence v3.0 (BoE legal page; the
excluded third-party exchange-rate series are not taken here); SONIA rows carry the attribution statement the Bank
requires.
"""

from __future__ import annotations

import csv
import sys
from collections import Counter
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from reva.adapters.connectors import hf_daily_common as hc
from reva.atlas.sources import AtlasSourceSpec

NAME = "atlas.boe_daily"
VERSION = "1.0.0"
PARSER_VERSION = "atlas_hf_boe/1"
SOURCE_KEY = "boe.iadb"
ATLAS_SOURCE = "boe"
DATASETS = ("rates_d",)
HOST = "www.bankofengland.co.uk"
GEO = "GB"
GEO_SCHEME = "iso2"
CODES = ("IUDBEDR", "IUDSOIA", "IUDZLS6", "IUDZLS7", "IUDZLS8", "IUDZLS9")
SONIA = frozenset({"IUDSOIA", "IUDZLS6", "IUDZLS7", "IUDZLS8", "IUDZLS9"})
CSV_URL = (
    f"https://{HOST}/boeapps/database/_iadb-fromshowcolumns.asp?csv.x=yes&Datefrom=01/Jan/1975&Dateto=now"
    f"&SeriesCodes={','.join(CODES)}&CSVF=TT&UsingCodes=Y&VPD=Y&VFD=N"
)
LEGAL_URL = f"https://{HOST}/legal"
SONIA_URL = f"https://{HOST}/statistics/details/further-details-about-sonia-data"

FILES: tuple[hc.FileSpec, ...] = (
    hc.FileSpec("iadb_csv", CSV_URL, DATASETS, ("application/csv", "text/csv", "application/octet-stream"), 5_000_000),
    hc.FileSpec("legal", LEGAL_URL, DATASETS, ("text/html",), 2_000_000, role="policy"),
    hc.FileSpec("sonia_page", SONIA_URL, DATASETS, ("text/html",), 2_000_000, role="policy"),
)

LICENCE = "UK Open Government Licence v3.0 (Bank of England Database)"
_Q_OGL = (
    "Reproduction of data in the Database is subject to the terms of the UK Open Government Licence, allowing and "
    "encouraging free and flexible data reuse."
)
_Q_SONIA_ATTR = (
    "SONIA and/or SONIA Compounded Index data licensed under the Open Government Licence v3.0 and copyright the "
    "Governor and Company of the Bank of England."
)
_Q_PUB = (
    "SONIA for the previous London business day is published by authorised distributors at 9am. It is made freely "
    "available, via our Database by 10am on the business day after it is first published."
)
RIGHTS: dict[str, dict[str, Any]] = {
    "rates_d": {
        "licence": LICENCE,
        "licence_url": "https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/",
        "terms_quote": _Q_OGL,
        "attribution": "Source: Bank of England. " + _Q_SONIA_ATTR,
        "licence_checked": "2026-10-17",
        "provider": "Bank of England",
        "publication": "SONIA: distributors 9am next London business day; BoE Database by 10am the business day after",
        "pub_lag": "second_business_day",
        "revision_policy": "not stated on the archived pages; value as fetched (as_of)",
        "policy_quotes": {
            "ogl": ("legal", _Q_OGL),
            "sonia_attribution": ("legal", _Q_SONIA_ATTR),
            "fx_excluded": ("legal", "Please note that selected exchange rate data and series are excluded from this "
                            "licence as they are reproduced by the Bank under licence from third parties."),
            "sonia_publication": ("sonia_page", _Q_PUB),
            "sonia_reform": ("sonia_page", "we introduced a series of reforms of the SONIA benchmark in April 2018."),
        },
    }
}  # fmt: skip
BENCHMARK_NOTE = "Bank of England: reforms of the SONIA benchmark introduced in April 2018 (day not stated)"


def code_digest() -> str:
    return hc.code_digest(sys.modules[__name__])


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=NAME,
        version=VERSION,
        source_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="Bank of England (Interactive Statistical Database)",
        licence=LICENCE,
        allowed_hosts=(HOST,),
        datasets=DATASETS,
        dataset_ids=tuple(f"BOE:{c}" for c in CODES),
        cadence_seconds=hc.DAY,
        planned_requests=len(FILES),
        max_response_bytes=5_000_000,
        min_interval_seconds=2.0,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(),
        field_mappings={"geo_scheme": GEO_SCHEME, "tables": ["boe.rates_d"]},
        notes="one IADB CSV export (~0.6 MB) + legal and SONIA pages per full run",
    )


def parse_csv(f: hc.Fetched, rows: hc.DailyRows, report: dict[str, Any]) -> None:
    lines = f.text("utf-8-sig").replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != "SERIES,DESCRIPTION":
        raise hc.err("rates_d: export must start with the SERIES,DESCRIPTION block (CSVF=TT)")
    try:
        blank = next(i for i, ln in enumerate(lines) if not ln.strip())
    except StopIteration as e:
        raise hc.err("rates_d: no blank line after the series block") from e
    titles = {r[0].strip(): r[1].strip() for r in csv.reader(lines[1:blank]) if len(r) >= 2}
    if set(titles) != set(CODES):
        raise hc.err(f"rates_d: series block {sorted(titles)} != requested {sorted(CODES)}")
    reader = csv.reader(lines[blank + 1 :])
    header = next(reader, None)
    if not header or header[0] != "DATE" or set(header[1:]) != set(CODES):
        raise hc.err(f"rates_d: data header {header}")
    rights = RIGHTS["rates_d"]
    empty: Counter[str] = Counter()
    n: Counter[str] = Counter()
    for i, rec in enumerate(reader, start=blank + 3):
        if not rec or not rec[0].strip():
            continue
        try:
            d = datetime.strptime(rec[0].strip(), "%d %b %Y").date()
        except ValueError as e:
            raise hc.err(f"rates_d: line {i}: bad date {rec[0]!r}") from e
        for j, code in enumerate(header[1:], start=1):
            v, normalised = hc.number(rec[j] if j < len(rec) else "")
            if v is None:
                empty[code] += 1
                continue
            fl = rows.flags(**hc.base_flags(
                rights, f, series_title=titles[code], unit_basis="export states no unit; Bank of England rates are "
                "published in percent", benchmark_note=BENCHMARK_NOTE if code in SONIA else None,
                attribution=_Q_SONIA_ATTR if code in SONIA else None, value_normalised=normalised or None,
                pub_lag=rights["pub_lag"] if code in SONIA else "not_stated"))  # fmt: skip
            rows.add(indicator=code, provider_geo=GEO, day=d, value=v, unit="percent", flags=fl, raw_sha256=f.sha256,
                     selector={"file": "iadb_csv", "line": i, "column": code})  # fmt: skip
            n[code] += 1
    report["rates_d"] = {"rows": dict(n), "empty_cells": dict(empty), "titles": titles}


def build(
    files: Mapping[str, hc.Fetched], wanted: tuple[str, ...] = DATASETS
) -> tuple[dict[str, hc.TableBuild], dict[str, Any]]:
    out = {ds: hc.TableBuild(hc.DailyRows(ATLAS_SOURCE, ds)) for ds in wanted}
    report: dict[str, Any] = {}
    if "rates_d" in wanted:
        parse_csv(files["iadb_csv"], out["rates_d"].rows, report)
    report["policy_quotes_found"] = hc.check_policy_pages(files, FILES, RIGHTS, wanted)
    return out, report


NOTES = {
    "rates_d": "Bank of England daily Official Bank Rate (IUDBEDR, since 1975) and SONIA with its volume percentiles "
    "(IUDSOIA since 1997, IUDZLS6-9), percent, one row per London business day reported; empty cells are no row. "
    "SONIA reformed April 2018 (flags.benchmark_note). OGL v3.0. provider_geo GB. flags.as_of = fetch date.",
}


async def atlas_ingest(run: Any) -> dict[str, Any]:
    wanted = tuple(d for d in DATASETS if run.wants(d))
    files = await hc.fetch_all(run, FILES, wanted)
    builds, report = build(files, wanted)
    tables = hc.register_tables(run, builds, rights=RIGHTS, notes=NOTES, source_key=SOURCE_KEY, geo_scheme=GEO_SCHEME)
    return {"ok": True, "tables": tables, "report": report}


__all__ = ["CODES", "DATASETS", "FILES", "NAME", "RIGHTS", "atlas_ingest", "atlas_source", "build"]
