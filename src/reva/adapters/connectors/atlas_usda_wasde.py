"""USDA WASDE monthly reports -> vintage atlas table (V6, agent V6AGRI).

Source ``atlas.usda_wasde`` -> table ``usda_wasde.wasde``: the USDA Office of the Chief Economist "Historical WASDE
Report Data" CSV files, one per World Agricultural Supply and Demand Estimates report,
``https://www.usda.gov/sites/default/files/documents/oce-wasde-report-data-<YYYY>-<MM>.csv`` (~1 MB each; two
corrected re-issues are named ``...-<YYYY>-<MM>-V2.csv``). USDA's note on the listing page (quoted in the report,
docs/dev/v6_agri.md): "The files below contain data as it was reported and appeared at the time of publication in
each World Agricultural Supply and Demand Estimates (WASDE) report from April 2010 to current ... These data do not
include subsequent revisions to estimates made to any underlying data series after each report was released".

* **Which reports.** Monthly files from :data:`FIRST_MONTH` (2021-01, the first single-month CSV) to the current
  month (run options ``first`` / ``last`` = ``YYYY-MM`` narrow it). For each month the plain name is tried, then
  the ``-V2`` name; a month with neither (no report, e.g. October 2025) is recorded as missing, never filled.
  The 2010-04..2020-12 history is published as two zip files that ``www.usda.gov`` answers with ``403 Access
  Denied`` to this client (the listing page too, 2026-10-17): not fetched (owner decision in the report).
* **Which rows.** Every row except the "Reliability of ... Projections" tables (statistics of past forecast errors,
  counted and skipped) and rows with an empty value (counted, never zero).
* **Identity.** ``variable`` = ``usda_wasde:<slug(ReportTitle)>.<slug(Commodity)>.<slug(Attribute)>`` (plus
  ``.quarterly`` for the quarterly U.S. animal-product rows); each label is kept verbatim in ``flags``.
  ``provider_geo`` = the ``Region`` text verbatim ("World", "India", "Major Exporters", ...): WASDE publishes names,
  not codes, so **no iso3 is derived** (names are never mapped to countries).
* **Periods.** ``MarketYear`` ``2025/26`` -> ``[2025-01-01, 2026-01-01)`` (first year of the marketing year, the
  same convention as ``atlas_usda_psd``; local marketing-year months are not in the file); ``2026`` -> the calendar
  year, or the quarter for ``AnnualQuarterFlag`` I..IV.
* **Observations vs forecasts.** ``ProjEstFlag`` ``Proj.`` / ``(Proj.)`` -> ``estimate_kind = projection``
  (USDA's projection for a marketing year at that report); ``Est.`` / ``(Est.)`` and unflagged years ->
  ``published_history`` (the value as then published; the WASDE label is kept in ``flags.wasde_status``).
* **Availability.** ``available_at`` = the day after the report's ``ReleaseDate`` (00:00 UTC) when the archived
  file's ``Last-Modified`` is on/before the release day (rule ``wasde_csv_release_date``); a file modified later
  (posted late or replaced) is available only from the day after ``Last-Modified``
  (``wasde_csv_file_last_modified``); no header -> no proof -> ``available_at = null``.

Licence: U.S. Government work (USDA), public domain in the United States (17 U.S.C. 105); attribution kept.
"""

from __future__ import annotations

import csv
import io
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from typing import Any

from reva.adapters.connectors.agri_common import module_digest, slug
from reva.adapters.connectors.vintage_common import (
    Rows,
    coverage_line,
    day_after,
    quarter_bounds,
    vintage_flags,
    year_bounds,
)
from reva.atlas.sources import AtlasSourceSpec
from reva.kernel.errors import SourceResponseError

