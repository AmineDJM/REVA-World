"""Fama/French research factors (Kenneth R. French Data Library) -> atlas tables (V6, agent V6FINDATA).

Source: ``https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/data_library.html`` (no key). Files (zipped CSV):

================  =======================================================================  ===================
table             file                                                                     content
================  =======================================================================  ===================
``kfrench.ff3``   F-F_Research_Data_Factors_CSV.zip                                         Mkt-RF, SMB, HML, RF,
                                                                                           monthly + annual
``kfrench.ff3_d`` F-F_Research_Data_Factors_daily_CSV.zip                                   same, daily
``kfrench.ff5``   F-F_Research_Data_5_Factors_2x3_CSV.zip                                   + RMW, CMA, monthly
                                                                                           + annual
``kfrench.mom``   F-F_Momentum_Factor_CSV.zip                                               Mom, monthly + annual
================  =======================================================================  ===================

Values: returns in percent per period, verbatim (``"  2.89"`` -> ``2.89``); the provider's missing markers
``-99.99`` / ``-999`` are no row. Monthly and annual sections are distinct indicators (``|freq=A``). The header text
(CRSP database vintage, T-bill source) is kept in ``flags.provider_note``; the CRSP vintage (``202608``) is in
``flags.provider_vintage`` — the library recomputes past factor values when CRSP data change, so table versions are
real vintages (Fama and French document such changes in the library).

**Licence: none granted.** The files end with "Copyright <year> Eugene F. Fama and Kenneth R. French"; no
reuse licence is stated, and the factors are computed from licensed CRSP data (and, for RF, Ibbotson Associates and
the ICE BofA US 1-Month Treasury Bill Index). Every row carries ``flags.licence_restriction =
"no_licence_granted"`` and ``flags.redistribution = "not_established"``; table rights carry ``open_licence: false``
(so the atlas marks the table restricted and names it next to any answer). Internal research use as a benchmark
(e.g. PRED-FIN factor baselines) only; publication or redistribution needs the owners' permission (owner decision
in ``docs/dev/v6_findata.md``).
"""

from __future__ import annotations

import io
import re
import sys
from collections import Counter
from collections.abc import Mapping
from datetime import date
from typing import Any

from reva.adapters.connectors import findata_common as fd
from reva.adapters.connectors import hf_daily_common as hc
from reva.atlas.sources import AtlasSourceSpec

