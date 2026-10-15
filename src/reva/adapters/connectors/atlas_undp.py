"""UNDP Human Development Report composite indices -> atlas tables (V3, agent A-QOL).

Source: UNDP Human Development Report Office (HDRO) "Documentation and downloads" page,
https://hdr.undp.org/data-center/documentation-and-downloads (checked 2026-10-15). Licence: every
material on hdr.undp.org is "copyrighted under the Creative Commons Attribution 3.0 IGO license"
(https://hdr.undp.org/terms-use, checked 2026-10-15).

Three bulk files (one request each, no API key):

* ``HDR25_Composite_indices_complete_time_series.csv`` — HDI, its components (life expectancy,
  expected/mean years of schooling, GNI per capita), GDI (+ female/male HDI and components), IHDI
  (+ coefficient of human inequality, loss, inequality in life expectancy/education/income), GII
  (+ maternal mortality, adolescent births, secondary education, parliament seats, labour force
  participation), PHDI (+ CO2 and material footprint per capita), population; **every year
  1990-2023** (IHDI 2010-2023) for 195 countries/territories + 11 UNDP aggregates (kept, unmapped).
  Encoding: Windows-1252 (``Côte d'Ivoire`` is byte 0xF4); decoded as such and recorded.
* ``HDR25_Composite_indices_metadata.xlsx`` — the codebook (full names with units, time-series
  span). Two variables carry a value forward: ``ineq_inc`` "2010-2021, 2022-2023=2022" and ``mmr``
  "1990-2019, 2020-2023=2020". Cells of the carried years are kept as the provider publishes them and
  flagged ``{"carried_forward_from": <year>}`` — they are **not** new observations.
* ``2026_gMPI_Table1and2.xlsx`` — global Multidimensional Poverty Index 2026 (UNDP HDRO + OPHI):
  Table 1 (latest survey per country: MPI, headcount, intensity, severity, vulnerability,
  dimension contributions, monetary poverty) and Table 2 (harmonised estimates over time with the
  ten indicator deprivations). Periods are the survey years (``2022/2023`` = 2022-01-01..2024-01-01);
  survey type letter (D = DHS, M = MICS, N = national, ...) and footnotes are kept in ``flags``.
  Monetary poverty columns refer to "the most recent year available during 2014-2025" without the
  year: their period is the whole window 2014-01-01..2026-01-01 and flags say so.
  MPI rows name countries (no codes): names map through the same provider's HDR25 CSV
  (``iso3``/``country`` pairs), then the crosswalk labels; residual names stay unmapped (listed).

Atlas tables: ``undp.hdr25`` (composite indices) and ``undp.gmpi2026`` (indicator prefix ``t1.`` /
``t2.``). Ranks/groups are kept: ranks numeric (unit ``rank``), HDI group / UNDP region / GDI group
as ``value_text``.
"""

from __future__ import annotations

import csv
import io
import re
import sys
from collections import Counter
from collections.abc import Sequence
from datetime import date
from typing import Any

from reva.adapters.connectors import atlas_qol_common as common
from reva.adapters.connectors.atlas_qol_common import AtlasBuild, NameIndex, canon, decimal_text
from reva.adapters.connectors.atlas_xlsx import Workbook
from reva.kernel.errors import SourceResponseError
from reva.ports.connectors import ConnectorSpec, FetchItem, RawAcquisition

CONNECTOR_NAME = "atlas.undp_hdr"
CONNECTOR_VERSION = "1.0.0"
PARSER_VERSION = "atlas_undp/1"
SOURCE_KEY = "undp.hdr"
SOURCE = "undp"
HOST = "hdr.undp.org"
LICENCE = "CC BY 3.0 IGO (https://hdr.undp.org/terms-use)"
ATTRIBUTION = "UNDP (United Nations Development Programme). 2025. Human Development Report 2025. New York."

COMPOSITE_FILE = "HDR25_Composite_indices_complete_time_series.csv"
METADATA_FILE = "HDR25_Composite_indices_metadata.xlsx"
MPI_FILE = "2026_gMPI_Table1and2.xlsx"
COMPOSITE_URL = f"https://{HOST}/sites/default/files/2025_HDR/{COMPOSITE_FILE}"
METADATA_URL = f"https://{HOST}/sites/default/files/2025_HDR/{METADATA_FILE}"
MPI_URL = f"https://{HOST}/sites/default/files/publications/additional-files/2026-10/{MPI_FILE}"

