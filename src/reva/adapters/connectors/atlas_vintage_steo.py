"""EIA Short-Term Energy Outlook archives -> vintage atlas table (V6, agent PRED-ENERGY).

Source ``atlas.vintage_steo`` -> table ``vintage_steo.steo_archive``: the Excel data file (``<mon><yy>_base.xlsx``)
of past STEO issues, as listed on EIA's STEO Archives page (``https://www.eia.gov/outlooks/steo/outlook.php``,
columns "STEO Issues | Release Date | Full PDF Report | Excel Data File"). Each issue is one **vintage**: the
history EIA published at that release plus EIA's forecast of the following months. Conventions (flags,
availability, estimate kinds) are those of :mod:`.vintage_common`, so the vintage accessor
(:mod:`reva.evaluation.vintages`) reads this table like the WEO / SPF vintages.

* **Which issues.** The issues whose month is in :data:`ISSUE_MONTHS` (January, April, July, October) from
  :data:`FIRST_ISSUE` (July 2013, the first issue whose data file is ``.xlsx``; earlier files are binary ``.xls``,
  which the project's stdlib reader cannot open) to the latest listed: ~54 files of ~0.85-1.1 MB (raw archive
  ≤ 100 MB rule). A bigger machine can widen :data:`ISSUE_MONTHS` to all twelve months (bump the version).
* **Which tables.** Sheets :data:`SHEETS` (U.S. energy markets summary, energy prices, world petroleum
  production / consumption / inventories, world crude oil production by country, U.S. petroleum supply and
  inventories, U.S. natural gas): every row whose first cell is an EIA series code.
* **Periods.** Monthly columns, read from each sheet's own year/month header rows and checked against the
  workbook's ``Dates`` sheet (``AAAA_DATEX`` row); a sheet whose header does not agree is skipped (counted).
* **Availability.** ``available_at`` = the day after the issue's "Release Date" (00:00 UTC; EIA releases at
  midday Eastern time) when the archived file's ``Last-Modified`` header is on/before the release day (rule
  ``steo_archive_release_date``); a file modified later may have been replaced after release, so its vintage is
  available only from the day after ``Last-Modified`` (rule ``steo_archive_file_last_modified``); no header ->
  no proof -> ``available_at = null`` (never selected).
* **Observations vs forecasts.** Recent workbooks declare a workbook-wide "Last Historical Month" in the
  ``Dates`` sheet: later months are ``projection``; earlier months are ``published_history_boundary_unknown``
  because the boundary is per workbook, not per series (EIA's bulk file shows world production / inventories
  with an earlier per-series ``lastHistoricalPeriod``: those months are EIA estimates). Older workbooks declare
  no boundary: months from the issue month on (not yet over at release) are ``projection``; earlier months
  ``published_history_boundary_unknown`` (``flags.workbook_historical_column = null``).
* **Identity.** ``variable`` = ``eia_steo:<CODE>`` (EIA's series code, upper-cased: the workbooks write
  ``papr_world``, the bulk file ``STEO.PAPR_WORLD.M``; ``provider_variable`` keeps the cell text verbatim);
  ``provider_geo = not_stated`` (the workbooks state no geography; the code names it and the bulk STEO table
  declares it per code). Row label, table heading and table title are quoted in ``flags``.
* Values are the cached cell text (no float conversion; exponent notation written out by
  ``decimal_or_none``); text cells (``-``, ``NA``, ``n/a`` …) are not values (counted, never zero).

Licence: U.S. public domain (``https://www.eia.gov/about/copyrights_reuse.php``).
"""

from __future__ import annotations

import hashlib
import inspect
import re
import sys
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from typing import Any

from reva.adapters.connectors.atlas_xlsx import Workbook, XlsxError
from reva.adapters.connectors.vintage_common import (
    Rows,
    coverage_line,
    day_after,
    decimal_or_none,
    indicator,
    month_bounds,
    vintage_flags,
)
from reva.atlas.sources import AtlasSourceSpec
from reva.kernel.errors import SourceResponseError