NAME = "atlas.usda_wasde"
VERSION = "1.0.0"
PARSER_VERSION = "atlas_usda_wasde/1"
SOURCE_KEY = "usda.wasde"
ATLAS_SOURCE = "usda_wasde"
DS = "wasde"
FAMILY = "usda_wasde"
HOST = "www.usda.gov"
FILE_URL = f"https://{HOST}/sites/default/files/documents/oce-wasde-report-data-{{name}}.csv"
LISTING_PAGE = "https://www.usda.gov/about-usda/general-information/staff-offices/office-chief-economist/commodity-markets/wasde-report"
LICENCE = "U.S. Government work (USDA Office of the Chief Economist, WASDE): public domain in the United States"
ATTRIBUTION = "USDA, World Agricultural Supply and Demand Estimates (WASDE), Office of the Chief Economist"
CSV_MEDIA = ("text/csv", "application/csv", "text/plain", "application/octet-stream")
FIRST_MONTH = (2021, 1)
MAX_FILE_BYTES = 4_000_000
HEADER = (
    "WasdeNumber",
    "ReportDate",
    "ReportTitle",
    "Attribute",
    "ReliabilityProjection",
    "Commodity",
    "Region",
    "MarketYear",
    "ProjEstFlag",
    "AnnualQuarterFlag",
    "Value",
    "Unit",
    "ReleaseDate",
    "ReleaseTime",
    "ForecastYear",
    "ForecastMonth",
)
PROJ = frozenset({"Proj.", "(Proj.)"})
EST = frozenset({"Est.", "(Est.)", ""})
QUARTERS = {"I": 1, "II": 2, "III": 3, "IV": 4}
_MY = re.compile(r"^([0-9]{4})/([0-9]{2})$")
_Y = re.compile(r"^[0-9]{4}$")
_MONTH = re.compile(r"^([0-9]{4})-(0[1-9]|1[0-2])$")
_DEC = re.compile(r"^-?([0-9]+)?(\.[0-9]+)?$")


class WasdeError(SourceResponseError):
    def __init__(self, msg: str, kind: str = "schema_drift") -> None:
        super().__init__(msg, retryable=False, kind=kind)


@dataclass
class FileReport:
    month: str
    file: str
    rows: int = 0
    cells: int = 0
    reliability_rows: int = 0
    empty_values: int = 0
    no_period_rows: int = 0
    projections: int = 0
    release_date: str | None = None
    wasde_number: str | None = None
    available_at: str | None = None
    basis: str | None = None
    last_modified: str | None = None
    last_modified_date: str | None = None
    titles: dict[str, int] = field(default_factory=dict)


def months(first: tuple[int, int], last: tuple[int, int]) -> list[tuple[int, int]]:
    out, (y, m) = [], first
    while (y, m) <= last:
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _month_opt(v: Any, what: str) -> tuple[int, int]:
    mm = _MONTH.match(str(v or ""))
    if not mm:
        raise WasdeError(f"run option {what} must be YYYY-MM, got {v!r}", kind="bad_option")
    return int(mm.group(1)), int(mm.group(2))


def parse_release_date(text: str) -> date:
    t = text.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(t, fmt).date()
        except ValueError:
            continue
    raise WasdeError(f"unexpected ReleaseDate {text!r}")


def availability(release: date, last_modified: str | None) -> tuple[datetime | None, str | None, date | None]:
    """(available_at, rule, Last-Modified date) — conservative, see the module doc."""
    if not last_modified:
        return None, None, None
    try:
        lm = parsedate_to_datetime(last_modified).astimezone(UTC).date()
    except (TypeError, ValueError):
        return None, None, None
    if lm <= release:
        return day_after(release), "wasde_csv_release_date", lm
    return day_after(lm), "wasde_csv_file_last_modified", lm


def _decimal(v: str) -> str | None:
    s = v.strip()
    if not s or not _DEC.match(s) or s in ("-", ".", "-."):
        return None
    neg = s.startswith("-")
    s = s.lstrip("-")
    if s.startswith("."):
        s = "0" + s
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    s = s.lstrip("0") or "0"
    if s.startswith("."):
        s = "0" + s
    return ("-" + s) if neg and s != "0" else s


def _period(my: str, aq: str) -> tuple[date, date, str] | None:
    m = _MY.match(my)
    if m:
        y = int(m.group(1))
        if int(m.group(2)) != (y + 1) % 100 or aq != "Annual":
            raise WasdeError(f"unexpected MarketYear {my!r} / {aq!r}")
        s, e = year_bounds(y)
        return s, e, "marketing year (first year)"
    if _Y.match(my):
        y = int(my)
        if aq == "Annual":
            s, e = year_bounds(y)
            return s, e, "calendar year"
        if aq in QUARTERS:
            s, e = quarter_bounds(y, QUARTERS[aq])
            return s, e, "calendar quarter"
        raise WasdeError(f"unexpected AnnualQuarterFlag {aq!r}")
    if not my:
        return None
    raise WasdeError(f"unexpected MarketYear {my!r}")