#: HDR25 codebook variables -> unit. Units are the codebook's own parentheses; the five IHDI
#: inequality measures (no unit in the codebook) are percentages per HDR25 Statistical Annex
#: Table 3 ("Overall loss (%)", "(%)" columns).
UNITS: dict[str, str] = {
    "hdi_rank": "rank",
    "hdi": "index (0-1)",
    "le": "years",
    "eys": "years",
    "mys": "years",
    "gnipc": "2021 PPP$",
    "gdi": "index (female HDI / male HDI)",
    "hdi_f": "index (0-1)",
    "le_f": "years",
    "eys_f": "years",
    "mys_f": "years",
    "gni_pc_f": "2021 PPP$",
    "hdi_m": "index (0-1)",
    "le_m": "years",
    "eys_m": "years",
    "mys_m": "years",
    "gni_pc_m": "2021 PPP$",
    "ihdi": "index (0-1)",
    "coef_ineq": "%",
    "loss": "%",
    "ineq_le": "%",
    "ineq_edu": "%",
    "ineq_inc": "%",
    "gii_rank": "rank",
    "gii": "index (0-1)",
    "mmr": "deaths per 100,000 live births",
    "abr": "births per 1,000 women ages 15-19",
    "se_f": "% of females ages 25 and older",
    "se_m": "% of males ages 25 and older",
    "pr_f": "% of seats held by women",
    "pr_m": "% of seats held by men",
    "lfpr_f": "% of females ages 15 and older",
    "lfpr_m": "% of males ages 15 and older",
    "rankdiff_hdi_phdi": "rank difference",
    "phdi": "index (0-1)",
    "diff_hdi_phdi": "%",
    "co2_prod": "tonnes per capita",
    "mf": "tonnes per capita",
    "pop_total": "millions",
}
TEXT_VARS = {"gdi_group": "GDI group (1-5)"}
STATIC_TEXT = {"hdicode": "HDI group", "region": "UNDP developing region"}
_COL = re.compile(r"^([a-z][a-z0-9_]*?)_(\d{4})$")
_SPAN = re.compile(r"(\d{4})-(\d{4})=(\d{4})")


class UndpParseError(SourceResponseError):
    def __init__(self, msg: str) -> None:
        super().__init__(msg, retryable=False, kind="parse_error")


def _decode_csv(body: bytes) -> tuple[str, str]:
    try:
        return body.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return body.decode("cp1252"), "cp1252"


# --------------------------------------------------------------------------- codebook
def parse_codebook(body: bytes) -> dict[str, dict[str, Any]]:
    """``{short_name: {"full_name", "time_series", "carried": {year: from_year}}}``."""
    wb = Workbook(body)
    if "codebook" not in wb.sheets:
        raise UndpParseError(f"{METADATA_FILE}: no 'codebook' sheet (has {sorted(wb.sheets)})")
    rows = wb.table("codebook")
    if not rows or [str(c or "").strip() for c in rows[0][:3]] != ["Full name", "Short name", "Time series"]:
        raise UndpParseError(f"{METADATA_FILE}: unexpected codebook header")
    out: dict[str, dict[str, Any]] = {}
    for r in rows[1:]:
        full, short, span = [*r, None, None, None][:3]
        if not short:
            continue
        carried: dict[int, int] = {}
        for a, b, src in _SPAN.findall(span or ""):
            for y in range(int(a), int(b) + 1):
                if y != int(src):
                    carried[y] = int(src)
        out[str(short).strip()] = {"full_name": str(full).strip(), "time_series": span, "carried": carried}
    return out


