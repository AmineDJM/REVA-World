"""Philadelphia Fed real-time data and Survey of Professional Forecasters -> vintage atlas tables
(V5, agent PRED-VINTAGE). Conventions in :mod:`.vintage_common`.

Source ``atlas.vintage_philfed`` (host ``www.philadelphiafed.org``, open downloads, no key):

``vintage_us.rtdsm`` — Real-Time Data Set for Macroeconomists (RTDSM), quarterly vintages:
    ``ROUTPUTQvQd.xlsx`` (real GNP/GDP, quarterly), ``pQvQd.xlsx`` (GNP/GDP price index, quarterly),
    ``rucQvMd.xlsx`` (civilian unemployment rate, monthly observations), ``cpiQvMd.xlsx`` (CPI, monthly
    observations). One column per vintage (``ROUTPUT08Q1``), one row per observation date
    (``1947:Q1`` / ``1947:01``); ``#N/A`` = not in that vintage (no cell). The documentation
    (``gen_doc_NIPA.pdf``, archived with the table) states: "the observations represent the data as they
    were known in the middle month of each quarter (February, May, August, and November)". Hence
    ``available_at`` = first instant of the month after the middle month (rule ``rtdsm_middle_month_end``).
    Every cell is ``published_history``.

``vintage_us.spf_median`` — SPF median forecasts per survey (``medianLevel.xlsx``: levels / rates for all
    quarterly and annual horizons; ``medianGrowth.xlsx``: annualised quarter-on-quarter growth), with the
    survey's true **News Release Date** from ``spf-release-dates.txt`` (verbatim, ``8/31/90*``):
    ``available_at`` = the day after the release date, 00:00 UTC (rule ``spf_news_release_date``).
    Surveys before 1990:Q2 have no known release date ("True deadline and news release dates for surveys
    prior to 1990:Q2 are not known"): their cells carry ``available_at = null`` and are never selected as
    of any date. Horizons (SPF documentation, quoted in the report): suffix ``1`` = the quarter before the
    survey quarter (known to forecasters; ``flags.jump_off``), ``2`` = the survey quarter, ``3``-``6`` = the
    next four quarters, ``A``/``B``/``C``/``D`` = the survey year and the next three years (annual average
    for levels and rates; fourth quarter over fourth quarter for CPI/core CPI/PCE/core PCE inflation). All
    SPF cells are ``projection`` (survey expectations), never observations.
"""

from __future__ import annotations

import hashlib
import inspect
import re
import sys
from datetime import date, datetime
from typing import Any

import polars as pl

from reva.adapters.connectors.atlas_xlsx import Workbook, XlsxError
from reva.adapters.connectors.vintage_common import (
    Rows,
    coverage_line,
    day_after,
    decimal_or_none,
    first_of_next_month,
    indicator,
    month_bounds,
    quarter_bounds,
    vintage_flags,
    year_bounds,
)
from reva.atlas.sources import AtlasSourceSpec
from reva.kernel.errors import SourceResponseError

NAME = "atlas.vintage_philfed"
VERSION = "1.0.0"
PARSER_VERSION = "atlas_vintage_philfed/1"
SOURCE_KEY = "philfed.data"
ATLAS_SOURCE = "vintage_us"
DS_RTDSM = "rtdsm"
DS_SPF = "spf_median"
HOST = "www.philadelphiafed.org"
BASE = f"https://{HOST}/-/media/frbp/assets/surveys-and-data"
RTDSM_DOC_URL = (
    f"https://{HOST}/-/media/FRBP/Assets/Surveys-And-Data/real-time-data/data-files/documentation/gen_doc_NIPA.pdf"
)
SPF_DOC_URL = f"{BASE}/survey-of-professional-forecasters/spf-documentation.pdf"
SPF_DATES_URL = f"{BASE}/survey-of-professional-forecasters/spf-release-dates.txt"
SPF_FILES = {"level": "medianLevel.xlsx", "growth": "medianGrowth.xlsx"}
XLSX_MEDIA = ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "application/octet-stream")
MAX_BYTES = 5_000_000
GEO = "USA"
TERMS_URL = "https://www.philadelphiafed.org/privacy-and-terms-of-use"

