"""US Treasury daily par yield curves -> atlas tables (V5, agent PRED-HF).

Source: the Treasury "Daily Treasury Par Yield Curve Rates" and "Daily Treasury Par Real Yield Curve Rates" CSV
downloads, one file per calendar year (the all-years variant answers 403 to non-browser clients; per-year files are
the documented download): ``https://home.treasury.gov/resource-center/data-chart-center/interest-rates/
daily-treasury-rates.csv/<year>/all?type=<type>&field_tdr_date_value=<year>&page&_format=csv``.

==============  ==========================================  ==================================================
dataset         type                                        content
==============  ==========================================  ==================================================
``par_yield``   ``daily_treasury_yield_curve`` (1990-)       nominal par yields read at fixed tenors (``1 Mo``
                                                            … ``30 Yr``; tenors appear and disappear: 2 Mo from
                                                            2018-10, 4 Mo from 2022-10, 1.5 Month from 2025)
``real_yield``  ``daily_treasury_real_yield_curve`` (2003-)  real (TIPS) par yields, ``5 YR`` … ``30 YR``
==============  ==========================================  ==================================================

Indicator ``UST_PAR|tenor=<column header verbatim>`` / ``UST_REAL_PAR|tenor=<header>``; an empty cell (tenor not
published that day) is no row. ``provider_geo`` = ``US`` (issuer; ``iso3`` USA through the ISO alpha-2 crosswalk).
Unit: the CSV states none; Treasury publishes these yields as percent (``flags.unit_basis`` says so).

Semantics (quoted in :data:`RIGHTS`, checked against the archived methodology page at each run): inputs are
"indicative, bid-side market price quotations (not actual transactions) … obtained by the Federal Reserve Bank of
New York at or near 3:30 PM each trading day"; Treasury "reserves the option to make changes to the yield curve as
appropriate and in its sole discretion"; the monotone convex method replaced the quasi-cubic hermite spline as of
December 6, 2021 (``flags.curve_method`` on every nominal row, by date). No publication time or revision policy is
stated: ``flags.as_of`` (fetch date) is the earliest availability REVA can prove.

Licence: works of the U.S. Government are not subject to copyright in the United States (17 U.S.C. § 105); the
Treasury interest-rate pages carry no reuse restriction (checked 2026-10-17).
"""

from __future__ import annotations

import csv
import io
import sys
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any

from reva.adapters.connectors import hf_daily_common as hc
from reva.atlas.sources import AtlasSourceSpec

NAME = "atlas.ust_yields"
VERSION = "1.0.0"
PARSER_VERSION = "atlas_hf_ust/1"
SOURCE_KEY = "ustreasury.rates"
ATLAS_SOURCE = "ust"
DATASETS = ("par_yield", "real_yield")
HOST = "home.treasury.gov"
GEO = "US"
GEO_SCHEME = "iso2"
CSV_BASE = f"https://{HOST}/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv"
METHOD_URL = (
    f"https://{HOST}/policy-issues/financing-the-government/interest-rate-statistics/treasury-yield-curve-methodology"
)
TYPES = {"par_yield": ("daily_treasury_yield_curve", 1990, "UST_PAR"),
         "real_yield": ("daily_treasury_real_yield_curve", 2003, "UST_REAL_PAR")}  # fmt: skip
#: offline fixtures (tests/fixtures/source/atlas.ust_yields): the first year and two recent years of each curve
FIXTURE_YEARS = {"par_yield": (1990, 2025, 2026), "real_yield": (2003, 2026)}
CSV_MEDIA = ("text/csv", "application/csv", "application/octet-stream")
METHOD_SWITCH = date(2021, 12, 6)

LICENCE = "U.S. Government work (17 U.S.C. 105): not subject to copyright in the United States"
_Q_INPUTS = (
    "Our inputs are indicative, bid-side market price quotations (not actual transactions) for the most recently "
    "auctioned securities obtained by the Federal Reserve Bank of New York at or near 3:30 PM each trading day."
)
_Q_RESERVE = (
    "Treasury reserves the option to make changes to the yield curve as appropriate and in its sole discretion."
)
_Q_SWITCH = (
    "The monotone convex method for deriving the official Treasury yield curve replaced the previous quasi-cubic "
    "hermite spline method as of December 6, 2021."
)
_BASE: dict[str, Any] = {
    "licence": LICENCE,
    "licence_url": "https://www.law.cornell.edu/uscode/text/17/105",
    "terms_quote": "Copyright protection under this title is not available for any work of the United States "
    "Government (17 U.S.C. 105).",
    "attribution": "Source: U.S. Department of the Treasury, Daily Treasury Par Yield Curve Rates",
    "licence_checked": "2026-10-17",
    "provider": "U.S. Department of the Treasury",
    "publication": "not stated; inputs are indicative bid-side quotations at or near 3:30 PM ET of the trading day",
    "pub_lag": "not_stated",
    "revision_policy": "Treasury may change the yield curve at its sole discretion (no schedule); value as fetched",
    "policy_quotes": {
        "inputs": ("method", _Q_INPUTS),
        "reserve": ("method", _Q_RESERVE),
        "method_switch": ("method", _Q_SWITCH),
    },
}
RIGHTS: dict[str, dict[str, Any]] = {ds: dict(_BASE) for ds in DATASETS}


def csv_url(kind: str, year: int) -> str:
    return f"{CSV_BASE}/{year}/all?type={kind}&field_tdr_date_value={year}&page&_format=csv"