def parse_csv(
    body: bytes,
    *,
    month: tuple[int, int],
    file: str,
    raw_sha256: str,
    last_modified: str | None,
    rows: Rows,
) -> FileReport:
    """One WASDE CSV -> cells appended to ``rows``. Layout surprises are typed :class:`WasdeError`."""
    vintage = f"{month[0]}-{month[1]:02d}"
    rep = FileReport(month=vintage, file=file, last_modified=last_modified)
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = body.decode("cp1252")
    rd = csv.reader(io.StringIO(text, newline=""))
    head = tuple(h.strip() for h in next(rd, []))
    if head != HEADER:
        raise WasdeError(f"{file}: header changed: {head}")
    data = [r for r in rd if r]
    if not data:
        raise WasdeError(f"{file}: no row")
    releases = {r[12].strip() for r in data}
    fy = {(r[14].strip(), r[15].strip()) for r in data}
    if len(releases) != 1 or len(fy) != 1:
        raise WasdeError(f"{file}: several release dates / forecast months {sorted(releases)[:3]} {sorted(fy)[:3]}")
    (fy_y, fy_m), release_text = fy.pop(), releases.pop()
    if (int(fy_y), int(fy_m)) != month:
        raise WasdeError(f"{file}: ForecastYear/Month {fy_y}-{fy_m} is not the requested report {vintage}")
    release = parse_release_date(release_text)
    avail, basis, lm = availability(release, last_modified)
    rep.release_date, rep.available_at, rep.basis = release.isoformat(), avail.isoformat() if avail else None, basis
    rep.wasde_number = data[0][0].strip()
    titles: Counter[str] = Counter()
    for n, r in enumerate(data, start=2):
        if len(r) != len(HEADER):
            raise WasdeError(f"{file}: line {n} has {len(r)} fields")
        rep.rows += 1
        num, rdate, title, attr, rel, comm, region, my, pe, aq, raw_value, unit = (c.strip() for c in r[:12])
        if rel:
            rep.reliability_rows += 1
            continue
        value = _decimal(raw_value)
        if value is None:
            if raw_value.strip():
                raise WasdeError(f"{file}: line {n}: value {raw_value[:30]!r} is not a number")
            rep.empty_values += 1
            continue
        per = _period(my, aq)
        if per is None:
            rep.no_period_rows += 1
            continue
        start, end, period_kind = per
        if pe not in PROJ and pe not in EST:
            raise WasdeError(f"{file}: line {n}: unexpected ProjEstFlag {pe!r}")
        kind = "projection" if pe in PROJ else "published_history"
        variable = f"usda_wasde:{slug(title)}.{slug(comm)}.{slug(attr)}" + ("" if aq == "Annual" else ".quarterly")
        indicator = f"{variable}|v={vintage}"
        if len(indicator) > 200:
            raise WasdeError(f"{file}: indicator too long ({len(indicator)}): {indicator[:80]}")
        flags = vintage_flags(
            family=FAMILY,
            vintage=vintage,
            available_at=avail,
            basis=basis,
            variable=variable,
            provider_variable=f"{title} | {comm} | {attr}",
            estimate_kind=kind,
            vintage_date_text=release_text,
            report_title=title,
            commodity=comm,
            attribute=attr,
            region=region,
            market_year=my,
            period_kind=period_kind,
            annual_quarter=aq,
            wasde_status=pe or None,
            report_date=rdate,
            wasde_number=num,
            file=file,
            file_last_modified=last_modified,
            geography="WASDE region name as published (not mapped to a country code)",
        )
        rows.add(
            source=ATLAS_SOURCE,
            dataset=DS,
            indicator=indicator,
            provider_geo=(region or "not_stated")[:64],
            start=start,
            end=end,
            value=value,
            unit=unit or "not stated by WASDE",
            flags=flags,
            raw_sha256=raw_sha256,
            selector={"file": file, "row": n},
        )
        rep.cells += 1
        rep.projections += kind == "projection"
        titles[title] += 1
    rep.titles = dict(titles)
    rep.last_modified_date = lm.isoformat() if lm else None
    return rep