NAME = "atlas.vintage_steo"
VERSION = "1.0.0"
PARSER_VERSION = "atlas_vintage_steo/1"
SOURCE_KEY = "eia.steo_archive"
ATLAS_SOURCE = "vintage_steo"
DS = "steo_archive"
FAMILY = "eia_steo"
HOST = "www.eia.gov"
PAGE_URL = f"https://{HOST}/outlooks/steo/outlook.php"
FILE_URL = f"https://{HOST}/outlooks/steo/archives/{{file}}"
TERMS_URL = "https://www.eia.gov/about/copyrights_reuse.php"
LICENCE = "U.S. public domain (EIA: 'U.S. government publications are in the public domain')"
XLSX_MEDIA = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/octet-stream",
    "application/zip",
)
FIRST_ISSUE = (2013, 7)
ISSUE_MONTHS = (1, 4, 7, 10)
SHEETS = ("1tab", "2tab", "3atab", "3dtab", "4atab", "5atab")
MAX_FILE_BYTES = 5_000_000
MAX_PAGE_BYTES = 2_000_000
NOT_STATED = "not_stated"
MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
MONTH_NAMES = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
CODE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{1,40}$")
_ISSUE_RE = re.compile(r"^(" + "|".join(MONTH_NAMES) + r") ([0-9]{4})$")
_DATE_RE = re.compile(r"^([0-9]{1,2})/([0-9]{1,2})/([0-9]{4})$")
_NOTICE_RE = re.compile(r'href="archives/(notice_data_[0-9_]+\.php)"')
_FILE_RE = re.compile(r'href="archives/([a-z]{3}[0-9]{2}_base\.xlsx?)"')
_TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_UNIT_RE = re.compile(r"\(([^()]*)\)\s*$")
_FOOTNOTE_RE = re.compile(r"\s*\([a-z]{1,2}\)\s*$")
_YYYYMM = re.compile(r"^([0-9]{4})(0[1-9]|1[0-2])$")


class SteoArchiveError(SourceResponseError):
    def __init__(self, msg: str) -> None:
        super().__init__(msg, retryable=False, kind="parse_error")


def code_digest() -> str:
    return "sha256:" + hashlib.sha256(inspect.getsource(sys.modules[__name__]).encode()).hexdigest()


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=NAME,
        version=VERSION,
        source_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="U.S. Energy Information Administration (EIA), Short-Term Energy Outlook archives (Excel data files)",
        licence=LICENCE,
        allowed_hosts=(HOST,),
        datasets=(DS,),
        dataset_ids=("STEO archives",),
        cadence_seconds=30 * 86_400,
        planned_requests=70,
        max_response_bytes=MAX_FILE_BYTES,
        min_interval_seconds=2.0,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(),
        field_mappings={"atlas_tables": [f"{ATLAS_SOURCE}.{DS}"], "vintage_family": FAMILY, "geo": NOT_STATED},
        notes="STEO Archives page + one <mon><yy>_base.xlsx per selected issue (quarterly issues from July 2013)",
    )


def rights() -> dict[str, Any]:
    return {
        "licence": "U.S. public domain",
        "licence_url": TERMS_URL,
        "terms_quote": "U.S. government publications are in the public domain and are not subject to copyright"
        " protection.",
        "attribution": "Source: U.S. Energy Information Administration (EIA), Short-Term Energy Outlook (archived "
        "issues).",
        "provider": "U.S. Energy Information Administration",
        "open_licence": True,
        "licence_checked": "2026-10-17",
        "source_key": SOURCE_KEY,
    }


# --------------------------------------------------------------------------- archives page