NAME = "atlas.ff_factors"
VERSION = "1.0.1"  # 1.0.0 was registered in the dev DB from an earlier working-tree state
PARSER_VERSION = "atlas_ff_factors/1"
SOURCE_KEY = "kfrench.library"
ATLAS_SOURCE = "kfrench"
HOST = "mba.tuck.dartmouth.edu"
BASE = f"https://{HOST}/pages/faculty/ken.french"
DATASETS = ("ff3", "ff3_d", "ff5", "mom")
ZIP_MEDIA = ("application/zip", "application/x-zip-compressed", "application/octet-stream")
FILES_BY_DS = {
    "ff3": ("F-F_Research_Data_Factors_CSV.zip", "F-F_Research_Data_Factors.csv"),
    "ff3_d": ("F-F_Research_Data_Factors_daily_CSV.zip", "F-F_Research_Data_Factors_daily.csv"),
    "ff5": ("F-F_Research_Data_5_Factors_2x3_CSV.zip", "F-F_Research_Data_5_Factors_2x3.csv"),
    "mom": ("F-F_Momentum_Factor_CSV.zip", "F-F_Momentum_Factor.csv"),
}
COLUMNS = {
    "ff3": ("Mkt-RF", "SMB", "HML", "RF"),
    "ff3_d": ("Mkt-RF", "SMB", "HML", "RF"),
    "ff5": ("Mkt-RF", "SMB", "HML", "RMW", "CMA", "RF"),
    "mom": ("Mom",),
}
FILES: tuple[hc.FileSpec, ...] = (
    *(hc.FileSpec(ds, f"{BASE}/ftp/{z}", (ds,), ZIP_MEDIA, 5_000_000) for ds, (z, _m) in FILES_BY_DS.items()),
    hc.FileSpec("library", f"{BASE}/data_library.html", DATASETS, ("text/html",), 2_000_000, role="policy"),
)
MISSING = {"-99.99", "-999"}
LICENCE = (
    "No licence granted: (c) Eugene F. Fama and Kenneth R. French (Kenneth R. French Data Library); computed from "
    "licensed CRSP data; not an open licence"
)
_COPY = "Eugene F. Fama and Kenneth R. French"
_CRSP = "CRSP's Stock and Indexes Legacy Format (FIZ) files were discontinued after the December 2024 data release."
RIGHTS: dict[str, dict[str, Any]] = {
    ds: {
        "licence": LICENCE,
        "licence_url": f"{BASE}/data_library.html",
        "open_licence": False,
        "restriction": "no licence granted by the copyright holders; inputs from licensed CRSP data (and Ibbotson "
        "Associates / ICE BofA for RF): internal research use only, redistribution not established",
        "attribution": "Kenneth R. French Data Library (Eugene F. Fama and Kenneth R. French)",
        "licence_checked": "2026-10-17",
        "provider": "Kenneth R. French Data Library (Tuck School of Business, Dartmouth)",
        "publication": "library updated (typically monthly) with a new CRSP vintage; files carry the CRSP vintage",
        "pub_lag": "not_stated",
        "revision_policy": "history recomputed when CRSP data or the library's method change (stated in the library);"
        " value as fetched (as_of), earlier vintages kept as table versions",
        "policy_quotes": {"ff_copyright": ("library", _COPY), "ff_crsp": ("library", _CRSP)},
    }
    for ds in DATASETS
}
_ROW = re.compile(r"^\s*([0-9]{4}|[0-9]{6}|[0-9]{8})\s*,(.*)$")
_VINTAGE = re.compile(r"using the ([0-9]{6}) CRSP database")


def code_digest() -> str:
    return hc.code_digest(fd, sys.modules[__name__])


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=NAME,
        version=VERSION,
        source_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="Kenneth R. French Data Library (Fama/French research factors)",
        licence=LICENCE,
        allowed_hosts=(HOST,),
        datasets=DATASETS,
        dataset_ids=tuple(z for z, _m in FILES_BY_DS.values()),
        cadence_seconds=30 * hc.DAY,
        planned_requests=len(FILES),
        max_response_bytes=5_000_000,
        min_interval_seconds=2.0,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(),
        field_mappings={"geo_scheme": None, "tables": [f"kfrench.{d}" for d in DATASETS]},
        notes="4 small zips (~0.2 MB) + the library page per full run; no licence granted (restricted table rights)",
    )


def _period(token: str) -> tuple[str, date, date]:
    if len(token) == 8:
        d = date(int(token[:4]), int(token[4:6]), int(token[6:]))
        return "D", *hc.day_span(d)
    if len(token) == 6:
        y, m = int(token[:4]), int(token[4:])
        if not 1 <= m <= 12:
            raise hc.err(f"bad month {token!r}")
        return "M", date(y, m, 1), (date(y, m + 1, 1) if m < 12 else date(y + 1, 1, 1))
    y = int(token)
    return "A", date(y, 1, 1), date(y + 1, 1, 1)


