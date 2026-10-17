"""EIA Weekly Petroleum Status Report (WPSR) series -> atlas table (V7, agent BEAT-ENERGY).

Source: the EIA petroleum data browser weekly history pages, one per series
(``https://www.eia.gov/dnav/pet/hist/LeafHandler.ashx?n=PET&s=<SERIES>&f=W``): an HTML table with one row per
month ("``2026-Oct``") and up to five (End Date ``MM/DD``, Value) pairs, the values as EIA prints them, and the
page's "Release Date" / "Next Release Date". No key is needed (the EIA API v2 needs one). The week of a value ends
on its printed End Date (a Friday; checked, fail closed); its period is the seven days ending that day.

Series (all U.S. totals, EIA survey data): ``WCRFPUS2`` field production of crude oil (thousand b/d; EIA's weekly
estimate), ``WCESTUS1`` ending stocks excluding SPR of crude oil, ``WCSSTUS1`` crude oil in the SPR,
``WCRRIUS2`` refiner net input of crude oil, ``WGTSTUS1`` total gasoline stocks, ``WDISTUS1`` distillate fuel oil
stocks (stocks in thousand barrels). EIA notation ``-``/``--``/``NA``/``W`` -> no row (counted).

**Vintages.** The page holds the series as currently published (no archive of past releases is offered here). Each
row records the page's release date (``flags.release_date``) and the conservative availability of the week's first
publication: the WPSR is released on the Wednesday after the week ending Friday (Thursday after a holiday), so
``flags.first_available_rule = week_end + 7 days`` (day after the latest usual release). Revisions of past weekly
values are not stated on the page (``revision_policy``); a real-time backtest that reads this table therefore
assumes weekly values unrevised, stated in its results.

Licence: EIA's own survey data — U.S. public domain (quote checked on EIA's copyright page at every run).
"""

from __future__ import annotations

import html
import re
import sys
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from reva.adapters.connectors import hf_daily_common as hc
from reva.atlas.sources import AtlasSourceSpec

NAME = "atlas.eia_wpsr"
VERSION = "1.0.0"
PARSER_VERSION = "atlas_eia_wpsr/1"
SOURCE_KEY = "eia.dnav"
ATLAS_SOURCE = "eia"
DATASETS = ("wpsr_w",)
HOST = "www.eia.gov"
HTML_MEDIA = ("text/html",)
COPYRIGHT_URL = f"https://{HOST}/about/copyrights_reuse.php"
AVAILABLE_AFTER_DAYS = 7


@dataclass(frozen=True)
class Series:
    sid: str
    title_start: str  # checked against the page title

    @property
    def url(self) -> str:
        return f"https://{HOST}/dnav/pet/hist/LeafHandler.ashx?n=PET&s={self.sid}&f=W"


SERIES: tuple[Series, ...] = (
    Series("WCRFPUS2", "Weekly U.S. Field Production of Crude Oil"),
    Series("WCESTUS1", "Weekly U.S. Ending Stocks excluding SPR of Crude Oil"),
    Series("WCSSTUS1", "Weekly U.S. Ending Stocks of Crude Oil in SPR"),
    Series("WCRRIUS2", "Weekly U.S. Refiner Net Input of Crude Oil"),
    Series("WGTSTUS1", "Weekly U.S. Ending Stocks of Total Gasoline"),
    Series("WDISTUS1", "Weekly U.S. Ending Stocks of Distillate Fuel Oil"),
)
FILES: tuple[hc.FileSpec, ...] = (
    *(hc.FileSpec(f"wpsr_{s.sid}", s.url, DATASETS, HTML_MEDIA, 2_000_000) for s in SERIES),
    hc.FileSpec("copyright", COPYRIGHT_URL, DATASETS, HTML_MEDIA, 2_000_000, role="policy"),
)
LICENCE = "EIA: U.S. government publications are in the public domain (EIA weekly survey data)"
RIGHTS: dict[str, dict[str, Any]] = {
    "wpsr_w": {
        "licence": LICENCE,
        "licence_url": COPYRIGHT_URL,
        "terms_quote": "U.S. government publications are in the public domain and are not subject to copyright "
        "protection.",
        "attribution": "Source: U.S. Energy Information Administration (EIA), Weekly Petroleum Status Report",
        "licence_checked": "2026-10-17",
        "provider": "U.S. Energy Information Administration",
        "publication": "weekly (WPSR, Wednesday after the week ending Friday); flags.release_date of the archived page",
        "pub_lag": "weekly_release",
        "revision_policy": "not stated on the page; value as fetched (as_of) in the release of flags.release_date",
        "policy_quotes": {
            "public_domain": ("copyright", "U.S. government publications are in the public domain and are not subject "
                              "to copyright protection."),
        },
    }
}  # fmt: skip