#: RTDSM file -> (column root, REVA variable, unit as documented, observation frequency).
RTDSM_FILES: dict[str, tuple[str, str, str, str]] = {
    "ROUTPUTQvQd.xlsx": ("ROUTPUT", "real_gdp_level", "billions of real dollars, seasonally adjusted", "Q"),
    "pQvQd.xlsx": ("P", "gdp_price_index", "index (GNP/GDP price index), seasonally adjusted", "Q"),
    "rucQvMd.xlsx": ("RUC", "unemployment_rate", "percent (civilian unemployment rate), seasonally adjusted", "M"),
    "cpiQvMd.xlsx": ("CPI", "cpi_level", "index (consumer price index), seasonally adjusted", "M"),
}
#: SPF medianLevel sheet -> (REVA variable for quarterly horizons, for annual horizons, unit).
SPF_LEVEL: dict[str, tuple[str, str, str]] = {
    "NGDP": ("nominal_gdp_level", "nominal_gdp_level", "billions of dollars, SAAR (base year of the survey)"),
    "PGDP": ("gdp_price_index", "gdp_price_index", "index (GDP price index, base year of the survey)"),
    "RGDP": ("real_gdp_level", "real_gdp_level", "billions of real dollars, SAAR (base year of the survey)"),
    "UNEMP": ("unemployment_rate", "unemployment_rate", "percent"),
    "CPI": ("cpi_inflation_q_ann", "cpi_inflation_q4q4", "percent (annualised Q/Q; annual: Q4/Q4)"),
    "CORECPI": ("core_cpi_inflation_q_ann", "core_cpi_inflation_q4q4", "percent (annualised Q/Q; annual: Q4/Q4)"),
    "PCE": ("pce_inflation_q_ann", "pce_inflation_q4q4", "percent (annualised Q/Q; annual: Q4/Q4)"),
    "COREPCE": ("core_pce_inflation_q_ann", "core_pce_inflation_q4q4", "percent (annualised Q/Q; annual: Q4/Q4)"),
    "INDPROD": ("industrial_production_index", "industrial_production_index", "index"),
    "HOUSING": ("housing_starts", "housing_starts", "millions of units, annual rate"),
    "TBILL": ("tbill_3m_rate", "tbill_3m_rate", "percent"),
    "TBOND": ("tbond_10y_rate", "tbond_10y_rate", "percent"),
    "EMP": ("nonfarm_payroll_employment", "nonfarm_payroll_employment", "thousands"),
}
#: SPF medianGrowth sheet -> (REVA variable, column prefix).
SPF_GROWTH: dict[str, tuple[str, str]] = {
    "RGDP": ("real_gdp_growth_q_ann", "drgdp"),
    "NGDP": ("nominal_gdp_growth_q_ann", "dngdp"),
    "PGDP": ("gdp_price_inflation_q_ann", "dpgdp"),
}
FIXTURE_RTDSM = ("ROUTPUTQvQd.xlsx", "rucQvMd.xlsx")

_VCOL = re.compile(r"^([A-Za-z]+)(\d{2})Q([1-4])$")
_QDATE = re.compile(r"^(\d{4}):Q([1-4])$")
_MDATE = re.compile(r"^(\d{4}):(0[1-9]|1[0-2])$")
_DATE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{2})")
_SPF_HEAD = re.compile(r"^([A-Z]+)([1-6A-D])$")


class PhilFedError(SourceResponseError):
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
        provider="Federal Reserve Bank of Philadelphia (Real-Time Data Set for Macroeconomists; Survey of "
        "Professional Forecasters)",
        licence="Federal Reserve Bank of Philadelphia public data downloads; no explicit open licence (site terms: "
        + TERMS_URL
        + "); cite the source",
        allowed_hosts=(HOST,),
        datasets=(DS_RTDSM, DS_SPF),
        dataset_ids=tuple(RTDSM_FILES) + tuple(SPF_FILES.values()),
        cadence_seconds=30 * 86_400,
        planned_requests=len(RTDSM_FILES) + len(SPF_FILES) + 3,
        max_response_bytes=MAX_BYTES,
        min_interval_seconds=2.0,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(),
        field_mappings={
            "atlas_tables": [f"{ATLAS_SOURCE}.{DS_RTDSM}", f"{ATLAS_SOURCE}.{DS_SPF}"],
            "geo_scheme": "iso3",
        },
        notes="RTDSM quarterly-vintage workbooks + SPF median workbooks + SPF release dates + documentation PDFs",
    )


def rights() -> dict[str, Any]:
    return {
        "licence": "Federal Reserve Bank of Philadelphia public data (no explicit open licence)",
        "terms_url": TERMS_URL,
        "attribution": "Federal Reserve Bank of Philadelphia, Real-Time Data Set for Macroeconomists / Survey of "
        "Professional Forecasters",
        "open_licence": None,
        "restriction": "no explicit licence statement on the data pages; redistribution terms not verified",
        "licence_checked": "2026-10-17",
    }