# --------------------------------------------------------------------------- composite CSV
def parse_composite(
    body: bytes, raw_sha: str, codebook: dict[str, dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, Any]]:
    """Rows of ``undp.hdr25``; also the provider's (country name -> iso3) table and a report."""
    text, encoding = _decode_csv(body)
    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        header = next(reader)
    except StopIteration as e:
        raise UndpParseError(f"{COMPOSITE_FILE}: empty") from e
    if header[:4] != ["iso3", "country", "hdicode", "region"]:
        raise UndpParseError(f"{COMPOSITE_FILE}: unexpected leading columns {header[:4]}")
    cols: list[tuple[int, str, int]] = []
    unknown: set[str] = set()
    for i, h in enumerate(header[4:], start=4):
        m = _COL.match(h)
        if not m:
            raise UndpParseError(f"{COMPOSITE_FILE}: column {h!r} is not <variable>_<year>")
        var, year = m.group(1), int(m.group(2))
        if var not in UNITS and var not in TEXT_VARS:
            unknown.add(var)
        cols.append((i, var, year))
    if unknown:
        # A new variable needs a declared unit first: fail closed rather than guess.
        raise UndpParseError(f"{COMPOSITE_FILE}: variables without a declared unit: {sorted(unknown)}")
    rows: list[dict[str, Any]] = []
    names: dict[str, str] = {}
    n_rows = 0
    carried_cells = 0
    for rnum, rec in enumerate(reader, start=2):
        if not rec or all(not c for c in rec):
            continue
        if len(rec) != len(header):
            raise UndpParseError(f"{COMPOSITE_FILE}: row {rnum} has {len(rec)} fields, header {len(header)}")
        n_rows += 1
        geo, cname = rec[0].strip(), rec[1].strip()
        if not geo:
            raise UndpParseError(f"{COMPOSITE_FILE}: row {rnum} without iso3")
        names[cname] = geo
        base = {"source": SOURCE, "dataset": "hdr25", "provider_geo": geo, "raw_sha256": raw_sha}
        for col, label in STATIC_TEXT.items():
            v = rec[header.index(col)].strip()
            if v:
                ps, pe = date(2023, 1, 1), date(2024, 1, 1)
                rows.append(
                    {
                        **base,
                        "indicator": col,
                        "period_start": ps,
                        "period_end": pe,
                        "value": None,
                        "value_text": v,
                        "unit": label,
                        "flags": canon(classification="HDR 2025 edition (HDI 2023)"),
                        "selector": canon(file=COMPOSITE_FILE, row=rnum, column=col),
                    }
                )
        for i, var, year in cols:
            cell = rec[i].strip()
            if cell == "":
                continue  # not reported: omitted (never a zero)
            flags: dict[str, Any] = {}
            cf = codebook.get(var, {}).get("carried", {}).get(year)
            if cf is not None:
                flags["carried_forward_from"] = cf
                flags["provider_note"] = f"HDR25 codebook time series: {codebook[var]['time_series']}"
                carried_cells += 1
            r = {
                **base,
                "indicator": var,
                "period_start": date(year, 1, 1),
                "period_end": date(year + 1, 1, 1),
                "flags": canon(flags),
                "selector": canon(file=COMPOSITE_FILE, row=rnum, column=header[i]),
            }
            if var in TEXT_VARS:
                r.update(value=None, value_text=cell, unit=TEXT_VARS[var])
            else:
                d = decimal_text(cell)
                if d is None:
                    raise UndpParseError(f"{COMPOSITE_FILE}: row {rnum} column {header[i]}: not a number")
                r.update(value=d, value_text=None, unit=UNITS[var])
            rows.append(r)
    report = {
        "encoding": encoding,
        "geographies": n_rows,
        "columns": len(header),
        "variables": sorted({v for _, v, _ in cols}),
        "years": [min(y for *_, y in cols), max(y for *_, y in cols)],
        "carried_forward_cells": carried_cells,
    }
    return rows, names, report


# --------------------------------------------------------------------------- gMPI tables
_SURVEY = re.compile(r"^(\d{4})(?:/(\d{4}))?\s+([A-Za-z]+)$")
_NOTE = re.compile(r"^([a-z])\.\s+(.*)$")