def rights() -> dict[str, Any]:
    return {
        "licence": "Public domain (U.S. Government work, 17 U.S.C. 105)",
        "licence_url": "https://www.usda.gov/policies-and-links",
        "attribution": ATTRIBUTION,
        "provider": "USDA Office of the Chief Economist / World Agricultural Outlook Board",
        "licence_checked": "2026-10-17",
        "source_key": SOURCE_KEY,
    }


def code_digest() -> str:
    return module_digest(sys.modules[__name__])


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=NAME,
        version=VERSION,
        source_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="USDA, World Agricultural Supply and Demand Estimates (Office of the Chief Economist historical CSV)",
        licence=LICENCE,
        allowed_hosts=(HOST,),
        datasets=(DS,),
        dataset_ids=("oce-wasde-report-data",),
        cadence_seconds=30 * 86_400,
        planned_requests=100,
        max_response_bytes=MAX_FILE_BYTES,
        min_interval_seconds=2.0,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(),
        field_mappings={"vintage_family": FAMILY, "variable_prefix": "usda_wasde:", "geo": "WASDE Region text"},
        notes="One ~1 MB CSV per monthly WASDE report from 2021-01 (+ '-V2' corrected files); vintage table.",
    )


async def _fetch_month(run: Any, y: int, m: int) -> tuple[Any, str] | None:
    """The month's CSV (plain name, else ``-V2``) or ``None`` when neither exists."""
    for suffix in ("", "-V2"):
        name = f"{y}-{m:02d}{suffix}"
        try:
            raw = await run.get(
                FILE_URL.format(name=name),
                key=f"wasde:{name}",
                media_types=CSV_MEDIA,
                max_bytes=MAX_FILE_BYTES,
                params={"report": f"{y}-{m:02d}"},
            )
        except SourceResponseError as e:
            d = e.details or {}
            if d.get("status") == 404 or (run.origin == "test_fixture" and d.get("kind") == "fixture_missing"):
                continue
            raise
        return raw, f"oce-wasde-report-data-{name}.csv"
    return None


async def atlas_ingest(run: Any) -> dict[str, Any]:
    if not run.wants(DS):
        return {"tables": {}}
    now = datetime.now(UTC)
    first = _month_opt(run.options["first"], "first") if run.options.get("first") else FIRST_MONTH
    last = _month_opt(run.options["last"], "last") if run.options.get("last") else (now.year, now.month)
    rows = Rows()
    shas: list[str] = []
    per_file: list[dict[str, Any]] = []
    missing: list[str] = []
    for y, m in months(first, last):
        got = await _fetch_month(run, y, m)
        if got is None:
            missing.append(f"{y}-{m:02d}")
            continue
        raw, file = got
        sha = run.archive(raw)
        shas.append(sha)
        headers = {str(k).lower(): v for k, v in (raw.response_metadata.get("headers") or {}).items()}
        rep = parse_csv(
            raw.body, month=(y, m), file=file, raw_sha256=sha, last_modified=headers.get("last-modified"), rows=rows
        )
        del raw
        per_file.append({**rep.__dict__, "titles": len(rep.titles), "sha256": sha})
    if not shas:
        raise WasdeError(f"no WASDE CSV found for {first}..{last}", kind="no_data")
    frame = rows.frame()
    late = [f["month"] for f in per_file if f["basis"] == "wasde_csv_file_last_modified"]
    note = (
        f"USDA OCE Historical WASDE Report Data, one CSV per report ({FILE_URL.format(name='YYYY-MM')}): "
        f"{len(per_file)} reports {per_file[0]['month']}..{per_file[-1]['month']}; months without a file: {missing}. "
        "Vintage = report month; available_at = day after ReleaseDate (00:00 UTC) when Last-Modified is on/before "
        f"the release day, else the day after Last-Modified (files dated after release: {late}). Proj. -> "
        "projection; Est./unflagged -> published_history (label in flags.wasde_status). Reliability tables skipped. "
        "Region names verbatim, never mapped to countries. Period: marketing year as its first calendar year. "
        f"Coverage: {coverage_line(frame)}. Public domain; cite {ATTRIBUTION}."
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
        "files": per_file,
        "missing_months": missing,
    }


__all__ = [
    "NAME",
    "WasdeError",
    "atlas_ingest",
    "atlas_source",
    "availability",
    "months",
    "parse_csv",
    "parse_release_date",
]