# --------------------------------------------------------------------------- RTDSM
def _year2(yy: str) -> int:
    n = int(yy)
    return 1900 + n if n >= 60 else 2000 + n


def rtdsm_vintage(col: str, root: str) -> tuple[int, int] | None:
    m = _VCOL.match(col)
    if not m or m.group(1).upper() != root.upper():
        return None
    return _year2(m.group(2)), int(m.group(3))


def rtdsm_available_at(year: int, quarter: int) -> datetime:
    middle = 3 * (quarter - 1) + 2  # Feb, May, Aug, Nov
    return first_of_next_month(year, middle)


def parse_rtdsm(body: bytes, file: str, raw_sha: str, rows: Rows, stats: dict[str, int]) -> None:
    root, variable, unit, freq = RTDSM_FILES[file]
    try:
        wb = Workbook(body)
    except XlsxError as e:
        raise PhilFedError(f"{file}: {e}") from e
    if len(wb.sheets) != 1:
        raise PhilFedError(f"{file}: expected one sheet, got {sorted(wb.sheets)}")
    sheet = next(iter(wb.sheets))
    header: dict[int, tuple[str, int, int]] = {}
    for rnum, cells in wb.rows(sheet):
        if not header:
            if cells.get(0) is None or cells[0].text.strip().upper() != "DATE":
                raise PhilFedError(f"{file}: first header cell is not DATE")
            for ci, c in cells.items():
                if ci == 0:
                    continue
                v = rtdsm_vintage(c.text.strip(), root)
                if v is None:
                    raise PhilFedError(f"{file}: unexpected column header {c.text!r}")
                header[ci] = (c.text.strip(), *v)
            continue
        d = cells.get(0)
        if d is None:
            continue
        dt = d.text.strip()
        if freq == "Q":
            m = _QDATE.match(dt)
            if not m:
                raise PhilFedError(f"{file} row {rnum}: date {dt!r} is not YYYY:Qn")
            start, end = quarter_bounds(int(m.group(1)), int(m.group(2)))
        else:
            m = _MDATE.match(dt)
            if not m:
                raise PhilFedError(f"{file} row {rnum}: date {dt!r} is not YYYY:MM")
            start, end = month_bounds(int(m.group(1)), int(m.group(2)))
        for ci, c in cells.items():
            if ci == 0:
                continue
            if ci not in header:
                raise PhilFedError(f"{file} row {rnum}: value outside the header columns")
            col, vy, vq = header[ci]
            val = decimal_or_none(c.text) if c.kind == "n" else None
            if val is None:
                if c.kind in ("s", "e") and c.text.strip() == "#N/A":
                    stats["na"] = stats.get("na", 0) + 1
                    continue
                raise PhilFedError(f"{file} {c.ref}: unexpected cell {c.kind}:{c.text[:40]!r}")
            vintage = f"{vy}Q{vq}"
            rows.add(
                source=ATLAS_SOURCE,
                dataset=DS_RTDSM,
                indicator=indicator(root, vintage),
                provider_geo=GEO,
                iso3=GEO,
                start=start,
                end=end,
                value=val,
                unit=unit,
                flags=vintage_flags(
                    family="philfed_rtdsm",
                    vintage=vintage,
                    available_at=rtdsm_available_at(vy, vq),
                    basis="rtdsm_middle_month_end",
                    variable=variable,
                    provider_variable=root,
                    estimate_kind="published_history",
                    column=col,
                    file=file,
                ),
                raw_sha256=raw_sha,
                selector={"file": file, "sheet": sheet, "cell": c.ref, "column": col, "date": dt},
            )
            stats["cells"] = stats.get("cells", 0) + 1


# --------------------------------------------------------------------------- SPF
def parse_release_dates(body: bytes) -> dict[tuple[int, int], dict[str, str]]:
    """``{(year, quarter): {"deadline": text, "release": text}}`` from spf-release-dates.txt (verbatim)."""
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        text = body.decode("latin-1")
    if "News Release Date" not in text:
        raise PhilFedError("spf-release-dates.txt: no 'News Release Date' header")
    out: dict[tuple[int, int], dict[str, str]] = {}
    year: int | None = None
    for line in text.splitlines():
        m = re.match(r"^\s*(?:(\d{4})\s+)?Q([1-4])\s+(\S+)\s+(\S+)\s*$", line)
        if not m:
            continue
        if m.group(1):
            year = int(m.group(1))
        if year is None:
            raise PhilFedError(f"release dates: quarter line before any year: {line!r}")
        out[(year, int(m.group(2)))] = {"deadline": m.group(3), "release": m.group(4)}
    if len(out) < 100:
        raise PhilFedError(f"release dates: only {len(out)} surveys parsed")
    return out