def parse(ds: str, f: hc.Fetched, rows: fd.PeriodRows) -> dict[str, Any]:
    _zname, member = FILES_BY_DS[ds]
    z = fd.zip_member(f, member, max_bytes=50_000_000)
    text = io.TextIOWrapper(z.open(member), encoding="utf-8-sig", errors="strict").read()
    lines = text.splitlines()
    head: list[str] = []
    for ln in lines:
        if ln.startswith(","):
            break
        if ln.strip():
            head.append(ln.strip())
    note = " ".join(head)[:1000]
    m = _VINTAGE.search(note)
    if not m:
        raise hc.err(f"{ds}: CRSP vintage line not found in the file header")
    if not any(_COPY in ln for ln in lines[-5:]):
        raise hc.err(f"{ds}: copyright line not found at the end of the file")
    want = COLUMNS[ds]
    cols: list[str] | None = None
    rep: Counter[str] = Counter()
    for n, ln in enumerate(lines, start=1):
        if ln.startswith(","):
            cols = [c.strip() for c in ln.split(",")[1:]]
            if tuple(cols) != want:
                raise hc.err(f"{ds}: line {n}: columns {cols} != {list(want)}")
            continue
        mm = _ROW.match(ln)
        if not mm:
            continue
        if cols is None:
            raise hc.err(f"{ds}: line {n}: data row before a column header")
        freq, start, end = _period(mm.group(1))
        cells = [c.strip() for c in mm.group(2).split(",")]
        if len(cells) != len(cols):
            raise hc.err(f"{ds}: line {n}: {len(cells)} cells for {len(cols)} columns")
        for c, cell in zip(cols, cells, strict=True):
            if cell in MISSING:
                rep["missing_marker"] += 1
                continue
            v, normalised = hc.number(cell)
            if v is None:
                rep["not_a_number"] += 1
                continue
            extra: dict[str, Any] = {"frequency": freq, "precision": fd.PRECISION[freq], "provider_note": note,
                                     "provider_vintage": f"{m.group(1)} CRSP database",
                                     "licence_restriction": "no_licence_granted", "redistribution": "not_established",
                                     "estimate_kind": "observation"}  # fmt: skip
            if normalised:
                extra["value_normalised"] = True
            ind = f"FF|factor={c}" + ("|freq=A" if freq == "A" else "")
            rows.add_period(indicator=ind, provider_geo="US", start=start, end=end, value=v,
                            unit="percent return per period",
                            flags=rows.flags(**hc.base_flags(RIGHTS[ds], f, **extra)), raw_sha256=f.sha256,
                            selector={"file": member, "line": n, "column": c})  # fmt: skip
            rep[f"rows_{freq}"] += 1
    return {"crsp_vintage": m.group(1), **fd.counter_report(rep)}


def build(files: Mapping[str, hc.Fetched], wanted: tuple[str, ...]) -> tuple[dict[str, hc.TableBuild], dict[str, Any]]:
    out: dict[str, hc.TableBuild] = {}
    report: dict[str, Any] = {}
    for ds in wanted:
        rows = fd.PeriodRows(ATLAS_SOURCE, ds)
        report[ds] = parse(ds, files[ds], rows)
        out[ds] = hc.TableBuild(rows)
    report["policy_quotes_found"] = hc.check_policy_pages(files, FILES, RIGHTS, wanted)
    return out, report


NOTES = {
    ds: f"Kenneth R. French Data Library file {FILES_BY_DS[ds][0]}: Fama/French factors {', '.join(COLUMNS[ds])} "
    "(US stocks, CRSP), returns in percent per period, verbatim; monthly/daily and annual (|freq=A) indicators; "
    "missing markers -99.99/-999 are no row. provider_geo US = the US stock market universe (not a statistic of "
    "the country). NO LICENCE GRANTED (copyright Fama and French, CRSP-derived inputs): restricted, internal "
    "research use only."
    for ds in DATASETS
}


async def atlas_ingest(run: Any) -> dict[str, Any]:
    # no geography scheme: "US" names the stock-market universe, not a country statistic (iso3 stays empty)
    return await fd.ingest_files(run, FILES, DATASETS, build, rights=RIGHTS, notes=NOTES, source_key=SOURCE_KEY,
                                 geo_scheme=None)  # fmt: skip


__all__ = ["DATASETS", "FILES", "NAME", "RIGHTS", "atlas_ingest", "atlas_source", "build"]
