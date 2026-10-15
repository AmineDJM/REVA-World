"""World Bank "Women, Business and the Law" (WBL) -> atlas tables (V3, agent A-LAW).

Source (licence CC BY 4.0, World Bank; download page https://wbl.worldbank.org/en/data/download-data,
checked 2026-10-15), three XLSX downloads, one request each:

* ``panel`` — *WBL2024-1-0-Historical-Panel-Data.xlsx*: WBL 1.0 legal-frameworks panel, 190
  economies, report years 1971-2024: WBL index, 8 topic scores, 35 Yes/No questions, unscored
  context (leave lengths in days) — sheet "WBL Panel 2024" — plus, per topic sheet, each question's
  1/0 score and WBL's **legal basis** citation(s).
* ``wbl2026`` — *2026.02.19_WBL26_FINAL Data for Download.xlsx*: WBL 2026 (WBL 2.0: Legal Frameworks,
  Supportive Frameworks, Enforcement Perceptions; 10 topics), report year 2026 (Legal Frameworks
  index and topic scores also for 2024 and 2025). Columns carry WBL's own codes (row 2), e.g.
  ``Pen_Law_PensAgeEq_FullM`` = statutory age at which men can retire with full pension benefits;
  ``*_LB`` columns are the legal basis / source of the questions they prefix.
* ``wbl1_2026`` — *2026-02-26-WBL-2026-1-0-Scores-Values_F.xlsx*: WBL 1.0 values for report year
  2026 ("for comparison purposes"), same 55-column layout as the panel's main sheet.

Atlas tables (``source = "wbl"``): ``panel_1971_2024``, ``wbl2026``, ``wbl1_2026``.

Cells and provenance
--------------------
* Indicator = the provider's code (WBL 2026) or the provider's column header text verbatim (WBL
  1.0, which has no codes). Topic-sheet 1/0 scores are ``"<question>|score"``. The six WBL 1.0
  2026 headers worded differently from the panel are mapped to the panel wording by position
  (:data:`WBL1_HEADER_ALIASES`, each pair checked against the file) so both tables share indicators.
* Numeric cell text is kept exactly (strict decimal, no float round trip); "Yes"/"No" and the
  provider notations ("NS" = no social security system, "N/A", "No applicable provisions could be
  located"…) are ``value_text`` verbatim. The enforcement-perception notation ".." (score not
  reported: too few expert responses) is a row with ``flags.missing = "not_reported"``.
* ``flags.legal_basis`` = list of ``{label, text, column}`` copied verbatim from the provider's
  legal-basis column(s) of the same economy and report year; ``flags.question`` (WBL 2026) is the
  provider's label row text. ``selector`` = ``{file, sheet, row, column, header|code}``.
* Period: report year Y is "current as of October 1" of Y-1 (panel About sheet: "WBL2024 data is
  current as of October 1, 2023"; WBL 2026 report page: data current as of October 1, 2025). Each cell
  covers the as-of day ``[Y-1-10-01, Y-1-10-02)`` and keeps ``report_year`` in flags. The WBL 1.0
  2026 file's About sheet says "current as of October 1, 2026" (a date after the file's own February
  2026 version date); the cells follow the report (2025-10-01) and record the discrepancy in flags.
* The WBL 2026 *pillar* sheets ("I. Legal Frameworks", "II. …", "III. …") carry broken cached
  formula values (``#REF!`` in every cell, checked 2026-10-15) and are not read; their content is in
  the topic sheets. Any ``#REF!``/``#N/A`` error cell elsewhere fails the parse (schema drift).
* Geography: ``provider_geo`` = the "ISO Code" column (geo scheme ``wbl`` = crosswalk ISO3 + ``KSV`` -> ``XKX``); the legacy "Economy
  Code" (ROM, TMP, WBG, ZAR) is kept in flags.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterator, Mapping, Sequence
from datetime import date
from typing import Any

from reva.adapters.connectors import atlas_law_common as common
from reva.adapters.connectors.atlas_xlsx import Workbook, col_letters
from reva.kernel.errors import SourceResponseError
from reva.atlas.sources import AtlasSourceSpec

SOURCE_KEY = "worldbank.wbl"
ATLAS_SOURCE = "wbl"
CONNECTOR_NAME = "atlas.wbl"
CONNECTOR_VERSION = "1.0.0"
PARSER_VERSION = "atlas-wbl-parse-1"
HOST = "wbl.worldbank.org"
BASE = f"https://{HOST}/content/dam/sites/wbl/documents"

ITEMS: dict[str, dict[str, Any]] = {
    "panel": {
        "url": f"{BASE}/2024/WBL2024-1-0-Historical-Panel-Data.xlsx",
        "file": "WBL2024-1-0-Historical-Panel-Data.xlsx",
        "dataset": "panel_1971_2024",
        "max_bytes": 20_000_000,
    },
    "wbl2026": {
        "url": f"{BASE}/2026/2026.02.19_WBL26_FINAL%20Data%20for%20Download.xlsx",
        "file": "2026.02.19_WBL26_FINAL Data for Download.xlsx",
        "dataset": "wbl2026",
        "max_bytes": 10_000_000,
    },
    "wbl1_2026": {
        "url": f"{BASE}/2026/2026-02-26-WBL-2026-1-0-Scores-Values_F.xlsx",
        "file": "2026-02-26-WBL-2026-1-0-Scores-Values_F.xlsx",
        "dataset": "wbl1_2026",
        "max_bytes": 2_000_000,
    },
}
XLSX_MEDIA = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/octet-stream",
    "application/vnd.ms-excel",
)

RIGHTS = {
    "licence": "CC-BY-4.0",
    "licence_url": "https://datacatalog.worldbank.org/public-licenses#cc-by",
    "attribution": "World Bank. Women, Business and the Law [dataset]. https://wbl.worldbank.org/en/wbl-data",
}
AS_OF_BASIS = {
    "panel": "file About sheet: 'The data for each report year is current as of the previous calendar year. "
    "For example WBL2024 data is current as of October 1, 2023.'",
    "wbl2026": "WBL 2026 report (wbl.worldbank.org/en/reports): data current as of October 1, 2025",
    "wbl1_2026": "WBL 2026 report: data current as of October 1, 2025 (the file's About sheet says "
    "'October 1, 2026', which postdates the file version of February 23, 2026)",
}

ID_COLUMNS = ("Economy", "Economy Code", "ISO Code", "Region", "Income Group", "Report Year")
PANEL_MAIN_SHEET = "WBL Panel 2024"
PANEL_TOPIC_SHEETS = ("Mobility", "Workplace", "Pay", "Marriage", "Parenthood", "Entrepreneurship", "Assets", "Pension")
WBL1_2026_SHEET = "WBL 2026 1.0"
WBL26_SCORES_SHEET = "WBL Economy Scores"
WBL26_TOPIC_SHEETS = (
    "1. Safety",
    "2. Mobility",
    "3. Work",
    "4. Pay",
    "5. Marriage",
    "6. Parenthood",
    "7. Childcare",
    "8. Entrepreneurship",
    "9. Assets",
    "10. Pension",
)
WBL26_BROKEN_SHEETS = ("I. Legal Frameworks", "II. Supportive Frameworks", "III. Enforcement Perceptions")

#: WBL 1.0 2026 header (file) -> panel header (canonical indicator), same column position.
WBL1_HEADER_ALIASES: dict[str, str] = {
    "WBL 1.0 INDEX": "WBL INDEX",
    "Does the government administer 100% of maternity leave benefits?": "Does the government administer 100 percent of maternity leave benefits?",
    "Do men and women have equal ownership rights to immovable property?": "Do women and men have equal ownership rights to immovable property?",
    "Is the age at which men and women can retire with full pension benefits the same?": "Is the age at which women and men can retire with full pension benefits the same?",
    "Is the age at which men and women can retire with partial pension benefits the same?": "Is the age at which women and men can retire with partial pension benefits the same?",
    "Is the mandatory retirement age for men and women the same?": "Is the mandatory retirement age for women and men the same?",
}
#: WBL 1.0 unscored context columns measured in days (WBL methodology; "Shared days" etc.).
WBL1_DAY_COLUMNS = frozenset(
    {
        "Length of paid maternity leave",
        "Length of paid paternity leave",
        "Shared days",
        "Days for the mother",
        "Days for the father",
    }
)

_NUM = re.compile(r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][-+]?[0-9]{1,3})?$")
_ERROR_TEXT = ("#REF!", "#N/A", "#VALUE!", "#DIV/0!", "#NAME?", "#NUM!", "#NULL!")
_CODE = re.compile(r"^[A-Za-z][A-Za-z0-9_&]*$")


class WblParseError(SourceResponseError):
    pass


def _err(msg: str) -> WblParseError:
    return WblParseError(msg, retryable=False, kind="schema_drift")


# ----------------------------------------------------------------------------- source


def atlas_source() -> AtlasSourceSpec:
    """The ATLASCORE source spec (connector ``atlas.wbl``; budget key ``worldbank.wbl``)."""
    return AtlasSourceSpec(
        name=CONNECTOR_NAME,
        version=CONNECTOR_VERSION,
        source_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="World Bank Women, Business and the Law",
        licence=RIGHTS["licence"],
        allowed_hosts=(HOST,),
        datasets=tuple(v["dataset"] for v in ITEMS.values()),
        dataset_ids=tuple(v["file"] for v in ITEMS.values()),
        cadence_seconds=365 * 86400,
        planned_requests=len(ITEMS),
        max_response_bytes=20_000_000,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        code_digest=common.code_digest(sys.modules[__name__]),
        field_mappings={"time_semantics": "report year Y = law as of 1 October Y-1 (as-of day)"},
    )


# ----------------------------------------------------------------------------- helpers


def as_of_period(report_year: int) -> tuple[date, date]:
    d = date(report_year - 1, 10, 1)
    return d, date(report_year - 1, 10, 2)


def _is_num(text: str) -> bool:
    return bool(_NUM.match(text))


def _check_error(text: str, where: str) -> None:
    if text.strip() in _ERROR_TEXT:
        raise _err(f"spreadsheet error value {text.strip()!r} in {where}")


def _ids(cells: Mapping[int, Any], where: str) -> tuple[str, str, str, int]:
    def g(i: int) -> str:
        c = cells.get(i)
        return c.text.strip() if c is not None else ""

    economy, econ_code, iso, year = g(0), g(1), g(2), g(5)
    if not iso or not year.isdigit():
        raise _err(f"{where}: missing ISO code or report year")
    return economy, econ_code, iso, int(year)


def _header_row(wb: Workbook, sheet: str, max_scan: int = 8) -> tuple[int, dict[int, str], dict[int, str]]:
    """(header row number, {col: label}, {col: text of the row above or codes}) for a sheet whose
    identifier header row starts with 'Economy'."""
    prev: dict[int, str] = {}
    for rnum, cells in wb.rows(sheet):
        labels = {i: c.text for i, c in cells.items()}
        if labels.get(0, "").strip() == "Economy":
            return rnum, labels, prev
        prev = labels
        if rnum > max_scan:
            break
    raise _err(f"sheet {sheet!r}: no header row starting with 'Economy'")


def _check_ids(labels: Mapping[int, str], sheet: str) -> None:
    got = tuple(labels.get(i, "").strip() for i in range(6))
    if got != ID_COLUMNS:
        raise _err(f"sheet {sheet!r}: identifier columns {got} != {ID_COLUMNS}")


def _base_flags(key: str, report_year: int, econ_code: str, iso: str, economy: str) -> dict[str, Any]:
    f: dict[str, Any] = {
        "report_year": report_year,
        "as_of": as_of_period(report_year)[0].isoformat(),
        "as_of_basis": AS_OF_BASIS[key],
        "economy": economy,
        "transcription": common.VERBATIM,
        "licence": RIGHTS["licence"],
    }
    if econ_code and econ_code != iso:
        f["economy_code"] = econ_code
    return f


def _value_cell(text: str) -> tuple[str | None, str | None, dict[str, Any]]:
    """(value, value_text, extra flags) of a provider cell (never coerced)."""
    t = text.strip()
    if t == "..":
        return None, None, {"missing": "not_reported", "provider_notation": ".."}
    if _is_num(t):
        return t, None, {}
    clipped, cut = common.clip_text(t)
    return None, clipped, ({"value_text_truncated": True} if cut else {})


# ----------------------------------------------------------------------------- WBL 1.0 panel


def _wbl1_unit(header: str, is_score: bool = False) -> str:
    if is_score:
        return "score (0/1)"
    if header in WBL1_DAY_COLUMNS:
        return "days"
    if header.isupper() or header in ("WBL INDEX",):
        return "score (0-100)"
    return "yes/no"


def _main_sheet_rows(
    wb: Workbook,
    sheet: str,
    *,
    key: str,
    file: str,
    raw_sha256: str,
    header_aliases: Mapping[str, str] | None = None,
    expected_headers: Sequence[str] | None = None,
    legal_basis: Mapping[tuple[str, int, str], list[dict[str, str]]] | None = None,
) -> Iterator[dict[str, Any]]:
    hdr_row, labels, _ = _header_row(wb, sheet)
    _check_ids(labels, sheet)
    width = max(labels) + 1
    headers: dict[int, str] = {}
    for j in range(6, width):
        h = (labels.get(j) or "").strip()
        if not h:
            raise _err(f"sheet {sheet!r}: empty header in column {col_letters(j)}")
        canonical = (header_aliases or {}).get(h, h)
        headers[j] = canonical
    if expected_headers is not None:
        got = [headers[j] for j in sorted(headers)]
        if got != list(expected_headers):
            pairs = zip(got, expected_headers, strict=False)
            diff = [(col_letters(j + 6), a, b) for j, (a, b) in enumerate(pairs) if a != b]
            raise _err(
                f"sheet {sheet!r}: headers differ from the panel layout ({len(got)} vs {len(expected_headers)} "
                f"columns): {diff[:5]}"
            )
    dataset = ITEMS[key]["dataset"]
    for rnum, cells in wb.rows(sheet):
        if rnum <= hdr_row:
            continue
        economy, econ_code, iso, year = _ids(cells, f"{sheet}!row {rnum}")
        base = _base_flags(key, year, econ_code, iso, economy)
        period = as_of_period(year)
        for j, h in headers.items():
            c = cells.get(j)
            if c is None or not c.text.strip():
                continue
            _check_error(c.text, f"{sheet}!{c.ref}")
            value, text, extra = _value_cell(c.text)
            flags = {**base, **extra}
            raw_header = (labels.get(j) or "").strip()
            if raw_header != h:
                flags["provider_header"] = raw_header
            lb = (legal_basis or {}).get((iso, year, h))
            if lb:
                flags["legal_basis"] = lb
            yield common.row(
                source=ATLAS_SOURCE,
                dataset=dataset,
                indicator=h,
                provider_geo=iso,
                period=period,
                raw_sha256=raw_sha256,
                selector={"file": file, "sheet": sheet, "row": rnum, "column": col_letters(j), "header": raw_header},
                flags=flags,
                unit=_wbl1_unit(h),
                value=value,
                value_text=text,
            )


def _panel_topic_pass(
    wb: Workbook, *, file: str, raw_sha256: str
) -> tuple[dict[tuple[str, int, str], list[dict[str, str]]], list[dict[str, Any]]]:
    """Legal-basis map {(iso, year, question): [{label, text, column}]} and the '|score' rows."""
    lb_map: dict[tuple[str, int, str], list[dict[str, str]]] = {}
    score_rows: list[dict[str, Any]] = []
    dataset = ITEMS["panel"]["dataset"]
    for sheet in PANEL_TOPIC_SHEETS:
        hdr_row, labels, _ = _header_row(wb, sheet)
        _check_ids(labels, sheet)
        roles: dict[int, tuple[str, str]] = {}  # col -> (role, question)
        last_q: str | None = None
        scored_q: str | None = None
        for j in range(6, max(labels) + 1):
            h = (labels.get(j) or "").strip()
            if not h:
                raise _err(f"sheet {sheet!r}: empty header in column {col_letters(j)}")
            if j == 6:
                if not h.endswith(" SCORE"):
                    raise _err(f"sheet {sheet!r}: column G is {h!r}, expected '<TOPIC> SCORE'")
                continue  # topic score: taken from the main sheet
            if h == "Score":
                if last_q is None:
                    raise _err(f"sheet {sheet!r}: 'Score' column {col_letters(j)} without a question")
                scored_q = last_q
                roles[j] = ("score", last_q)
            elif h.startswith("Legal Basis"):
                if scored_q is None:
                    raise _err(f"sheet {sheet!r}: legal basis column {col_letters(j)} without a scored question")
                roles[j] = ("lb", scored_q)
            else:
                last_q = h
        for rnum, cells in wb.rows(sheet):
            if rnum <= hdr_row:
                continue
            economy, econ_code, iso, year = _ids(cells, f"{sheet}!row {rnum}")
            for j, (role, q) in roles.items():
                c = cells.get(j)
                if c is None or not c.text.strip():
                    continue
                _check_error(c.text, f"{sheet}!{c.ref}")
                if role == "lb":
                    text, cut = common.clip_text(c.text.strip(), 1500)
                    entry = {
                        "label": (labels.get(j) or "").strip(),
                        "text": text,
                        "column": f"{sheet}!{col_letters(j)}",
                    }
                    if cut:
                        entry["truncated"] = "true"
                    lb_map.setdefault((iso, year, q), []).append(entry)
                else:
                    value, _vtext, extra = _value_cell(c.text)
                    if value is None:
                        raise _err(f"{sheet}!{c.ref}: score is not numeric ({c.text!r})")
                    score_rows.append(
                        {
                            "key": (iso, year, q),
                            "row": common.row(
                                source=ATLAS_SOURCE,
                                dataset=dataset,
                                indicator=f"{q}|score",
                                provider_geo=iso,
                                period=as_of_period(year),
                                raw_sha256=raw_sha256,
                                selector={
                                    "file": file,
                                    "sheet": sheet,
                                    "row": rnum,
                                    "column": col_letters(j),
                                    "header": "Score",
                                    "question": q,
                                },
                                flags={**_base_flags("panel", year, econ_code, iso, economy), **extra},
                                unit="score (0/1)",
                                value=value,
                            ),
                        }
                    )
    return lb_map, score_rows


def parse_panel(body: bytes, raw_sha256: str) -> list[dict[str, Any]]:
    """WBL 1.0 panel 1971-2024 -> atlas rows (main-sheet values + topic-sheet scores, legal basis)."""
    wb = Workbook(body)
    missing = [s for s in (PANEL_MAIN_SHEET, *PANEL_TOPIC_SHEETS) if s not in wb.sheets]
    if missing:
        raise _err(f"panel workbook lacks sheets {missing}")
    file = ITEMS["panel"]["file"]
    lb_map, score_rows = _panel_topic_pass(wb, file=file, raw_sha256=raw_sha256)
    rows = list(
        _main_sheet_rows(wb, PANEL_MAIN_SHEET, key="panel", file=file, raw_sha256=raw_sha256, legal_basis=lb_map)
    )
    main_questions = {r["indicator"] for r in rows}
    orphan = sorted({k[2] for k in lb_map} - main_questions)
    if orphan:
        raise _err(f"topic-sheet questions absent from the main sheet: {orphan[:5]}")
    for s in score_rows:
        lb = lb_map.get(s["key"])
        r = s["row"]
        if lb:
            flags = json.loads(r["flags"])
            flags["legal_basis"] = lb
            r["flags"] = common.canon(flags)
        rows.append(r)
    return rows


def panel_headers(body: bytes) -> list[str]:
    """The panel main-sheet headers after the identifier columns (the WBL 1.0 layout)."""
    wb = Workbook(body)
    _, labels, _ = _header_row(wb, PANEL_MAIN_SHEET)
    return [(labels.get(j) or "").strip() for j in range(6, max(labels) + 1)]


def parse_wbl1_2026(
    body: bytes, raw_sha256: str, expected_headers: Sequence[str] | None = None
) -> list[dict[str, Any]]:
    """WBL 1.0 values for report year 2026 (same layout as the panel main sheet)."""
    wb = Workbook(body)
    if WBL1_2026_SHEET not in wb.sheets:
        raise _err(f"WBL 1.0 2026 workbook lacks sheet {WBL1_2026_SHEET!r}")
    return list(
        _main_sheet_rows(
            wb,
            WBL1_2026_SHEET,
            key="wbl1_2026",
            file=ITEMS["wbl1_2026"]["file"],
            raw_sha256=raw_sha256,
            header_aliases=WBL1_HEADER_ALIASES,
            expected_headers=expected_headers,
        )
    )


# ----------------------------------------------------------------------------- WBL 2026 (2.0)


def wbl26_unit(code: str, values_numeric: bool) -> str:
    if code.endswith(("_ts", "_index")):
        return "score (0-100)"
    if re.search(r"_Enf_.*_is$", code):
        return "score (WBL 2026 enforcement-perception indicator scale)"
    if code.endswith("_is"):
        return "score (0-1)"
    if code.startswith("Pen_Law_PensAgeEq_") and code[-5:] in ("FullM", "FullW", "PartM", "PartW"):
        return "years"
    if code in ("Pen_Law_MandAgeEq_Women", "Pen_Law_MandAgeEq_Men"):
        return "years"
    if code in ("Par_Law_MatLeave_Total", "Par_Law_PatLeave_Total"):
        return "days"
    if code in ("Pen_Law_PensAgeEq_Full", "Pen_Law_PensAgeEq_Part"):
        return "score (0-1)"
    return "as published" if values_numeric else "yes/no or provider category"


def _lb_targets(codes: Mapping[int, str]) -> dict[int, list[int]]:
    """LB column -> data columns it documents: data codes that start with the LB prefix, assigned to
    the longest matching LB prefix (e.g. ``Pen_Law_PensAgeEq_Full_LB`` documents ``…_Full``,
    ``…_FullM``, ``…_FullW``)."""
    lbs = {j: c[: -len("_LB")] for j, c in codes.items() if c.endswith("_LB")}
    out: dict[int, list[int]] = {j: [] for j in lbs}
    for j, c in codes.items():
        if c.endswith("_LB"):
            continue
        best = None
        for lj, prefix in lbs.items():
            if (
                c == prefix
                or c.startswith(prefix + "_")
                or (c.startswith(prefix) and c[len(prefix) : len(prefix) + 1].isupper())
            ):
                if best is None or len(prefix) > len(lbs[best]):
                    best = lj
        if best is not None:
            out[best].append(j)
    return out


def parse_wbl2026(body: bytes, raw_sha256: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """WBL 2026 -> atlas rows (+ parse notes). Economy Scores sheet first, then the 10 topic sheets;
    a code seen twice for the same economy and report year must carry the same text (else drift)."""
    wb = Workbook(body)
    needed = (WBL26_SCORES_SHEET, *WBL26_TOPIC_SHEETS)
    missing = [s for s in needed if s not in wb.sheets]
    if missing:
        raise _err(f"WBL 2026 workbook lacks sheets {missing}")
    file = ITEMS["wbl2026"]["file"]
    dataset = ITEMS["wbl2026"]["dataset"]
    seen: dict[tuple[str, str, int], tuple[str, str]] = {}  # (code, iso, year) -> (text, where)
    rows: list[dict[str, Any]] = []
    duplicates = 0
    for sheet in needed:
        hdr_row, labels, _ = _header_row(wb, sheet)
        _check_ids(labels, sheet)
        codes_row = next((cells for rnum, cells in wb.rows(sheet) if rnum == 2), None)
        if codes_row is None:
            raise _err(f"sheet {sheet!r}: no code row 2")
        codes: dict[int, str] = {}
        for j in range(6, max(max(labels), max(codes_row)) + 1):
            c = codes_row.get(j)
            code = c.text.strip() if c is not None else ""
            if not code:
                lab = (labels.get(j) or "").strip()
                if lab:
                    raise _err(f"sheet {sheet!r}: labelled column {col_letters(j)} has no code")
                continue  # blank spacer column
            if not _CODE.match(code):
                raise _err(f"sheet {sheet!r}: malformed code {code!r} in column {col_letters(j)}")
            codes[j] = code
        lb_targets = _lb_targets(codes)
        lb_of: dict[int, list[int]] = {}
        for lj, targets in lb_targets.items():
            for tj in targets:
                lb_of.setdefault(tj, []).append(lj)
        numeric_cols: dict[int, bool] = {}
        data = [(rnum, cells) for rnum, cells in wb.rows(sheet) if rnum > hdr_row]
        for j in codes:
            vals = [cells[j].text.strip() for _rn, cells in data if j in cells and cells[j].text.strip()]
            numeric_cols[j] = bool(vals) and all(_is_num(v) or v in ("..", "NS", "N/A") for v in vals)
        for rnum, cells in data:
            economy, econ_code, iso, year = _ids(cells, f"{sheet}!row {rnum}")
            base = _base_flags("wbl2026", year, econ_code, iso, economy)
            period = as_of_period(year)
            for j, code in codes.items():
                if code.endswith("_LB"):
                    continue
                c = cells.get(j)
                if c is None or not c.text.strip():
                    continue
                _check_error(c.text, f"{sheet}!{c.ref}")
                k = (code, iso, year)
                if k in seen:
                    if seen[k][0] != c.text.strip():
                        raise _err(f"{code} for {iso} {year}: {sheet}!{c.ref} != {seen[k][1]}")
                    duplicates += 1
                    continue
                seen[k] = (c.text.strip(), f"{sheet}!{c.ref}")
                value, text, extra = _value_cell(c.text)
                flags = {**base, **extra}
                q, _ = common.clip_text((labels.get(j) or "").strip(), 600)
                if q:
                    flags["question"] = q
                lbs = []
                for lj in lb_of.get(j, []):
                    lc = cells.get(lj)
                    if lc is not None and lc.text.strip():
                        _check_error(lc.text, f"{sheet}!{lc.ref}")
                        t, cut = common.clip_text(lc.text.strip(), 1500)
                        e = {
                            "label": (labels.get(lj) or "").strip(),
                            "text": t,
                            "column": f"{sheet}!{col_letters(lj)}",
                            "code": codes[lj],
                        }
                        if cut:
                            e["truncated"] = "true"
                        lbs.append(e)
                if lbs:
                    flags["legal_basis"] = lbs
                rows.append(
                    common.row(
                        source=ATLAS_SOURCE,
                        dataset=dataset,
                        indicator=code,
                        provider_geo=iso,
                        period=period,
                        raw_sha256=raw_sha256,
                        selector={"file": file, "sheet": sheet, "row": rnum, "column": col_letters(j), "code": code},
                        flags=flags,
                        unit=wbl26_unit(code, numeric_cols[j]),
                        value=value,
                        value_text=text,
                    )
                )
    notes = {
        "skipped_sheets": [s for s in WBL26_BROKEN_SHEETS if s in wb.sheets],
        "skipped_reason": "pillar sheets hold broken cached formula values (#REF!); content is in topic sheets",
        "duplicate_cells_identical": duplicates,
    }
    return rows, notes


# ----------------------------------------------------------------------------- ingest

NOTES = {
    "panel": "WBL 1.0 panel, report years 1971-2024, 190 economies; legal basis per question in flags",
    "wbl2026": "WBL 2026 (2.0) legal/supportive/enforcement data, 190 economies; pillar sheets not read (#REF!)",
    "wbl1_2026": "WBL 1.0 values for report year 2026 (comparison file), panel layout and indicators",
}


async def atlas_ingest(run: Any) -> dict[str, Any]:
    """Fetch, archive and register the WBL atlas tables (``run`` = ``reva.atlas.ingest.AtlasRun``).
    A failed download is a typed ``Blocked`` entry; the other downloads are still registered. The
    WBL 1.0 2026 file is checked against the panel's header layout when the panel is in the run."""
    report = common.IngestReport(CONNECTOR_NAME)
    wanted = [k for k, v in ITEMS.items() if run.wants(v["dataset"])]
    if "wbl1_2026" in wanted and "panel" not in wanted:
        wanted.insert(0, "panel")  # needed for the header check only (not registered)
    raws: dict[str, tuple[Any, str]] = {}
    for key in wanted:
        item = ITEMS[key]
        got = await common.fetch(run, item["url"], key=key, source_key=SOURCE_KEY, media_types=XLSX_MEDIA,
                                 max_bytes=item["max_bytes"])  # fmt: skip
        if isinstance(got, common.Blocked):
            report.blocked.append(got)
            continue
        sha = run.archive(got)
        raws[key] = (got, sha)
        report.raw.append({"item": key, "raw_sha256": sha, "bytes": len(got.body), "url": item["url"]})
    panel_hdrs = panel_headers(raws["panel"][0].body) if "panel" in raws else None
    for key, (raw, sha) in raws.items():
        if not run.wants(ITEMS[key]["dataset"]):
            continue
        if key == "panel":
            rows = parse_panel(raw.body, sha)
        elif key == "wbl2026":
            rows, _notes = parse_wbl2026(raw.body, sha)
        else:
            rows = parse_wbl1_2026(raw.body, sha, expected_headers=panel_hdrs)
        out = run.register(dataset=ITEMS[key]["dataset"], frame=common.atlas_frame(rows), raw_sha256s=[sha],
                           coverage_note=NOTES[key], geo_scheme="wbl", rights=RIGHTS)  # fmt: skip
        report.tables.append(common.table_summary(out))
    return report.as_dict()


__all__ = [
    "ATLAS_SOURCE",
    "CONNECTOR_NAME",
    "ITEMS",
    "WBL1_HEADER_ALIASES",
    "atlas_ingest",
    "atlas_source",
    "as_of_period",
    "panel_headers",
    "parse_panel",
    "parse_wbl1_2026",
    "parse_wbl2026",
]