def release_day(text: str) -> date:
    m = _DATE.search(text)
    if not m:
        raise PhilFedError(f"release date {text!r} is not m/d/yy")
    return date(_year2(m.group(3)), int(m.group(1)), int(m.group(2)))


def _num(cell: Any) -> int | None:
    if cell is None or cell.kind != "n":
        return None
    try:
        f = float(cell.text)
    except ValueError:
        return None
    return int(f) if f == int(f) else None


def horizon_period(sy: int, sq: int, h: str) -> tuple[date, date, str]:
    """(start, end, kind) of an SPF horizon suffix for a survey (year, quarter)."""
    if h in "ABCD":
        return (*year_bounds(sy + "ABCD".index(h)), "annual")
    k = int(h) - 2  # 1 = previous quarter, 2 = survey quarter
    idx = sy * 4 + (sq - 1) + k
    return (*quarter_bounds(idx // 4, idx % 4 + 1), "quarter")


def parse_spf(
    body: bytes,
    kind: str,
    raw_sha: str,
    dates: dict[tuple[int, int], dict[str, str]],
    rows: Rows,
    stats: dict[str, int],
) -> None:
    try:
        wb = Workbook(body)
    except XlsxError as e:
        raise PhilFedError(f"SPF {kind}: {e}") from e
    sheets = SPF_LEVEL if kind == "level" else SPF_GROWTH
    file = SPF_FILES[kind]
    for sheet in sheets:
        if sheet not in wb.sheets:
            raise PhilFedError(f"{file}: sheet {sheet} missing")
        header: dict[int, tuple[str, str]] = {}
        for rnum, cells in wb.rows(sheet):
            if not header:
                if [cells.get(0) and cells[0].text, cells.get(1) and cells[1].text] != ["YEAR", "QUARTER"]:
                    raise PhilFedError(f"{file}/{sheet}: header must start YEAR, QUARTER")
                for ci, c in cells.items():
                    if ci < 2:
                        continue
                    name = c.text.strip()
                    if kind == "level":
                        m = _SPF_HEAD.match(name.upper())
                        if not m or m.group(1) != sheet:
                            raise PhilFedError(f"{file}/{sheet}: unexpected header {name!r}")
                        header[ci] = (name, m.group(2))
                    else:
                        prefix = SPF_GROWTH[sheet][1]
                        if not re.fullmatch(prefix + r"[2-6]", name.lower()):
                            raise PhilFedError(f"{file}/{sheet}: unexpected header {name!r}")
                        header[ci] = (name, name[-1])
                continue
            sy, sq = _num(cells.get(0)), _num(cells.get(1))
            if sy is None or sq is None or not 1 <= sq <= 4:
                raise PhilFedError(f"{file}/{sheet} row {rnum}: bad YEAR/QUARTER")
            rd = dates.get((sy, sq))
            avail = day_after(release_day(rd["release"])) if rd else None
            vintage = f"{sy}Q{sq}"
            for ci, c in cells.items():
                if ci < 2:
                    continue
                if ci not in header:
                    raise PhilFedError(f"{file}/{sheet} {c.ref}: value outside header")
                col, h = header[ci]
                val = decimal_or_none(c.text) if c.kind == "n" else None
                if val is None:
                    if c.text.strip() == "#N/A":
                        stats["na"] = stats.get("na", 0) + 1
                        continue
                    raise PhilFedError(f"{file}/{sheet} {c.ref}: unexpected cell {c.kind}:{c.text[:40]!r}")
                start, end, pk = horizon_period(sy, sq, h)
                if kind == "level":
                    qv, av, unit = SPF_LEVEL[sheet]
                    variable = av if pk == "annual" else qv
                else:
                    variable, unit = SPF_GROWTH[sheet][0], "percent (annualised Q/Q growth)"
                rows.add(
                    source=ATLAS_SOURCE,
                    dataset=DS_SPF,
                    indicator=indicator(col, vintage),
                    provider_geo=GEO,
                    iso3=GEO,
                    start=start,
                    end=end,
                    value=val,
                    unit=unit,
                    flags=vintage_flags(
                        family="philfed_spf",
                        vintage=vintage,
                        vintage_date_text=rd["release"] if rd else None,
                        available_at=avail,
                        basis="spf_news_release_date" if rd else None,
                        variable=variable,
                        provider_variable=sheet if kind == "level" else SPF_GROWTH[sheet][1],
                        estimate_kind="projection",
                        horizon=h,
                        jump_off=True if h == "1" else None,
                        statistic="median",
                        deadline_text=rd["deadline"] if rd else None,
                        file=file,
                    ),
                    raw_sha256=raw_sha,
                    selector={"file": file, "sheet": sheet, "cell": c.ref, "column": col},
                )
                stats["cells"] = stats.get("cells", 0) + 1
                if avail is None:
                    stats["no_release_date"] = stats.get("no_release_date", 0) + 1


# --------------------------------------------------------------------------- ingest
def _note(ds: str, frame: pl.DataFrame, stats: dict[str, Any]) -> str:
    head = (
        "Philadelphia Fed RTDSM quarterly vintages (ROUTPUT, P, RUC, CPI); 'the observations represent the data "
        "as they were known in the middle month of each quarter (February, May, August, and November)'; "
        "available_at = first instant of the following month."
        if ds == DS_RTDSM
        else "Philadelphia Fed SPF median forecasts (medianLevel/medianGrowth) per survey; available_at = day after "
        "the News Release Date of spf-release-dates.txt; surveys before 1990:Q2 have no known release date "
        "(available_at null). All cells are survey projections."
    )
    return f"{head} Coverage: {coverage_line(frame)}. Parse: {stats}."[:4000]


async def atlas_ingest(run: Any) -> dict[str, Any]:
    fixture = run.origin == "test_fixture"
    out: dict[str, Any] = {"tables": {}, "failures": [], "requests": 0}

    async def get(url: str, key: str, media: tuple[str, ...]) -> tuple[bytes, str]:
        raw = await run.get(url, key=key, media_types=media, max_bytes=MAX_BYTES)
        out["requests"] += 1
        return raw.body, run.archive(raw)

    if run.wants(DS_RTDSM):
        rows, stats, raws = Rows(), dict[str, int](), []
        if not fixture:  # the documentation PDF is archived as the evidence of the availability rule
            _, doc_sha = await get(RTDSM_DOC_URL, "rtdsm:doc", ("application/pdf",))
            raws.append(doc_sha)
        for file in FIXTURE_RTDSM if fixture else tuple(RTDSM_FILES):
            body, sha = await get(f"{BASE}/real-time-data/data-files/xlsx/{file}", f"rtdsm:{file}", XLSX_MEDIA)
            raws.append(sha)
            parse_rtdsm(body, file, sha, rows, stats)
        frame = rows.frame()
        reg = run.register(
            dataset=DS_RTDSM,
            frame=frame,
            raw_sha256s=raws,
            coverage_note=_note(DS_RTDSM, frame, stats),
            geo_scheme="iso3",
            rights=rights(),
        )
        out["tables"][DS_RTDSM] = {
            "dataset_key": reg["dataset_key"],
            "rows": reg["row_count"],
            "created": reg["created"],
            "coverage": coverage_line(frame),
            "stats": stats,
        }
    if run.wants(DS_SPF):
        rows, stats, raws = Rows(), dict[str, int](), []
        if not fixture:
            _, doc_sha = await get(SPF_DOC_URL, "spf:doc", ("application/pdf",))
            raws.append(doc_sha)
        dbody, dsha = await get(SPF_DATES_URL, "spf:release_dates", ("text/plain",))
        raws.append(dsha)
        dates = parse_release_dates(dbody)
        for kind, file in SPF_FILES.items():
            body, sha = await get(
                f"{BASE}/survey-of-professional-forecasters/historical-data/{file.lower()}", f"spf:{file}", XLSX_MEDIA
            )
            raws.append(sha)
            parse_spf(body, kind, sha, dates, rows, stats)
        frame = rows.frame()
        reg = run.register(
            dataset=DS_SPF,
            frame=frame,
            raw_sha256s=raws,
            coverage_note=_note(DS_SPF, frame, stats),
            geo_scheme="iso3",
            rights=rights(),
        )
        out["tables"][DS_SPF] = {
            "dataset_key": reg["dataset_key"],
            "rows": reg["row_count"],
            "created": reg["created"],
            "coverage": coverage_line(frame),
            "stats": stats,
        }
    return out


__all__ = ["NAME", "atlas_ingest", "atlas_source", "parse_release_dates", "parse_rtdsm", "parse_spf"]
