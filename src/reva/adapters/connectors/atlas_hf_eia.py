"""EIA daily spot prices (crude oil, petroleum products, Henry Hub natural gas) -> atlas table (V5, agent PRED-HF).

Source: the EIA petroleum and natural gas data browser history pages, one per series, daily view
(``https://www.eia.gov/dnav/pet/hist/<SERIES>D.htm``, ``https://www.eia.gov/dnav/ng/hist/rngwhhdD.htm``): an HTML
table with one row per week ("``1987 May-18 to May-22``") and five weekday columns Mon..Fri, the values as EIA
prints them, and the page's "Release Date" / "Next Release Date". No key is needed (the EIA API v2 needs one; the
``hist_xls`` downloads hold the same numbers as binary BIFF8 workbooks, which this project cannot read without a
new dependency; ``PET.zip`` bulk is 55 MB). The day of a value is the Monday of its week row plus the column offset
(``Mon`` = +0 … ``Fri`` = +4); REVA checks that the row start is a Monday and that its printed end is start + 4
days (fail closed otherwise).

Table ``eia.spot_d``: indicator ``EIA_SPOT|series=<EIA series id>`` (``RWTC``, ``RBRTE``, ``EER_EPMRU_PF4_Y35NY_DPG``
…, ``RNGWHHD``), ``provider_geo`` = the trading location as printed in the EIA series title (``Cushing, OK``,
``Europe``, ``New York Harbor``, ``U.S. Gulf Coast``, ``Los Angeles``, ``Mont Belvieu, TX``, ``Henry Hub``; not a
country, ``iso3`` null), unit from the title (``Dollars per Barrel`` / ``per Gallon`` / ``per Million Btu``).
EIA notation (page footnote): ``-`` No Data Reported, ``--`` Not Applicable, ``NA`` Not Available, ``W`` Withheld
-> no row (counted). Negative prices are kept as printed (WTI 2020-04-20).

Licence per value (owner review flagged): EIA says U.S. government publications are in the public domain, but the
table-definition pages name the source of these prices as "Refinitiv, an LSEG business" and EIA's copyright page
warns that information "contributed or licensed by ... companies ... may be protected". Every row carries
``flags.licence_review``; futures prices (NYMEX/CME) are not taken.
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

NAME = "atlas.eia_spot"
VERSION = "1.0.0"
PARSER_VERSION = "atlas_hf_eia/1"
SOURCE_KEY = "eia.dnav"
ATLAS_SOURCE = "eia"
DATASETS = ("spot_d",)
HOST = "www.eia.gov"
HTML_MEDIA = ("text/html",)
COPYRIGHT_URL = f"https://{HOST}/about/copyrights_reuse.php"


@dataclass(frozen=True)
class Series:
    sid: str
    area: str  # "pet" | "ng"
    location: str  # as printed in the EIA title (checked at run time)
    product: str

    @property
    def url(self) -> str:
        return f"https://{HOST}/dnav/{self.area}/hist/{self.sid if self.area == 'pet' else self.sid.lower()}D.htm"


SERIES: tuple[Series, ...] = (
    Series("RWTC", "pet", "Cushing, OK", "WTI crude oil"),
    Series("RBRTE", "pet", "Europe", "Brent crude oil"),
    Series("EER_EPMRU_PF4_Y35NY_DPG", "pet", "New York Harbor", "conventional gasoline regular"),
    Series("EER_EPMRU_PF4_RGC_DPG", "pet", "U.S. Gulf Coast", "conventional gasoline regular"),
    Series("EER_EPMRR_PF4_Y05LA_DPG", "pet", "Los Angeles", "RBOB regular gasoline"),
    Series("EER_EPD2F_PF4_Y35NY_DPG", "pet", "New York Harbor", "No. 2 heating oil"),
    Series("EER_EPD2DXL0_PF4_Y35NY_DPG", "pet", "New York Harbor", "ultra-low sulfur No. 2 diesel"),
    Series("EER_EPD2DXL0_PF4_RGC_DPG", "pet", "U.S. Gulf Coast", "ultra-low sulfur No. 2 diesel"),
    Series("EER_EPD2DC_PF4_Y05LA_DPG", "pet", "Los Angeles", "ultra-low sulfur CARB diesel"),
    Series("EER_EPJK_PF4_RGC_DPG", "pet", "U.S. Gulf Coast", "kerosene-type jet fuel"),
    Series("EER_EPLLPA_PF4_Y44MB_DPG", "pet", "Mont Belvieu, TX", "propane"),
    Series("RNGWHHD", "ng", "Henry Hub", "natural gas"),
)

FILES: tuple[hc.FileSpec, ...] = (
    *(hc.FileSpec(f"hist_{s.sid}", s.url, DATASETS, HTML_MEDIA, 3_000_000) for s in SERIES),
    hc.FileSpec("pet_defs", f"https://{HOST}/dnav/pet/TblDefs/pet_pri_spt_tbldef2.asp", DATASETS, HTML_MEDIA, 1_000_000,
                role="policy"),
    hc.FileSpec("ng_defs", f"https://{HOST}/dnav/ng/TblDefs/ng_pri_fut_tbldef2.asp", DATASETS, HTML_MEDIA, 1_000_000,
                role="policy"),
    hc.FileSpec("copyright", COPYRIGHT_URL, DATASETS, HTML_MEDIA, 2_000_000, role="policy"),
)  # fmt: skip

LICENCE = (
    "EIA: U.S. government publications are in the public domain; these prices are third-party data (Refinitiv, an LSEG "
    "business) republished by EIA"
)
LICENCE_REVIEW = (
    "third-party spot prices (Refinitiv, an LSEG business) republished by EIA; EIA warns licensed third-party "
    "material may be protected: owner review before redistribution"
)
RIGHTS: dict[str, dict[str, Any]] = {
    "spot_d": {
        "licence": LICENCE,
        "licence_url": COPYRIGHT_URL,
        "licence_review": LICENCE_REVIEW,
        "terms_quote": "U.S. government publications are in the public domain and are not subject to copyright "
        "protection.",
        "attribution": "Source: U.S. Energy Information Administration (EIA), spot prices from Refinitiv, an LSEG "
        "business",
        "licence_checked": "2026-10-17",
        "provider": "U.S. Energy Information Administration",
        "publication": "weekly EIA data-browser release (flags.release_date / next_release_date of the archived page)",
        "pub_lag": "weekly_release",
        "revision_policy": "not stated by provider; value as fetched (as_of) in the release of flags.release_date",
        "policy_quotes": {
            "pet_source": ("pet_defs", "Sources Refinitiv, an LSEG business"),
            "pet_averages": ("pet_defs", "Weekly, monthly, and annual prices are calculated by EIA from daily data by "
                             "taking an unweighted average of the daily closing spot prices for a given product over "
                             "the specified time period."),
            "ng_source": ("ng_defs", "Spot Price: Refinitiv, an LSEG business."),
            "public_domain": ("copyright", "U.S. government publications are in the public domain and are not subject "
                              "to copyright protection."),
            "protected": ("copyright", "You may see on our website documents, illustrations, photographs, or other "
                          "information resources contributed or licensed by private individuals, companies, or "
                          "organizations that may be protected by U.S. and foreign copyright laws."),
        },
    }
}  # fmt: skip

#: EIA notation printed in the page footnote: no row for any of them (never zero)
NOTATION = {"-": "No Data Reported", "--": "Not Applicable", "NA": "Not Available", "W": "Withheld"}
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri")
MONTHS = {m: i for i, m in enumerate(("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov",
                                      "Dec"), start=1)}  # fmt: skip
TITLE_RE = re.compile(r"class='Title1'>([^<]+)</td>")
ROW_RE = re.compile(r"<td class='B6'>([^<]*)</td>((?:\s*<td class='B3'>[^<]*</td>){5})", re.S)
CELL_RE = re.compile(r"<td class='B3'>([^<]*)</td>")
WEEK_RE = re.compile(r"^(\d{4}) ([A-Z][a-z]{2})-\s?(\d{1,2}) to ([A-Z][a-z]{2})-\s?(\d{1,2})$")
RELEASE_RE = re.compile(r"(Next )?Release Date:\s*(\d{2}/\d{2}/\d{4})")
UNIT_RE = re.compile(r"\(([^()]+)\)\s*$")


def code_digest() -> str:
    return hc.code_digest(sys.modules[__name__])


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=NAME,
        version=VERSION,
        source_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="U.S. Energy Information Administration (petroleum and natural gas data browser, daily spot prices)",
        licence=LICENCE,
        allowed_hosts=(HOST,),
        datasets=DATASETS,
        dataset_ids=tuple(f"EIA:{s.sid}.D" for s in SERIES),
        cadence_seconds=7 * hc.DAY,
        planned_requests=len(FILES),
        max_response_bytes=3_000_000,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(),
        field_mappings={"geo": "trading location as printed in the EIA title; iso3 null", "tables": ["eia.spot_d"]},
        notes="12 daily history pages (~4.6 MB) + 3 definition/terms pages per full run",
    )


def _mdy(s: str) -> str:
    return datetime.strptime(s, "%m/%d/%Y").date().isoformat()


def parse_page(s: Series, f: hc.Fetched, rows: hc.DailyRows, report: dict[str, Any]) -> None:
    t = f.text("latin-1")
    titles = TITLE_RE.findall(t)
    if len(titles) != 1:
        raise hc.err(f"{s.sid}: expected one table title, found {len(titles)}")
    title = re.sub(r"\s+", " ", html.unescape(titles[0])).strip()
    if s.location not in title:
        raise hc.err(f"{s.sid}: location {s.location!r} not in title {title!r}")
    um = UNIT_RE.search(title)
    if not um:
        raise hc.err(f"{s.sid}: no unit in title {title!r}")
    unit = um.group(1).strip()
    heads = re.findall(r"<th[^>]*>([^<]*)</th>", t)
    heads = [html.unescape(h).replace("\xa0", " ").strip() for h in heads]
    if heads[:6] != ["Week Of", *WEEKDAYS]:
        raise hc.err(f"{s.sid}: table header {heads[:6]} is not Week Of + Mon..Fri")
    rel = {bool(n): _mdy(d) for n, d in RELEASE_RE.findall(t)}
    if False not in rel:
        raise hc.err(f"{s.sid}: no Release Date on the page")
    rights = RIGHTS["spot_d"]
    fl = rows.flags(**hc.base_flags(rights, f, series_title=title, product=s.product, release_date=rel[False],
                                    next_release_date=rel.get(True), eia_series=s.sid))  # fmt: skip
    fl_norm = rows.flags(**hc.base_flags(rights, f, series_title=title, product=s.product, release_date=rel[False],
                                         next_release_date=rel.get(True), eia_series=s.sid,
                                         value_normalised=True))  # fmt: skip
    notation: Counter[str] = Counter()
    weeks = 0
    for m in ROW_RE.finditer(t):
        label = html.unescape(m.group(1)).replace("\xa0", " ").strip()
        w = WEEK_RE.match(label)
        if not w:
            raise hc.err(f"{s.sid}: week label {label!r} not understood")
        y, m1, d1, m2, d2 = w.groups()
        if m1 not in MONTHS or m2 not in MONTHS:
            raise hc.err(f"{s.sid}: month in {label!r}")
        start = date(int(y), MONTHS[m1], int(d1))
        end = start + timedelta(days=4)
        if start.weekday() != 0 or (end.month, end.day) != (MONTHS[m2], int(d2)):
            raise hc.err(f"{s.sid}: week {label!r} is not Monday..Friday")
        weeks += 1
        for k, cell in enumerate(CELL_RE.findall(m.group(2))):
            c = html.unescape(cell).replace("\xa0", " ").strip()
            if not c:
                notation["<empty>"] += 1
                continue
            if c in NOTATION:
                notation[c] += 1
                continue
            v, normalised = hc.number(c)
            if v is None:
                raise hc.err(f"{s.sid}: cell {c!r} in week {label!r} is neither a number nor EIA notation")
            rows.add(indicator=f"EIA_SPOT|series={s.sid}", provider_geo=s.location, day=start + timedelta(days=k),
                     value=v, unit=unit, flags=fl_norm if normalised else fl, raw_sha256=f.sha256,
                     selector={"file": f.key, "byte_offset": m.start(), "week": label, "column": WEEKDAYS[k]})  # fmt: skip
    if not weeks:
        raise hc.err(f"{s.sid}: no week rows", kind="incomplete")
    report.setdefault("series", {})[s.sid] = {"weeks": weeks, "no_row_cells": dict(notation), "unit": unit,
                                              "release_date": rel[False]}  # fmt: skip


def build(
    files: Mapping[str, hc.Fetched], wanted: tuple[str, ...] = DATASETS
) -> tuple[dict[str, hc.TableBuild], dict[str, Any]]:
    out = {ds: hc.TableBuild(hc.DailyRows(ATLAS_SOURCE, ds)) for ds in wanted}
    report: dict[str, Any] = {}
    if "spot_d" in wanted:
        for s in SERIES:
            parse_page(s, files[f"hist_{s.sid}"], out["spot_d"].rows, report)
    report["policy_quotes_found"] = hc.check_policy_pages(files, FILES, RIGHTS, wanted)
    return out, report


NOTES = {
    "spot_d": "EIA daily spot prices (closing), 11 petroleum series + Henry Hub natural gas, from the EIA data browser "
    "daily history pages; day = Monday of the week row + weekday column. provider_geo = trading location printed in "
    "the EIA title (not a country). Licence review flagged on every row: prices sourced by EIA from Refinitiv (LSEG). "
    "flags.release_date = EIA release of this vintage; flags.as_of = fetch date.",
}


async def atlas_ingest(run: Any) -> dict[str, Any]:
    wanted = tuple(d for d in DATASETS if run.wants(d))
    files = await hc.fetch_all(run, FILES, wanted)
    builds, report = build(files, wanted)
    tables = hc.register_tables(run, builds, rights=RIGHTS, notes=NOTES, source_key=SOURCE_KEY)
    return {"ok": True, "tables": tables, "report": report}


__all__ = ["DATASETS", "FILES", "NAME", "RIGHTS", "SERIES", "atlas_ingest", "atlas_source", "build"]