NOTATION = {"-": "No Data Reported", "--": "Not Applicable", "NA": "Not Available", "W": "Withheld"}
MONTHS = {m: i for i, m in enumerate(("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov",
                                      "Dec"), start=1)}  # fmt: skip
TITLE_RE = re.compile(r"class='Title1'>([^<]+)</td>")
ROW_RE = re.compile(
    r"<td class='B6'>([^<]*)</td>((?:\s*<td class='B5'>[^<]*</td>\s*<td class='B3'>[^<]*</td>){5})", re.S
)
PAIR_RE = re.compile(r"<td class='B5'>([^<]*)</td>\s*<td class='B3'>([^<]*)</td>")
MONTH_RE = re.compile(r"^(\d{4})-([A-Z][a-z]{2})$")
END_RE = re.compile(r"^(\d{2})/(\d{2})$")
RELEASE_RE = re.compile(r"(Next )?Release Date:\s*(\d{2}/\d{2}/\d{4})")
UNIT_RE = re.compile(r"\(([^()]+)\)\s*$")
THOUSANDS_RE = re.compile(r"^-?\d{1,3}(,\d{3})+$")
HEAD = ["Year-Month", "Week 1", "Week 2", "Week 3", "Week 4", "Week 5", *(["End Date", "Value"] * 5)]


def code_digest() -> str:
    return hc.code_digest(sys.modules[__name__])


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=NAME,
        version=VERSION,
        source_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="U.S. Energy Information Administration (petroleum data browser, Weekly Petroleum Status Report)",
        licence=LICENCE,
        allowed_hosts=(HOST,),
        datasets=DATASETS,
        dataset_ids=tuple(f"EIA:{s.sid}.W" for s in SERIES),
        cadence_seconds=7 * hc.DAY,
        planned_requests=len(FILES),
        max_response_bytes=2_000_000,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(),
        field_mappings={"geo": "U.S. totals (iso3 USA)", "tables": ["eia.wpsr_w"]},
        notes="6 weekly history pages (~1.4 MB) + EIA copyright page per full run",
    )


def _mdy(s: str) -> str:
    return datetime.strptime(s, "%m/%d/%Y").date().isoformat()


def _clean(s: str) -> str:
    return html.unescape(s).replace("\xa0", " ").strip()