#: (column, indicator, unit) of Table 1; each value column is followed by its footnote column.
T1_COLS: tuple[tuple[int, str, str], ...] = (
    (3, "t1.mpi", "index (0-1)"),
    (5, "t1.headcount_pct", "%"),
    (7, "t1.headcount_thousands_survey_year", "thousands"),
    (9, "t1.headcount_thousands_2024", "thousands"),
    (11, "t1.intensity_pct", "%"),
    (13, "t1.inequality_among_poor", "variance (0-1)"),
    (15, "t1.severe_pct", "%"),
    (17, "t1.vulnerable_pct", "%"),
    (19, "t1.contribution_health_pct", "%"),
    (21, "t1.contribution_education_pct", "%"),
    (23, "t1.contribution_living_standards_pct", "%"),
    (25, "t1.below_national_poverty_line_pct", "%"),
    (27, "t1.below_ppp_3_usd_day_pct", "%"),
)
T1_HEADER = {
    (6, 1): "Multidimensional Poverty Index",
    (6, 5): "Headcount",
    (6, 11): "Intensity of deprivation",
    (6, 13): "Inequality among the poor",
    (6, 15): "Population in severe multidimensional poverty",
    (6, 17): "Population vulnerable to multidimensional poverty",
    (6, 19): "Health",
    (6, 21): "Education",
    (6, 23): "Standard of living",
    (6, 25): "National poverty line",
    (6, 27): "PPP $3.00 a day",
    (8, 0): "Country",
    (8, 7): "In survey year",
    (8, 9): "2024",
}
T2_COLS: tuple[tuple[int, str, str], ...] = (
    (4, "t2.mpi", "index (0-1)"),
    (6, "t2.headcount_pct", "%"),
    (8, "t2.headcount_thousands_survey_year", "thousands"),
    (10, "t2.intensity_pct", "%"),
    (12, "t2.deprived_nutrition_pct", "%"),
    (14, "t2.deprived_child_mortality_pct", "%"),
    (16, "t2.deprived_years_of_schooling_pct", "%"),
    (18, "t2.deprived_school_attendance_pct", "%"),
    (20, "t2.deprived_cooking_fuel_pct", "%"),
    (22, "t2.deprived_sanitation_pct", "%"),
    (24, "t2.deprived_drinking_water_pct", "%"),
    (26, "t2.deprived_electricity_pct", "%"),
    (28, "t2.deprived_housing_pct", "%"),
    (30, "t2.deprived_assets_pct", "%"),
)
T2_HEADER = {
    (4, 2): "Multidimensional Poverty Index",
    (4, 10): "Intensity of deprivation",
    (4, 12): "Nutrition",
    (4, 14): "Child mortality",
    (4, 16): "Years of schooling",
    (4, 18): "School attendance",
    (4, 20): "Cooking fuel",
    (4, 22): "Sanitation",
    (4, 24): "Drinking water",
    (4, 26): "Electricity",
    (4, 28): "Housing",
    (4, 30): "Assets",
    (5, 0): "Country",
    (5, 2): "Year and survey",
    (5, 8): "In survey year",
}
MONETARY = {"t1.below_national_poverty_line_pct", "t1.below_ppp_3_usd_day_pct"}


def _check_header(table: list[list[Any]], expect: dict[tuple[int, int], str], sheet: str) -> None:
    for (r, c), want in expect.items():
        got = table[r - 1][c] if r - 1 < len(table) and c < len(table[r - 1]) else None
        if got is None or str(got).strip().rstrip("\xa0").strip() != want:
            raise UndpParseError(f"{MPI_FILE} {sheet}: header cell r{r}c{c} is {got!r}, expected {want!r}")