@dataclass(frozen=True)
class Issue:
    label: str  # "October 2026" (verbatim)
    year: int
    month: int
    release_text: str  # "10/06/2026" (verbatim)
    release: date
    file: str  # "oct26_base.xlsx"
    notice: str | None = None  # "notice_data_10_06_2026.php": EIA notice linked from the issue row

    @property
    def vintage(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"

    @property
    def url(self) -> str:
        return FILE_URL.format(file=self.file)


def _text(cell_html: str) -> str:
    return " ".join(_TAG_RE.sub(" ", cell_html).split())


def parse_archive_page(body: bytes) -> tuple[list[Issue], dict[str, int]]:
    """Issues listed on the STEO Archives page with their release date and Excel data file (verbatim)."""
    try:
        html = body.decode("utf-8")
    except UnicodeDecodeError:
        html = body.decode("latin-1")
    if "STEO Archives" not in html:
        raise SteoArchiveError("archives page: heading 'STEO Archives' not found")
    stats: dict[str, int] = {}
    out: dict[str, Issue] = {}
    # the page's table markup is irregular (some rows lack "<tr>"), so cells are read in document order: an
    # issue row is [issue label, release date, PDF link, Excel link, optional notice link]
    tds = _TD_RE.findall(html)
    for j, td in enumerate(tds):
        m = _ISSUE_RE.match(_text(td))
        if not m:
            continue
        stats["issues_listed"] = stats.get("issues_listed", 0) + 1
        d = _DATE_RE.match(_text(tds[j + 1])) if j + 1 < len(tds) else None
        if not d:
            stats["no_release_date"] = stats.get("no_release_date", 0) + 1
            continue
        f = _FILE_RE.search(tds[j + 3]) if j + 3 < len(tds) else None
        if not f:
            stats["no_excel_file"] = stats.get("no_excel_file", 0) + 1
            continue
        nt = _NOTICE_RE.search(tds[j + 4]) if j + 4 < len(tds) else None
        month = MONTH_NAMES.index(m.group(1)) + 1
        year = int(m.group(2))
        file = f.group(1)
        if file[:5] != f"{MONTHS[month - 1]}{year % 100:02d}":
            raise SteoArchiveError(f"archives page: issue {m.group(0)!r} links file {file!r}")
        rel = date(int(d.group(3)), int(d.group(1)), int(d.group(2)))
        iss = Issue(m.group(0), year, month, _text(tds[j + 1]), rel, file, nt.group(1) if nt else None)
        if iss.vintage in out and out[iss.vintage] != iss:
            raise SteoArchiveError(f"archives page lists issue {iss.label} twice with different rows")
        out[iss.vintage] = iss
    if not out:
        raise SteoArchiveError("archives page: no issue row with a release date and an Excel data file")
    return sorted(out.values(), key=lambda i: (i.year, i.month)), stats


def select_issues(issues: list[Issue], stats: dict[str, Any]) -> list[Issue]:
    sel: list[Issue] = []
    for i in issues:
        if (i.year, i.month) < FIRST_ISSUE or i.month not in ISSUE_MONTHS:
            continue
        if not i.file.endswith(".xlsx"):
            stats["not_xlsx"] = stats.get("not_xlsx", 0) + 1
            continue
        if (i.release.year, i.release.month) != (i.year, i.month):
            # a release outside the issue month would break the "issue month = first forecast month" rule
            stats.setdefault("release_outside_issue_month", []).append(i.label)
            continue
        sel.append(i)
    return sel


def availability(issue: Issue, last_modified: str | None) -> tuple[datetime | None, str | None, str | None]:
    """(available_at, rule, Last-Modified ISO text) — conservative, see the module docstring."""
    if not last_modified:
        return None, None, None
    try:
        lm = parsedate_to_datetime(last_modified)
    except (TypeError, ValueError):
        return None, None, None
    if lm.tzinfo is None:
        lm = lm.replace(tzinfo=UTC)
    lm = lm.astimezone(UTC)
    if lm.date() <= issue.release:
        return day_after(issue.release), "steo_archive_release_date", lm.isoformat()
    return day_after(lm.date()), "steo_archive_file_last_modified", lm.isoformat()


# --------------------------------------------------------------------------- workbook


@dataclass
class WorkbookReport:
    sheets: dict[str, int] = field(default_factory=dict)
    sheets_missing: list[str] = field(default_factory=list)
    sheets_skipped: dict[str, str] = field(default_factory=dict)
    series: int = 0
    cells: int = 0
    non_numeric: dict[str, int] = field(default_factory=dict)
    duplicate_codes: int = 0
    duplicate_conflicts: list[str] = field(default_factory=list)
    last_historical_month: str | None = None
    modeling_completion: str | None = None


def _dates_sheet(wb: Workbook) -> tuple[str, str | None, str | None, dict[int, int]]:
    """(forecast month text, last historical month YYYYMM or None, completion text, {col: month index})."""
    if "Dates" not in wb.sheets:
        raise SteoArchiveError("workbook has no Dates sheet")
    fm: str | None = None
    lhm: str | None = None
    done: str | None = None
    cols: dict[int, int] = {}
    for _r, cells in wb.rows("Dates"):
        head = cells[0].text.strip() if 0 in cells else ""
        if head.startswith("Forecast Month"):
            fm = cells[3].text.strip() if 3 in cells else None
        elif head.startswith("Last Historical Month"):
            lhm = cells[3].text.strip() if 3 in cells else None
        elif head.startswith("Modeling and analysis completion"):
            done = cells[6].text.strip() if 6 in cells else None
        elif 1 in cells and cells[1].text.strip().startswith("AAAA_DATEX") and not cols:
            for c, cell in cells.items():
                mm = _YYYYMM.match(cell.text.strip())
                if c >= 2 and mm:
                    cols[c] = int(mm.group(1)) * 12 + int(mm.group(2)) - 1
    if not fm:
        raise SteoArchiveError("Dates sheet: no 'Forecast Month' value")
    if lhm is not None and not _YYYYMM.match(lhm):
        raise SteoArchiveError(f"Dates sheet: Last Historical Month {lhm!r} is not YYYYMM")
    return fm, lhm, done, cols


def _sheet_columns(rows: list[tuple[int, dict[int, Any]]]) -> tuple[int, dict[int, int]] | None:
    """(header row number, {col: month index}) from the sheet's year row + month-name row."""
    month_abbr = {n[:3]: i for i, n in enumerate(MONTH_NAMES)}
    for k in range(min(len(rows) - 1, 12)):
        rn, cells = rows[k]
        rn2, nxt = rows[k + 1]
        if rn2 != rn + 1 or 2 not in cells or 2 not in nxt:
            continue
        if not re.fullmatch(r"[0-9]{4}", cells[2].text.strip()) or nxt[2].text.strip() != "Jan":
            continue
        out: dict[int, int] = {}
        year: int | None = None
        for c in sorted(nxt):
            if c < 2:
                continue
            if c in cells and re.fullmatch(r"[0-9]{4}", cells[c].text.strip()):
                year = int(cells[c].text.strip())
            mon = month_abbr.get(nxt[c].text.strip())
            if year is None or mon is None:
                return None
            out[c] = year * 12 + mon
        return rn2, out
    return None


def _unit(label: str, section: str | None, title: str | None) -> tuple[str, str]:
    for basis, txt in (("row_label", label), ("table_heading", section), ("table_title", title)):
        if txt:
            t = txt.strip()
            while _FOOTNOTE_RE.search(t):  # footnote markers "(a)", "(b)" are not units
                t = _FOOTNOTE_RE.sub("", t)
            m = _UNIT_RE.search(t)
            # a row label is a unit only when it is nothing but "(unit)" (Table 1 layout); elsewhere a
            # parenthesis in a row label names the series ("Crude oil (excluding SPR)", "Congo (Brazzaville)")
            if m and len(m.group(1).strip()) > 2 and (basis != "row_label" or m.start() == 0):
                return m.group(1).strip(), basis
    return "not stated (see flags.series_label / flags.table)", "none"


def parse_workbook(
    body: bytes,
    issue: Issue,
    raw_sha: str,
    rows: Rows,
    *,
    available_at: datetime | None,
    basis: str | None,
    last_modified: str | None,
) -> WorkbookReport:
    try:
        wb = Workbook(body)
    except XlsxError as e:
        raise SteoArchiveError(f"{issue.file}: not a readable workbook ({e})") from e
    rep = WorkbookReport()
    fm, lhm, done, date_cols = _dates_sheet(wb)
    if fm != issue.label:
        raise SteoArchiveError(f"{issue.file}: Dates 'Forecast Month' {fm!r} != archives page issue {issue.label!r}")
    issue_idx = issue.year * 12 + issue.month - 1
    lhm_idx = None
    if lhm is not None:
        lhm_idx = int(lhm[:4]) * 12 + int(lhm[4:]) - 1
        if lhm_idx >= issue_idx:
            raise SteoArchiveError(f"{issue.file}: Last Historical Month {lhm} is not before the issue month")
    rep.last_historical_month = lhm
    rep.modeling_completion = done
    seen: dict[tuple[str, int], str] = {}
    for sheet in SHEETS:
        if sheet not in wb.sheets:
            rep.sheets_missing.append(sheet)
            continue
        srows = list(wb.rows(sheet))
        hdr = _sheet_columns(srows)
        if hdr is None:
            rep.sheets_skipped[sheet] = "no year/month header rows"
            continue
        hdr_row, cols = hdr
        if date_cols and any(date_cols.get(c) != i for c, i in cols.items()):
            rep.sheets_skipped[sheet] = "month header disagrees with the Dates sheet"
            continue
        title = next((c[1].text.strip() for _r, c in srows[:2] if 1 in c and c[1].text.strip()), None)
        section: str | None = None
        n_series = 0
        for rn, cells in srows:
            if rn <= hdr_row:
                continue
            code_cell = cells.get(0)
            label = cells[1].text.strip() if 1 in cells and cells[1].kind == "s" else ""
            has_num = any(c in cols and cells[c].kind == "n" for c in cells)
            if code_cell is None or code_cell.kind != "s" or not CODE_RE.match(code_cell.text.strip()):
                if label and not has_num:
                    section = label
                continue
            code_text = code_cell.text.strip()
            code = code_text.upper()
            if not has_num:
                continue
            n_series += 1
            unit, unit_basis = _unit(label, section, title)
            for c, idx in sorted(cols.items()):
                cell = cells.get(c)
                if cell is None:
                    continue
                if cell.kind != "n":
                    t = cell.text.strip() or "(blank text)"
                    rep.non_numeric[t[:20]] = rep.non_numeric.get(t[:20], 0) + 1
                    continue
                val = decimal_or_none(cell.text)
                if val is None:
                    rep.non_numeric["(unparsable number)"] = rep.non_numeric.get("(unparsable number)", 0) + 1
                    continue
                key = (code, idx)
                if key in seen:
                    rep.duplicate_codes += 1
                    if seen[key] != val and len(rep.duplicate_conflicts) < 20:
                        rep.duplicate_conflicts.append(f"{code} {idx // 12}-{idx % 12 + 1:02d} {sheet}")
                    continue
                seen[key] = val
                if lhm_idx is not None:
                    projection = idx > lhm_idx
                    hist_col: bool | None = not projection
                else:
                    projection = idx >= issue_idx
                    hist_col = None
                y, m = idx // 12, idx % 12 + 1
                p0, p1 = month_bounds(y, m)
                rows.add(
                    source=ATLAS_SOURCE,
                    dataset=DS,
                    indicator=indicator(code, issue.vintage),
                    provider_geo=NOT_STATED,
                    start=p0,
                    end=p1,
                    value=val,
                    unit=unit,
                    flags=vintage_flags(
                        family=FAMILY,
                        vintage=issue.vintage,
                        available_at=available_at,
                        basis=basis,
                        variable=f"eia_steo:{code}",
                        provider_variable=code_text,
                        estimate_kind="projection" if projection else "published_history_boundary_unknown",
                        vintage_date_text=issue.release_text,
                        horizon=str(idx - issue_idx) if projection else None,
                        issue=issue.label,
                        series_label=label[:200] or None,
                        table=sheet,
                        table_title=(title or "")[:200] or None,
                        table_heading=(section or "")[:200] or None,
                        unit_basis=unit_basis,
                        workbook_last_historical_month=lhm,
                        workbook_historical_column=hist_col,
                        file=issue.file,
                        file_last_modified=last_modified,
                        eia_notice=(f"https://{HOST}/outlooks/steo/archives/{issue.notice}" if issue.notice else None),
                    ),
                    raw_sha256=raw_sha,
                    selector={"file": issue.file, "sheet": sheet, "row": rn, "col": c, "code": code_text},
                )
                rep.cells += 1
        rep.sheets[sheet] = n_series
        rep.series += n_series
    if rep.cells == 0:
        raise SteoArchiveError(f"{issue.file}: no value in sheets {SHEETS}")
    return rep


# --------------------------------------------------------------------------- run


async def atlas_ingest(run: Any) -> dict[str, Any]:
    page = await run.get(PAGE_URL, key="steo:archives_page", media_types=("text/html",), max_bytes=MAX_PAGE_BYTES)
    page_sha = run.archive(page)
    issues, page_stats = parse_archive_page(page.body)
    stats: dict[str, Any] = {"page": page_stats}
    selected = select_issues(issues, stats)
    rows = Rows()
    shas = [page_sha]
    per_issue: list[dict[str, Any]] = []
    for iss in selected:
        raw = await run.get(
            iss.url,
            key=f"steo:archive:{iss.file}",
            media_types=XLSX_MEDIA,
            max_bytes=MAX_FILE_BYTES,
            params={"issue": iss.label, "release_date": iss.release_text},
        )
        sha = run.archive(raw)
        shas.append(sha)
        headers = {str(k).lower(): v for k, v in (raw.response_metadata.get("headers") or {}).items()}
        avail, basis, lm = availability(iss, headers.get("last-modified"))
        rep = parse_workbook(raw.body, iss, sha, rows, available_at=avail, basis=basis, last_modified=lm)
        del raw
        per_issue.append(
            {
                "issue": iss.label,
                "release": iss.release_text,
                "file": iss.file,
                "bytes_sha256": sha,
                "available_at": avail.isoformat() if avail else None,
                "basis": basis,
                "last_historical_month": rep.last_historical_month,
                "series": rep.series,
                "cells": rep.cells,
                "sheets_missing": rep.sheets_missing,
                "sheets_skipped": rep.sheets_skipped,
                "non_numeric": rep.non_numeric,
                "duplicate_codes": rep.duplicate_codes,
                "duplicate_conflicts": rep.duplicate_conflicts,
            }
        )
    frame = rows.frame()
    note = (
        f"EIA STEO archived issues ({PAGE_URL}): {len(per_issue)} issues "
        f"({per_issue[0]['issue'] if per_issue else '-'} .. {per_issue[-1]['issue'] if per_issue else '-'}; months "
        f"{ISSUE_MONTHS}, from {FIRST_ISSUE[0]}-{FIRST_ISSUE[1]:02d}), sheets {SHEETS}; monthly columns; vintage = "
        "issue month; available_at = day after the Release Date (Last-Modified checked). Months after the "
        "workbook's Last Historical Month (or, without one, from the issue month on) are EIA projections; "
        f"earlier months are history as then published (per-series boundary not stated). Coverage: "
        f"{coverage_line(frame)}. Licence: U.S. public domain ({TERMS_URL})."
    )[:4000]
    reg = run.register(dataset=DS, frame=frame, raw_sha256s=shas, coverage_note=note, rights=rights())
    return {
        "tables": {
            DS: {
                "dataset_key": reg["dataset_key"],
                "rows": reg["row_count"],
                "created": reg["created"],
                "coverage": coverage_line(frame),
            }
        },
        "issues": per_issue,
        "stats": stats,
        "requests": 1 + len(selected),
    }


__all__ = [
    "NAME",
    "Issue",
    "SteoArchiveError",
    "atlas_ingest",
    "atlas_source",
    "availability",
    "parse_archive_page",
    "parse_workbook",
    "select_issues",
]