def years_for(ds: str, run: Any) -> tuple[int, ...]:
    if run is None or getattr(run, "origin", "test_fixture") != "live":
        return FIXTURE_YEARS[ds]
    first = TYPES[ds][1]
    return tuple(range(first, datetime.now(UTC).year + 1))


def files_for(run: Any) -> tuple[hc.FileSpec, ...]:
    out = [hc.FileSpec("method", METHOD_URL, DATASETS, ("text/html",), 3_000_000, role="policy")]
    for ds, (kind, _, _) in TYPES.items():
        for y in years_for(ds, run):
            out.append(hc.FileSpec(f"{ds}_{y}", csv_url(kind, y), (ds,), CSV_MEDIA, 2_000_000))
    return tuple(out)


FILES = files_for(None)


def code_digest() -> str:
    return hc.code_digest(sys.modules[__name__])


def atlas_source() -> AtlasSourceSpec:
    live_n = 1 + sum(datetime.now(UTC).year + 1 - first for _, first, _ in TYPES.values())
    return AtlasSourceSpec(
        name=NAME,
        version=VERSION,
        source_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="U.S. Department of the Treasury (daily Treasury par and real par yield curve rates)",
        licence=LICENCE,
        allowed_hosts=(HOST,),
        datasets=DATASETS,
        dataset_ids=tuple(f"UST:{k}" for k, _, _ in TYPES.values()),
        cadence_seconds=hc.DAY,
        planned_requests=live_n,
        max_response_bytes=3_000_000,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(),
        field_mappings={"geo_scheme": GEO_SCHEME, "tables": [f"ust.{d}" for d in DATASETS]},
        notes="one small CSV per curve and year (1990-/2003-) + the methodology page; ~1.5 MB per full run",
    )


def parse_year(ds: str, f: hc.Fetched, rows: hc.DailyRows, report: dict[str, Any]) -> None:
    kind, _, prefix = TYPES[ds]
    reader = csv.reader(io.StringIO(f.text("utf-8-sig")))
    header = next(reader, None)
    if not header or header[0] != "Date" or len(header) < 2:
        raise hc.err(f"{ds}: {f.key}: header must start with Date")
    tenors = [h.strip() for h in header[1:]]
    if any(not t for t in tenors):
        raise hc.err(f"{ds}: {f.key}: empty tenor header")
    rights = RIGHTS[ds]
    empty = 0
    stats = report.setdefault(ds, {"files": 0, "empty_cells": 0, "tenors": set()})
    stats["files"] += 1
    stats["tenors"].update(tenors)
    for i, rec in enumerate(reader, start=2):
        if not rec or not rec[0].strip():
            continue
        try:
            d = datetime.strptime(rec[0].strip(), "%m/%d/%Y").date()
        except ValueError as e:
            raise hc.err(f"{ds}: {f.key}: line {i}: bad date {rec[0]!r}") from e
        method = None
        if ds == "par_yield":
            method = "monotone convex" if d >= METHOD_SWITCH else "quasi-cubic hermite spline"
        for j, t in enumerate(tenors, start=1):
            v, normalised = hc.number(rec[j] if j < len(rec) else "")
            if v is None:
                empty += 1
                continue
            fl = rows.flags(**hc.base_flags(rights, f, curve_type=kind, curve_method=method,
                                            unit_basis="CSV states no unit; Treasury yields are published in percent",
                                            value_normalised=normalised or None))  # fmt: skip
            rows.add(indicator=f"{prefix}|tenor={t}", provider_geo=GEO, day=d, value=v, unit="percent", flags=fl,
                     raw_sha256=f.sha256, selector={"file": f.key, "line": i, "column": t})  # fmt: skip
    stats["empty_cells"] += empty


def build(
    files: Mapping[str, hc.Fetched], wanted: tuple[str, ...] = DATASETS
) -> tuple[dict[str, hc.TableBuild], dict[str, Any]]:
    out = {ds: hc.TableBuild(hc.DailyRows(ATLAS_SOURCE, ds)) for ds in wanted}
    report: dict[str, Any] = {}
    for key in sorted(files):
        for ds in wanted:
            if key.startswith(f"{ds}_"):
                parse_year(ds, files[key], out[ds].rows, report)
    for ds in wanted:
        if ds in report:
            report[ds]["tenors"] = sorted(report[ds]["tenors"])
    report["policy_quotes_found"] = hc.check_policy_pages(files, [s for s in files_for(None) if s.role == "policy"],
                                                          RIGHTS, wanted)  # fmt: skip
    return out, report


NOTES = {
    "par_yield": "US Treasury daily par yield curve rates (nominal), percent, one row per published tenor and "
    "trading day since 1990; tenors added/removed over time (empty cells are no row). Curve method switch "
    "2021-12-06 in flags.curve_method. provider_geo US (issuer). flags.as_of = fetch date.",
    "real_yield": "US Treasury daily par real yield curve rates (TIPS-based), percent, since 2003. provider_geo US.",
}


async def atlas_ingest(run: Any) -> dict[str, Any]:
    wanted = tuple(d for d in DATASETS if run.wants(d))
    files = await hc.fetch_all(run, files_for(run), wanted)
    builds, report = build(files, wanted)
    counts = Counter(k.split("_")[0] for k in files)
    report["files_fetched"] = dict(counts)
    tables = hc.register_tables(run, builds, rights=RIGHTS, notes=NOTES, source_key=SOURCE_KEY, geo_scheme=GEO_SCHEME)
    return {"ok": True, "tables": tables, "report": report}


__all__ = ["DATASETS", "FILES", "NAME", "RIGHTS", "atlas_ingest", "atlas_source", "build", "files_for"]