def _notes(table: list[list[Any]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for r in table:
        v = r[0] if r else None
        if isinstance(v, str):
            m = _NOTE.match(v.strip())
            if m:
                out.setdefault(m.group(1), m.group(2).replace("\xa0", " ").strip())
    return out


def _letters(v: Any) -> list[str]:
    if v is None:
        return []
    return [x.strip() for x in str(v).split(",") if x.strip()]


def parse_mpi(body: bytes, raw_sha: str, names: NameIndex) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    wb = Workbook(body)
    sheets = {s.lower(): s for s in wb.sheets}
    t1 = next((s for k, s in sheets.items() if k.endswith("table1")), None)
    t2 = next((s for k, s in sheets.items() if k.endswith("table2")), None)
    if t1 is None or t2 is None:
        raise UndpParseError(f"{MPI_FILE}: expected Table1 and Table2 sheets, found {sorted(wb.sheets)}")
    rows: list[dict[str, Any]] = []
    report: dict[str, Any] = {"unparsed_surveys": [], "aggregates_skipped": [], "name_mapping": Counter()}
    for sheet, cols, header, name_note_col, survey_col, survey_note_col in (
        (t1, T1_COLS, T1_HEADER, None, 1, 2),
        (t2, T2_COLS, T2_HEADER, 1, 2, 3),
    ):
        table = wb.table(sheet)
        _check_header(table, header, sheet)
        notes = _notes(table)
        section = None
        first_data = max(r for r, _ in header)
        for idx in range(first_data, len(table)):
            rec = table[idx] + [None] * 40
            rnum = idx + 1
            name = rec[0]
            if not isinstance(name, str) or not name.strip():
                continue
            name = name.strip()
            if name.lower().startswith(("notes", "footnotes", "definitions", "sources", "main data sources")):
                break
            survey = rec[survey_col]
            if survey is None and all(rec[c] is None for c, *_ in cols):
                section = name  # section heading ("Estimates based on surveys for 2020-2025")
                continue
            if str(survey or "").strip() in ("\u2014", "-", "\u2013"):
                # aggregates (developing countries, regions): no survey, no period -> not an atlas cell
                report["aggregates_skipped"].append(name)
                continue
            m = _SURVEY.match(str(survey or "").strip())
            if not m:
                report["unparsed_surveys"].append({"sheet": sheet, "row": rnum, "survey": survey})
                continue
            y0 = int(m.group(1))
            y1 = int(m.group(2)) if m.group(2) else y0
            iso3, how = names.lookup(name)
            report["name_mapping"][how] += 1
            row_notes = _letters(rec[survey_note_col]) + (_letters(rec[name_note_col]) if name_note_col else [])
            for c, ind, unit in cols:
                cell = rec[c]
                if cell is None or str(cell).strip() in ("..", ""):
                    continue  # not reported: omitted
                d = decimal_text(str(cell))
                if d is None:
                    raise UndpParseError(f"{MPI_FILE} {sheet} r{rnum}c{c}: {cell!r} is not a number")
                letters = row_notes + _letters(rec[c + 1])
                flags: dict[str, Any] = {"survey": str(survey).strip(), "survey_type": m.group(3)}
                if section:
                    flags["section"] = section
                if letters:
                    flags["footnotes"] = {k: notes.get(k, "(footnote text not found)") for k in sorted(set(letters))}
                if iso3 is not None:
                    flags["iso3_via"] = how
                if ind in MONETARY:
                    ps, pe = date(2014, 1, 1), date(2026, 1, 1)
                    flags["period_note"] = "most recent year available in 2014-2025; the year is not given"
                elif ind.endswith("_2024"):
                    ps, pe = date(2024, 1, 1), date(2025, 1, 1)
                    flags["period_note"] = "survey headcount rate applied to 2024 population (provider)"
                else:
                    ps, pe = date(y0, 1, 1), date(y1 + 1, 1, 1)
                rows.append(
                    {
                        "source": SOURCE,
                        "dataset": "gmpi2026",
                        "indicator": ind,
                        "iso3": iso3,
                        "provider_geo": name,
                        "period_start": ps,
                        "period_end": pe,
                        "value": d,
                        "value_text": None,
                        "unit": unit,
                        "flags": canon(flags),
                        "raw_sha256": raw_sha,
                        "selector": canon(file=MPI_FILE, sheet=sheet, row=rnum, column=c),
                    }
                )
    report["name_mapping"] = dict(report["name_mapping"])
    return rows, report


#: MPI country names that differ from HDR25 CSV names and crosswalk labels (provider spelling ->
#: crosswalk key). Filled from the live 2026 file; every entry is an exact provider string.
MPI_NAME_ALIASES: dict[str, str | None] = {}


# --------------------------------------------------------------------------- connector
class UndpHdrConnector:
    def describe(self) -> ConnectorSpec:
        return common.atlas_spec(
            name=CONNECTOR_NAME,
            version=CONNECTOR_VERSION,
            source_key=SOURCE_KEY,
            provider="UNDP Human Development Report Office",
            licence=LICENCE,
            allowed_hosts=(HOST,),
            dataset_ids=("hdr25_composite", "hdr25_metadata", "gmpi2026_tables_1_2"),
            field_mappings={"atlas_tables": ["undp.hdr25", "undp.gmpi2026"]},
            units=dict(UNITS),
            geography=("all",),
            time_semantics="calendar years (composite); survey years (MPI)",
            lineage_family="undp_hdro",
            cadence_seconds=30 * common.DAY,
            release_aware=True,
            revision_strategy="new HDR edition = new files; same content re-registers as a no-op",
            max_response_bytes=20_000_000,
            max_pages=1,
            parser_version=PARSER_VERSION,
            code_digest=common.code_digest(sys.modules[__name__]),
        )

    def fetch_items(self) -> tuple[FetchItem, ...]:
        csv_t = ("text/csv", "application/octet-stream")
        xl_t = ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "application/octet-stream")
        return (
            FetchItem(key="composite", url=COMPOSITE_URL, expected_media_types=csv_t, max_bytes=10_000_000),
            FetchItem(key="metadata", url=METADATA_URL, expected_media_types=xl_t, max_bytes=2_000_000),
            FetchItem(key="gmpi", url=MPI_URL, expected_media_types=xl_t, max_bytes=5_000_000),
        )

    def follow_up(self, raw: RawAcquisition) -> tuple[FetchItem, ...]:
        return ()

    async def fetch(self, item: FetchItem, http: Any) -> RawAcquisition:
        return await common.fetch_plain(item, http)

    def build(self, raws: Sequence[RawAcquisition]) -> list[AtlasBuild]:
        by = {r.item.key: r for r in raws}
        missing = {"composite", "metadata", "gmpi"} - set(by)
        if missing:
            raise UndpParseError(f"missing raw items {sorted(missing)}")
        sha = {k: common.sha256_hex(r.body) for k, r in by.items()}
        codebook = parse_codebook(by["metadata"].body)
        rows, names, rep = parse_composite(by["composite"].body, sha["composite"], codebook)
        rights = {"licence": LICENCE, "attribution": ATTRIBUTION, "licence_checked": "2026-10-15"}
        builds = [
            AtlasBuild(
                source=SOURCE,
                dataset="hdr25",
                frame=common.frame(rows),
                raw_sha256s=[sha["composite"]],
                parser_version=PARSER_VERSION,
                coverage_note=(
                    "UNDP HDR 2025 composite indices complete time series 1990-2023 (HDI, GDI, IHDI, GII, "
                    "PHDI + components); 11 UNDP aggregates (ZZ*) kept unmapped. ineq_inc 2022-2023 and "
                    "mmr 2020-2023 repeat an earlier year per the HDR25 codebook (flag carried_forward_from). "
                    f"Codebook {METADATA_FILE} sha256 {sha['metadata']}."
                ),
                geo_scheme="iso3",
                rights=rights,
                report=rep,
            )
        ]
        mpi_rows, mrep = parse_mpi(by["gmpi"].body, sha["gmpi"], NameIndex(MPI_NAME_ALIASES, extra=names))
        builds.append(
            AtlasBuild(
                source=SOURCE,
                dataset="gmpi2026",
                frame=common.frame(mpi_rows),
                raw_sha256s=[sha["gmpi"]],
                parser_version=PARSER_VERSION,
                coverage_note=(
                    "Global MPI 2026 (UNDP HDRO & OPHI) Table 1 (t1.*, latest survey) and Table 2 (t2.*, "
                    "harmonised estimates over time). Countries are named, mapped via HDR25 names then "
                    "crosswalk labels. Periods are survey years; monetary poverty = most recent year in "
                    "2014-2025 (window period)."
                ),
                geo_scheme=None,
                rights={
                    **rights,
                    "attribution": "UNDP and OPHI (2026). Global Multidimensional Poverty Index 2026.",
                },
                report=mrep,
            )
        )
        return builds


__all__ = [
    "CONNECTOR_NAME",
    "UndpHdrConnector",
    "parse_codebook",
    "parse_composite",
    "parse_mpi",
]