def parse_page(s: Series, f: hc.Fetched, rows: hc.DailyRows, report: dict[str, Any]) -> None:
    t = f.text("latin-1")
    titles = TITLE_RE.findall(t)
    if len(titles) != 1:
        raise hc.err(f"{s.sid}: expected one table title, found {len(titles)}")
    title = re.sub(r"\s+", " ", _clean(titles[0]))
    if not title.startswith(s.title_start):
        raise hc.err(f"{s.sid}: title {title!r} does not start with {s.title_start!r}")
    um = UNIT_RE.search(title)
    if not um:
        raise hc.err(f"{s.sid}: no unit in title {title!r}")
    unit = um.group(1).strip()
    heads = [_clean(h) for h in re.findall(r"<th[^>]*>([^<]*)</th>", t)]
    if heads[: len(HEAD)] != HEAD:
        raise hc.err(f"{s.sid}: table header {heads[: len(HEAD)]} is not Year-Month + 5 x (End Date, Value)")
    rel = {bool(n): _mdy(d) for n, d in RELEASE_RE.findall(t)}
    if False not in rel:
        raise hc.err(f"{s.sid}: no Release Date on the page")
    rights = RIGHTS["wpsr_w"]
    common = {"series_title": title, "release_date": rel[False], "next_release_date": rel.get(True), "eia_series": s.sid,
              "first_available_rule": f"week_end + {AVAILABLE_AFTER_DAYS} days (WPSR Wednesday release, holiday margin)",
              "period": "week ending flags.week_end (period_start = week_end - 6 days)"}  # fmt: skip
    notation: Counter[str] = Counter()
    weeks = 0
    for m in ROW_RE.finditer(t):
        label = _clean(m.group(1))
        mm = MONTH_RE.match(label)
        if not mm or mm.group(2) not in MONTHS:
            raise hc.err(f"{s.sid}: month label {label!r} not understood")
        year, month = int(mm.group(1)), MONTHS[mm.group(2)]
        for k, (dcell, vcell) in enumerate(PAIR_RE.findall(m.group(2))):
            d, c = _clean(dcell), _clean(vcell)
            if not d:
                if c:
                    raise hc.err(f"{s.sid}: value {c!r} without an end date in {label!r}")
                continue
            e = END_RE.match(d)
            if not e or int(e.group(1)) != month:
                raise hc.err(f"{s.sid}: end date {d!r} not in row month {label!r}")
            end = date(year, month, int(e.group(2)))
            if end.weekday() != 4:
                raise hc.err(f"{s.sid}: week end {end} is not a Friday")
            weeks += 1
            if not c or c in NOTATION:
                notation[c or "<empty>"] += 1
                continue
            sep = bool(THOUSANDS_RE.match(c))  # EIA prints thousands separators ("8,634")
            v, normalised = hc.number(c.replace(",", "") if sep else c)
            normalised = normalised or sep
            if v is None:
                raise hc.err(f"{s.sid}: cell {c!r} in {label!r} is neither a number nor EIA notation")
            fl = rows.flags(**hc.base_flags(rights, f, **common, precision="week", week_end=end.isoformat(),
                                            first_available=(end + timedelta(days=AVAILABLE_AFTER_DAYS)).isoformat(),
                                            value_normalised=normalised or None))  # fmt: skip
            rows.add(indicator=f"EIA_WPSR|series={s.sid}", provider_geo="USA", iso3="USA", day=end - timedelta(days=6),
                     value=v, unit=unit, flags=fl, raw_sha256=f.sha256,
                     selector={"file": f.key, "byte_offset": m.start(), "month": label, "pair": k + 1})  # fmt: skip
            rows.cols["period_end"][-1] = end + timedelta(days=1)  # the week ending `end` (7 days)
    if not weeks:
        raise hc.err(f"{s.sid}: no week rows", kind="incomplete")
    report.setdefault("series", {})[s.sid] = {"weeks": weeks, "no_row_cells": dict(notation), "unit": unit,
                                              "release_date": rel[False]}  # fmt: skip


def build(
    files: Mapping[str, hc.Fetched], wanted: tuple[str, ...] = DATASETS
) -> tuple[dict[str, hc.TableBuild], dict[str, Any]]:
    out = {ds: hc.TableBuild(hc.DailyRows(ATLAS_SOURCE, ds)) for ds in wanted}
    report: dict[str, Any] = {}
    if "wpsr_w" in wanted:
        for s in SERIES:
            parse_page(s, files[f"wpsr_{s.sid}"], out["wpsr_w"].rows, report)
    report["policy_quotes_found"] = hc.check_policy_pages(files, FILES, RIGHTS, wanted)
    if not all(report["policy_quotes_found"].values()):
        raise hc.err(f"licence quote not found on the EIA copyright page: {report['policy_quotes_found']}")
    return out, report


NOTES = {
    "wpsr_w": "EIA Weekly Petroleum Status Report: U.S. crude oil field production (weekly estimate), crude stocks "
    "excl. SPR, SPR stocks, refiner net crude input, total gasoline and distillate stocks, from the EIA data browser "
    "weekly history pages; period = the 7 days ending the printed week-end Friday (flags.week_end). First "
    "availability rule: week_end + 7 days (flags.first_available). Values as published at flags.release_date; past "
    "revisions not stated by the page. U.S. public domain.",
}


async def atlas_ingest(run: Any) -> dict[str, Any]:
    wanted = tuple(d for d in DATASETS if run.wants(d))
    files = await hc.fetch_all(run, FILES, wanted)
    builds, report = build(files, wanted)
    tables = hc.register_tables(run, builds, rights=RIGHTS, notes=NOTES, source_key=SOURCE_KEY)
    return {"ok": True, "tables": tables, "report": report}


__all__ = ["DATASETS", "FILES", "NAME", "RIGHTS", "SERIES", "atlas_ingest", "atlas_source", "build", "parse_page"]
