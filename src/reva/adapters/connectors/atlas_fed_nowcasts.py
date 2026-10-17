"""Federal Reserve Banks' nowcasts with every published update + the real-time data to judge them (V7, BEAT-NOWCAST).

Source ``atlas.fed_nowcasts`` (open downloads, no key, generic project User-Agent) -> five vintage atlas tables:

* ``fed_nowcasts.gdpnow`` (family ``atl_gdpnow``) — Atlanta Fed GDPNow, workbook
  ``GDPTrackingModelDataAndForecasts.xlsx``: sheets ``TrackingDeepArchives`` ("Historical archives of GDPNow model
  forecasts for 2011q4 through 2014q1. These real-time forecasts were all made before the model went 'live' on the
  Atlanta Fed website.") and ``TrackingArchives`` ("Archives of TrackingHistory tab"), column ``GDP Nowcast`` per
  ``Forecast Date`` x ``Quarter being forecasted``; the current quarter from ``CurrentQtrEvolution`` (``Date``/
  ``GDP*`` triplets, quarter named in ``TrackingHistory`` row 2 "Evolution of GDP nowcast and components for
  YYYYqN"). One vintage per forecast date (``GDPNOW|v=YYYY-MM-DD``, ``estimate_kind = projection``, flag
  ``made_before_public_launch`` for the deep archive). BEA's advance estimate and its publication date as recorded
  in the archives (``Advance Estimate From BEA``, ``Publication Date of Advance Estimate``) ->
  ``GDPNOW_BEA_ADVANCE|v=advance`` (``published_history``, rule ``bea_release_recorded_by_atlanta_fed``).
* ``fed_nowcasts.nyfed`` (family ``nyfed_nowcast``) — New York Fed Staff Nowcast, workbook
  ``new-york-fed-staff-nowcast_data_2002-present.xlsx`` sheet ``Forecasts By Quarter`` (``Forecast Date`` x quarter
  columns). The workbook states: "Forecasts for GDP growth in quarters 2016Q1 to present were made in real-time"
  and "Forecasts ... 2002Q1 through 2015Q4 are historical reconstructions based on real-time data": reconstructed
  cells are loaded with ``reconstruction = true`` and **no availability** (never selected as of a date).
* ``fed_nowcasts.cleveland`` (family ``clev_inflation_nowcast``) — Cleveland Fed inflation nowcasting chart data
  ``nowcast_month.json`` (month-over-month percent change) and ``nowcast_quarter.json`` (quarterly annualized
  percent change): one chart per target period (``subcaption`` ``2013-7`` / ``2013:Q3``), series CPI, core CPI,
  PCE, core PCE and their "Actual" values (the published value, dated on its release day). Chart dates are
  ``MM/DD`` labels; the year is the target period's year, or the next year when the label month is before the
  period's first month (nowcasts run from the start of the period to the release, < 6 months).
* ``fed_nowcasts.rtdsm_monthly`` (family ``philfed_rtdsm_monthly``) — Philadelphia Fed Real-Time Data Set monthly
  vintages (``EMPLOY``, ``IPT``, ``H``, ``HSTARTS``, ``RCONM``; ``<VAR>yyMmm`` columns), vintages from
  :data:`FIRST_VINTAGE` and observations from :data:`FIRST_OBS` (disk budget), rule
  ``rtdsm_monthly_vintage_month_end``.
* ``fed_nowcasts.release_values`` (family ``philfed_release_values``) — Philadelphia Fed
  ``routput_first_second_third.xlsx`` (real GDP, "Q/Q Growth (Annual Rate, Percentage Points)", "First-Release,
  Second-Release, Third-Release, and the Most-Recent-Release Values"): the truth of the nowcast evaluation
  (first release = BEA advance/initial estimate; third release).

Licences (quoted verbatim in each table's rights and in docs/dev/v7_beat_nowcast.md): Atlanta Fed — reproduction
"not for commercial gain or monetary profit" with attribution (**non-commercial**); New York Fed — "non-exclusive
license ... to use, copy, and distribute Content for your personal or business purposes" (no endorsement);
Cleveland Fed — "for noncommercial, personal, or educational purposes only" with credit (**non-commercial**);
Philadelphia Fed — no data licence, footer "All rights reserved" (**restricted**: internal evaluation only).
FRED/ALFRED (St. Louis Fed, incl. the Economic News Index) is **not** used: the FRED Terms of Use prohibit using
"FRED® Content in connection with the development or training of any software program or system or machine
learning" (owner decision).
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from reva.adapters.connectors.agri_common import module_digest
from reva.adapters.connectors.atlas_xlsx import Workbook, XlsxError
from reva.adapters.connectors.vintage_common import (
    Rows,
    coverage_line,
    day_after,
    decimal_or_none,
    month_bounds,
    quarter_bounds,
    utc_midnight,
    vintage_flags,
)
from reva.atlas.sources import AtlasSourceSpec
from reva.kernel.errors import SourceResponseError

NAME = "atlas.fed_nowcasts"
VERSION = "1.0.0"
PARSER_VERSION = "atlas_fed_nowcasts/1"
SOURCE_KEY = "fed.nowcasts"
ATLAS_SOURCE = "fed_nowcasts"
DS_GDPNOW, DS_NYFED, DS_CLEV, DS_RTDSM, DS_REL = "gdpnow", "nyfed", "cleveland", "rtdsm_monthly", "release_values"
DATASETS = (DS_GDPNOW, DS_NYFED, DS_CLEV, DS_RTDSM, DS_REL)
GEO = "USA"

ATL_HOST, NY_HOST, CLE_HOST, PHL_HOST = (
    "www.atlantafed.org",
    "www.newyorkfed.org",
    "www.clevelandfed.org",
    "www.philadelphiafed.org",
)
GDPNOW_URL = (
    f"https://{ATL_HOST}/-/media/Project/Atlanta/FRBA/Documents/research-and-data/data/gdpnow/"
    "GDPTrackingModelDataAndForecasts.xlsx"
)
NYFED_URL = f"https://{NY_HOST}/medialibrary/media/research/policy/nowcast/new-york-fed-staff-nowcast_data_2002-present.xlsx"
CLEV_URL = f"https://{CLE_HOST}/-/media/files/webcharts/inflationnowcasting/nowcast_{{freq}}.json?sc_lang=en"
PHL_XLSX = f"https://{PHL_HOST}/-/media/frbp/assets/surveys-and-data/real-time-data/data-files/xlsx/{{file}}"
XLSX_MEDIA = ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "application/octet-stream")
JSON_MEDIA = ("application/json", "text/plain", "text/json", "application/octet-stream")
MAX_BYTES = 16_000_000

#: RTDSM monthly-vintage file -> (column root, REVA variable, unit as documented).
RTDSM_MONTHLY: dict[str, tuple[str, str, str]] = {
    "employMvMd.xlsx": ("EMPLOY", "nonfarm_payroll_employment", "thousands of employees, seasonally adjusted"),
    "iptMvMd.xlsx": ("IPT", "industrial_production_index", "index (industrial production, total), SA"),
    "hMvMd.xlsx": ("H", "aggregate_weekly_hours_index", "index (aggregate weekly hours, total private), SA"),
    "hstartsMvMd.xlsx": ("HSTARTS", "housing_starts", "thousands of units (housing starts), SAAR"),
    "rconmMvMd.xlsx": ("RCONM", "real_pce_monthly_level", "billions of real dollars (monthly real PCE), SAAR"),
}
RELEASE_FILE = "routput_first_second_third.xlsx"
FIRST_VINTAGE = (2010, 1)
FIRST_OBS = (1990, 1)

CLEV_SERIES = {
    "CPI Inflation": ("cpi", "projection"),
    "Core CPI Inflation": ("core_cpi", "projection"),
    "PCE Inflation": ("pce", "projection"),
    "Core PCE Inflation": ("core_pce", "projection"),
    "Actual CPI Inflation": ("cpi", "published_history"),
    "Actual Core CPI Inflation": ("core_cpi", "published_history"),
    "Actual PCE Inflation": ("pce", "published_history"),
    "Actual Core PCE Inflation": ("core_pce", "published_history"),
}
CLEV_VARIABLE = {
    ("month", "cpi"): "cpi_inflation_m",
    ("month", "core_cpi"): "core_cpi_inflation_m",
    ("month", "pce"): "pce_inflation_m",
    ("month", "core_pce"): "core_pce_inflation_m",
    ("quarter", "cpi"): "cpi_inflation_q_ann",
    ("quarter", "core_cpi"): "core_cpi_inflation_q_ann",
    ("quarter", "pce"): "pce_inflation_q_ann",
    ("quarter", "core_pce"): "core_pce_inflation_q_ann",
}
NYFED_REALTIME_NOTE = "Forecasts for GDP growth in quarters 2016Q1 to present were made in real-time"
NYFED_FIRST_REALTIME = (2016, 1)
EXCEL_EPOCH = date(1899, 12, 30)

_Q_RE = re.compile(r"^([0-9]{4})Q([1-4])$")
_QC_RE = re.compile(r"^([0-9]{4}):Q([1-4])$")
_MC_RE = re.compile(r"^([0-9]{4})-([0-9]{1,2})$")
_MD_RE = re.compile(r"^([0-9]{2})/([0-9]{2})$")
_RT_COL = re.compile(r"^([A-Z]+)([0-9]{2})M([0-9]{1,2})$")
_RT_ROW = re.compile(r"^([0-9]{4}):([0-9]{2})$")
_TRACK_Q = re.compile(r"Evolution of GDP nowcast and components for ([0-9]{4})q([1-4])", re.I)
_UPDATED = re.compile(r"Last updated on:\s*([A-Za-z]+ [0-9]{1,2}, [0-9]{4})")


class FedNowcastError(SourceResponseError):
    def __init__(self, msg: str, kind: str = "schema_drift") -> None:
        super().__init__(msg, retryable=False, kind=kind)


@dataclass
class Report:
    cells: int = 0
    skipped: Counter[str] = field(default_factory=Counter)
    notes: dict[str, Any] = field(default_factory=dict)


# ----------------------------------------------------------------------------------------------- helpers
def excel_date(text: Any) -> date:
    try:
        n = float(str(text))
    except (TypeError, ValueError) as e:
        raise FedNowcastError(f"not an Excel date serial: {text!r}") from e
    if not 20_000 < n < 80_000 or n != int(n):
        raise FedNowcastError(f"Excel date serial out of range: {text!r}")
    return EXCEL_EPOCH + timedelta(days=int(n))


def quarter_of(d: date) -> tuple[int, int]:
    return d.year, (d.month - 1) // 3 + 1


def quarter_label(y: int, q: int) -> str:
    return f"{y}Q{q}"


def first_of_month_after(y: int, m: int, k: int) -> datetime:
    """First instant (UTC) of the k-th month after month (y, m)."""
    idx = y * 12 + (m - 1) + k
    return utc_midnight(date(idx // 12, idx % 12 + 1, 1))


def _cell(rows: Rows, ds: str, ind: str, start: date, end: date, value: str, unit: str, flags: str, sha: str,
          sel: dict[str, Any]) -> None:  # fmt: skip
    rows.add(source=ATLAS_SOURCE, dataset=ds, indicator=ind, provider_geo=GEO, start=start, end=end, value=value,
             unit=unit, flags=flags, raw_sha256=sha, selector=sel, iso3=GEO)  # fmt: skip


# ----------------------------------------------------------------------------------------------- GDPNow
def parse_gdpnow(body: bytes, *, raw_sha256: str, rows: Rows) -> Report:
    rep = Report()
    try:
        wb = Workbook(body)
    except XlsxError as e:
        raise FedNowcastError(f"GDPNow workbook unreadable: {e}") from e
    advance: dict[tuple[int, int], tuple[str, date, str]] = {}
    seen: set[tuple[date, tuple[int, int]]] = set()
    unit = "percent change at a seasonally adjusted annual rate (real GDP, quarter on quarter)"
    for sheet, before_launch in (("TrackingDeepArchives", True), ("TrackingArchives", False)):
        t = wb.table(sheet)
        if not t:
            raise FedNowcastError(f"GDPNow sheet {sheet} missing or empty")
        head = [str(h).strip() if h is not None else "" for h in t[0]]
        try:
            i_d, i_q, i_g = head.index("Forecast Date"), head.index("Quarter being forecasted"), head.index("GDP Nowcast")
            i_a, i_p = head.index("Advance Estimate From BEA"), head.index("Publication Date of Advance Estimate")
        except ValueError as e:
            raise FedNowcastError(f"GDPNow {sheet}: header changed: {head[:6]}...") from e
        for n, r in enumerate(t[1:], start=2):
            if r[i_d] in (None, "") and r[i_g] in (None, ""):
                continue
            d, qd = excel_date(r[i_d]), excel_date(r[i_q])
            yq = quarter_of(qd)
            val = decimal_or_none(r[i_g])
            if val is None:
                rep.skipped["gdpnow_value_not_a_number"] += 1
                continue
            if (d, yq) in seen:
                raise FedNowcastError(f"GDPNow {sheet}: duplicate forecast date {d} for {yq}")
            seen.add((d, yq))
            s, e = quarter_bounds(*yq)
            fl = vintage_flags(
                family="atl_gdpnow", vintage=d.isoformat(), available_at=day_after(d),
                basis="fed_nowcast_publication_day", variable="real_gdp_growth_q_ann", provider_variable="GDP Nowcast",
                estimate_kind="projection", vintage_date_text=str(r[i_d]), sheet=sheet,
                made_before_public_launch=before_launch, target_quarter=quarter_label(*yq),
            )  # fmt: skip
            _cell(rows, DS_GDPNOW, f"GDPNOW|v={d.isoformat()}", s, e, val, unit, fl, raw_sha256,
                  {"sheet": sheet, "row": n, "column": "GDP Nowcast"})  # fmt: skip
            rep.cells += 1
            adv, pub = decimal_or_none(r[i_a]), r[i_p]
            if adv is not None and pub not in (None, "", "#N/A"):
                pd = excel_date(pub)
                prev = advance.get(yq)
                if prev is not None and (prev[0] != adv or prev[1] != pd):
                    raise FedNowcastError(f"GDPNow: two advance estimates recorded for {yq}: {prev[:2]} vs {adv, pd}")
                advance[yq] = (adv, pd, f"{sheet}!row{n}")
    # current quarter (not yet archived)
    th = wb.table("TrackingHistory")
    cq = None
    for r in th[:4]:
        for c in r:
            m = _TRACK_Q.search(str(c or ""))
            if m:
                cq = (int(m.group(1)), int(m.group(2)))
    ev = wb.table("CurrentQtrEvolution")
    if cq is not None and ev:
        head = [str(h or "").strip() for h in ev[0]]
        cols = [i for i, h in enumerate(head) if h == "Date"]
        for n, r in enumerate(ev[1:], start=2):
            for i in cols:
                if i + 2 >= len(r) or r[i] in (None, ""):
                    continue
                d = excel_date(r[i])
                val = decimal_or_none(r[i + 2])
                if val is None or (d, cq) in seen:
                    rep.skipped["current_quarter_already_archived_or_empty"] += 1
                    continue
                seen.add((d, cq))
                s, e = quarter_bounds(*cq)
                fl = vintage_flags(
                    family="atl_gdpnow", vintage=d.isoformat(), available_at=day_after(d),
                    basis="fed_nowcast_publication_day", variable="real_gdp_growth_q_ann", provider_variable="GDP*",
                    estimate_kind="projection", vintage_date_text=str(r[i]), sheet="CurrentQtrEvolution",
                    made_before_public_launch=False, target_quarter=quarter_label(*cq),
                    major_releases=str(r[i + 1] or "")[:200],
                )  # fmt: skip
                _cell(rows, DS_GDPNOW, f"GDPNOW|v={d.isoformat()}", s, e, val, unit, fl, raw_sha256,
                      {"sheet": "CurrentQtrEvolution", "row": n, "column": i})  # fmt: skip
                rep.cells += 1
    rep.notes["current_quarter"] = quarter_label(*cq) if cq else None
    for yq, (adv, pd, where) in sorted(advance.items()):
        s, e = quarter_bounds(*yq)
        fl = vintage_flags(
            family="atl_gdpnow", vintage="advance", available_at=day_after(pd),
            basis="bea_release_recorded_by_atlanta_fed", variable="real_gdp_growth_q_ann",
            provider_variable="Advance Estimate From BEA", estimate_kind="published_history",
            vintage_date_text=pd.isoformat(), bea_publication_date=pd.isoformat(), recorded_by="Atlanta Fed GDPNow",
        )  # fmt: skip
        _cell(rows, DS_GDPNOW, "GDPNOW_BEA_ADVANCE|v=advance", s, e, adv, unit, fl, raw_sha256, {"where": where})
        rep.cells += 1
    rep.notes["advance_estimates"] = len(advance)
    rep.notes["forecast_dates"] = len({d for d, _ in seen})
    return rep


# ----------------------------------------------------------------------------------------------- NY Fed
def parse_nyfed(body: bytes, *, raw_sha256: str, rows: Rows) -> Report:
    rep = Report()
    try:
        wb = Workbook(body)
    except XlsxError as e:
        raise FedNowcastError(f"NY Fed workbook unreadable: {e}") from e
    t = wb.table("Forecasts By Quarter")
    text = " ".join(str(c) for r in t[:14] for c in r if c)
    if NYFED_REALTIME_NOTE not in text:
        raise FedNowcastError("NY Fed: the real-time / reconstruction statement changed; re-read the workbook notes")
    hi = next((i for i, r in enumerate(t) if r and str(r[0] or "").strip() == "Forecast Date"), None)
    if hi is None:
        raise FedNowcastError("NY Fed: no 'Forecast Date' header row")
    head = t[hi]
    qcols: dict[int, tuple[int, int]] = {}
    for i, h in enumerate(head[1:], start=1):
        m = _Q_RE.match(str(h or "").strip())
        if m:
            qcols[i] = (int(m.group(1)), int(m.group(2)))
        elif h not in (None, ""):
            raise FedNowcastError(f"NY Fed: unexpected column header {h!r}")
    unit = "percent change at a seasonally adjusted annual rate (real GDP, quarter on quarter)"
    for n, r in enumerate(t[hi + 1 :], start=hi + 2):
        if not r or r[0] in (None, ""):
            continue
        d = excel_date(r[0])
        for i, yq in qcols.items():
            if i >= len(r):
                continue
            val = decimal_or_none(r[i])
            if val is None:
                continue
            realtime = yq >= NYFED_FIRST_REALTIME
            s, e = quarter_bounds(*yq)
            fl = vintage_flags(
                family="nyfed_nowcast", vintage=d.isoformat(),
                available_at=day_after(d) if realtime else None,
                basis="fed_nowcast_publication_day" if realtime else None,
                variable="real_gdp_growth_q_ann", provider_variable="New York Fed Staff Nowcast",
                estimate_kind="projection", vintage_date_text=str(r[0]), reconstruction=not realtime,
                target_quarter=quarter_label(*yq),
            )  # fmt: skip
            _cell(rows, DS_NYFED, f"NYFED_NOWCAST|v={d.isoformat()}", s, e, val, unit, fl, raw_sha256,
                  {"sheet": "Forecasts By Quarter", "row": n, "column": quarter_label(*yq)})  # fmt: skip
            rep.cells += 1
            rep.skipped["reconstructed_cells_loaded_without_availability"] += not realtime
    return rep


# ----------------------------------------------------------------------------------------------- Cleveland
def clev_target(freq: str, sub: str) -> tuple[int, int]:
    """(year, month) of the first month of the chart's target period."""
    if freq == "month":
        m = _MC_RE.match(sub.strip())
        if not m or not 1 <= int(m.group(2)) <= 12:
            raise FedNowcastError(f"Cleveland: unexpected monthly subcaption {sub!r}")
        return int(m.group(1)), int(m.group(2))
    m = _QC_RE.match(sub.strip())
    if not m:
        raise FedNowcastError(f"Cleveland: unexpected quarterly subcaption {sub!r}")
    return int(m.group(1)), 3 * (int(m.group(2)) - 1) + 1


def clev_date(label: str, target: tuple[int, int]) -> date:
    m = _MD_RE.match(label.strip())
    if not m:
        raise FedNowcastError(f"Cleveland: unexpected date label {label!r}")
    mm, dd = int(m.group(1)), int(m.group(2))
    y = target[0] + (1 if mm < target[1] else 0)
    return date(y, mm, dd)


def parse_cleveland(body: bytes, *, freq: str, raw_sha256: str, rows: Rows) -> Report:
    rep = Report()
    try:
        charts = json.loads(body)
    except ValueError as e:
        raise FedNowcastError(f"Cleveland {freq}: not JSON") from e
    if not isinstance(charts, list) or not charts:
        raise FedNowcastError(f"Cleveland {freq}: expected a non-empty list of charts")
    unit = (
        "month-over-month percent change, SA" if freq == "month" else "quarter-over-quarter percent change, annualized"
    )
    gen = None
    for ci, ch in enumerate(charts):
        meta = ch.get("chart") or {}
        gen = meta.get("_comment", gen)
        target = clev_target(freq, str(meta.get("subcaption", "")))
        if freq == "month":
            start, end = month_bounds(*target)
            tlabel = f"{target[0]}-{target[1]:02d}"
        else:
            start, end = quarter_bounds(target[0], (target[1] - 1) // 3 + 1)
            tlabel = f"{target[0]}Q{(target[1] - 1) // 3 + 1}"
        cats = [c for c in ch["categories"][0]["category"] if c.get("vline") != "true"]
        for ds in ch.get("dataset", []):
            name = str(ds.get("seriesname", "")).strip()
            if name not in CLEV_SERIES:
                raise FedNowcastError(f"Cleveland: unexpected series {name!r}")
            key, kind = CLEV_SERIES[name]
            data = ds.get("data", [])
            if len(data) != len(cats):
                raise FedNowcastError(f"Cleveland {tlabel} {name}: {len(data)} points for {len(cats)} dates")
            seen: dict[date, str] = {}
            for pi, (pt, cat) in enumerate(zip(data, cats, strict=True)):
                val = decimal_or_none(pt.get("value"))
                if val is None:
                    continue
                tip = str(pt.get("tooltext", "")).split("{br}")
                label = tip[1] if len(tip) > 1 and _MD_RE.match(tip[1].strip()) else str(cat["label"])
                if label.strip() != str(cat["label"]).strip():
                    raise FedNowcastError(f"Cleveland {tlabel} {name}: tooltip date {label} != axis {cat['label']}")
                d = clev_date(label, target)
                if kind == "published_history":
                    seen[d] = val
                    continue
                var = CLEV_VARIABLE[(freq, key)]
                fl = vintage_flags(
                    family="clev_inflation_nowcast", vintage=d.isoformat(), available_at=day_after(d),
                    basis="fed_nowcast_publication_day", variable=var, provider_variable=name,
                    estimate_kind="projection", vintage_date_text=label, chart=freq, target_period=tlabel,
                )  # fmt: skip
                _cell(rows, DS_CLEV, f"CLEV_{freq.upper()}_{key.upper()}|v={d.isoformat()}", start, end, val, unit,
                      fl, raw_sha256, {"file": f"nowcast_{freq}.json", "chart": ci, "series": name, "point": pi})  # fmt: skip
                rep.cells += 1
            if kind == "published_history" and seen:
                if len(set(seen.values())) != 1:
                    raise FedNowcastError(f"Cleveland {tlabel} {name}: several actual values {sorted(set(seen.values()))}")
                d = min(seen)
                var = CLEV_VARIABLE[(freq, key)]
                fl = vintage_flags(
                    family="clev_inflation_nowcast", vintage="actual", available_at=day_after(d),
                    basis="fed_nowcast_publication_day", variable=var, provider_variable=name,
                    estimate_kind="published_history", vintage_date_text=d.strftime("%m/%d"), chart=freq,
                    target_period=tlabel, release_day=d.isoformat(),
                    definition="value of the target period as published on its release day, recorded by the Cleveland Fed",
                )  # fmt: skip
                _cell(rows, DS_CLEV, f"CLEV_{freq.upper()}_{key.upper()}_ACTUAL|v=actual", start, end, seen[d], unit,
                      fl, raw_sha256, {"file": f"nowcast_{freq}.json", "chart": ci, "series": name})  # fmt: skip
                rep.cells += 1
    rep.notes[f"{freq}_charts"] = len(charts)
    rep.notes[f"{freq}_generated"] = gen
    return rep


# ----------------------------------------------------------------------------------------------- Philadelphia Fed
def rt_vintage(col: str, root: str) -> tuple[int, int] | None:
    m = _RT_COL.match(col.strip())
    if not m or m.group(1) != root:
        return None
    yy, mm = int(m.group(2)), int(m.group(3))
    if not 1 <= mm <= 12:
        return None
    return (1900 + yy if yy >= 64 else 2000 + yy), mm


def parse_rtdsm_monthly(body: bytes, *, file: str, raw_sha256: str, rows: Rows) -> Report:
    root, var, unit = RTDSM_MONTHLY[file]
    rep = Report()
    try:
        wb = Workbook(body)
    except XlsxError as e:
        raise FedNowcastError(f"{file} unreadable: {e}") from e
    sheets = list(wb._sheet_targets())
    t = wb.table(sheets[0])
    head = [str(h or "").strip() for h in t[0]]
    if head[0] != "DATE":
        raise FedNowcastError(f"{file}: first header {head[0]!r} != 'DATE'")
    cols: dict[int, tuple[int, int]] = {}
    for i, h in enumerate(head[1:], start=1):
        v = rt_vintage(h, root)
        if v is None:
            if h:
                raise FedNowcastError(f"{file}: unexpected column {h!r}")
            continue
        if v >= FIRST_VINTAGE:
            cols[i] = v
    for n, r in enumerate(t[1:], start=2):
        m = _RT_ROW.match(str(r[0] or "").strip())
        if not m:
            rep.skipped["non_date_rows"] += 1
            continue
        oy, om = int(m.group(1)), int(m.group(2))
        if (oy, om) < FIRST_OBS:
            continue
        s, e = month_bounds(oy, om)
        for i, (vy, vm) in cols.items():
            if i >= len(r):
                continue
            val = decimal_or_none(r[i])
            if val is None:
                continue
            lab = f"{vy}M{vm:02d}"
            fl = vintage_flags(
                family="philfed_rtdsm_monthly", vintage=lab, available_at=first_of_month_after(vy, vm, 1),
                basis="rtdsm_monthly_vintage_month_end", variable=var, provider_variable=root,
                estimate_kind="published_history", column=head[i], file=file,
            )  # fmt: skip
            _cell(rows, DS_RTDSM, f"{root}|v={lab}", s, e, val, unit, fl, raw_sha256,
                  {"file": file, "row": n, "column": head[i]})  # fmt: skip
            rep.cells += 1
    rep.notes[file] = {"vintages": len(cols)}
    return rep


def parse_release_values(body: bytes, *, raw_sha256: str, rows: Rows) -> Report:
    rep = Report()
    try:
        wb = Workbook(body)
    except XlsxError as e:
        raise FedNowcastError(f"{RELEASE_FILE} unreadable: {e}") from e
    notes = " ".join(str(c) for r in wb.table("NOTES") for c in r if c)
    if "Variable ID: ROUTPUT" not in notes or "Q/Q Growth (Annual Rate, Percentage Points)" not in notes:
        raise FedNowcastError(f"{RELEASE_FILE}: NOTES changed")
    mu = _UPDATED.search(notes)
    updated = datetime.strptime(mu.group(1), "%B %d, %Y").date() if mu else None
    t = wb.table("DATA")
    hi = next((i for i, r in enumerate(t) if r and str(r[0] or "").strip() == "Date"), None)
    if hi is None or [str(c or "").strip() for c in t[hi][:5]] != ["Date", "First", "Second", "Third", "Most_Recent"]:
        raise FedNowcastError(f"{RELEASE_FILE}: header changed")
    unit = "percent change at an annual rate (real GDP, Q/Q)"
    for n, r in enumerate(t[hi + 1 :], start=hi + 2):
        m = _QC_RE.match(str(r[0] or "").strip())
        if not m:
            continue
        y, q = int(m.group(1)), int(m.group(2))
        s, e = quarter_bounds(y, q)
        for j, lab in enumerate(("first", "second", "third", "most_recent"), start=1):
            val = decimal_or_none(r[j]) if j < len(r) else None
            if val is None:
                continue
            if lab == "most_recent":
                if updated is None:
                    avail, basis = None, None
                else:
                    avail, basis = day_after(updated), "philfed_file_last_updated"
            elif lab == "first":
                avail, basis = first_of_month_after(y, 3 * q, 4), "bea_release_fourth_month_bound"
            else:
                avail, basis = first_of_month_after(y, 3 * q, 6), "bea_release_sixth_month_bound"
            fl = vintage_flags(
                family="philfed_release_values", vintage=lab if lab != "most_recent" else f"most_recent@{updated}",
                available_at=avail, basis=basis, variable="real_gdp_growth_q_ann", provider_variable=f"ROUTPUT {lab}",
                estimate_kind="published_history", file=RELEASE_FILE,
            )  # fmt: skip
            _cell(rows, DS_REL, f"ROUTPUT|v={lab}", s, e, val, unit, fl, raw_sha256,
                  {"file": RELEASE_FILE, "sheet": "DATA", "row": n, "column": lab})  # fmt: skip
            rep.cells += 1
    rep.notes["last_updated"] = updated.isoformat() if updated else None
    return rep


# ----------------------------------------------------------------------------------------------- rights / spec
RIGHTS: dict[str, dict[str, Any]] = {
    DS_GDPNOW: {
        "licence": "Federal Reserve Bank of Atlanta: reproduction not for commercial gain, attribution required",
        "licence_url": "https://www.atlantafed.org/terms-of-use",
        "terms_quote": (
            "The Bank hereby grants permission to reproduce Bank-owned content (print and digital) offered through our "
            "website without limitation as to number, provided that such reproductions are not for commercial gain or "
            "monetary profit and that appropriate attribution to the Federal Reserve Bank of Atlanta is made on all such "
            "reproductions."
        ),
        "non_commercial": True,
        "attribution": "Federal Reserve Bank of Atlanta, GDPNow",
    },
    DS_NYFED: {
        "licence": "Federal Reserve Bank of New York Terms of Use: licence to use, copy and distribute (no endorsement)",
        "licence_url": "https://www.newyorkfed.org/privacy/termsofuse",
        "terms_quote": (
            "The New York Fed grants you a non-exclusive license, subject to the Terms, to use, copy, and distribute "
            "Content for your personal or business purposes."
        ),
        "non_commercial": False,
        "attribution": "Federal Reserve Bank of New York, Staff Nowcast",
    },
    DS_CLEV: {
        "licence": "Federal Reserve Bank of Cleveland: noncommercial, personal or educational use, with credit",
        "licence_url": "https://www.clevelandfed.org/privacy",
        "terms_quote": (
            "you are authorized to reproduce Federal Reserve Bank of Cleveland publications, or portions thereof, "
            "appearing on the Bank's Website without limitation as to number so long as both of the following apply: "
            "The information is used for noncommercial, personal, or educational purposes only (i.e., not for private "
            "gain) The information is appropriately credited to the Federal Reserve Bank of Cleveland"
        ),
        "non_commercial": True,
        "attribution": "Federal Reserve Bank of Cleveland, Inflation Nowcasting",
    },
    DS_RTDSM: {
        "licence": "no data licence stated; footer 'All rights reserved. Federal Reserve Bank of Philadelphia'",
        "licence_url": "https://www.philadelphiafed.org/surveys-and-data/real-time-data-research",
        "terms_quote": "Copyright 2026. All rights reserved. Federal Reserve Bank of Philadelphia",
        "restricted": True,
        "attribution": "Federal Reserve Bank of Philadelphia, Real-Time Data Set for Macroeconomists",
    },
    DS_REL: {
        "licence": "no data licence stated; footer 'All rights reserved. Federal Reserve Bank of Philadelphia'",
        "licence_url": "https://www.philadelphiafed.org/surveys-and-data/real-time-data-research/first-second-third",
        "terms_quote": "Copyright 2026. All rights reserved. Federal Reserve Bank of Philadelphia",
        "restricted": True,
        "attribution": "Federal Reserve Bank of Philadelphia, First-, Second-, and Third-Release Values (source: BEA)",
    },
}


def rights(ds: str) -> dict[str, Any]:
    return {**RIGHTS[ds], "licence_checked": "2026-10-17", "source_key": SOURCE_KEY}


def code_digest() -> str:
    return module_digest(sys.modules[__name__])


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=NAME,
        version=VERSION,
        source_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider=(
            "Federal Reserve Banks of Atlanta (GDPNow), New York (Staff Nowcast), Cleveland (inflation nowcasting) and "
            "Philadelphia (Real-Time Data Set, first/second/third releases)"
        ),
        licence="per table: Atlanta and Cleveland non-commercial; New York licence; Philadelphia all rights reserved",
        allowed_hosts=(ATL_HOST, NY_HOST, CLE_HOST, PHL_HOST),
        datasets=DATASETS,
        dataset_ids=("GDPTrackingModelDataAndForecasts", "new-york-fed-staff-nowcast", "nowcast_month",
                     "nowcast_quarter", *RTDSM_MONTHLY, RELEASE_FILE),  # fmt: skip
        cadence_seconds=7 * 86_400,
        planned_requests=12,
        max_response_bytes=MAX_BYTES,
        min_interval_seconds=2.0,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(),
        field_mappings={"vintage_families": ["atl_gdpnow", "nyfed_nowcast", "clev_inflation_nowcast",
                                             "philfed_rtdsm_monthly", "philfed_release_values"]},  # fmt: skip
        notes="Official nowcast histories (each update keyed by publication date) + Philadelphia Fed real-time data.",
    )


async def _get(run: Any, url: str, key: str, media: tuple[str, ...]) -> tuple[bytes, str]:
    raw = await run.get(url, key=key, media_types=media, max_bytes=MAX_BYTES)
    sha = run.archive(raw)
    return raw.body, sha


async def atlas_ingest(run: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"tables": {}, "reports": {}}

    def register(ds: str, rows: Rows, shas: list[str], note: str, rep: dict[str, Any]) -> None:
        frame = rows.frame()
        cov = coverage_line(frame)
        reg = run.register(dataset=ds, frame=frame, raw_sha256s=shas, coverage_note=(note + f" Coverage: {cov}.")[:4000],
                           rights=rights(ds))  # fmt: skip
        out["tables"][ds] = {"dataset_key": reg["dataset_key"], "rows": reg["row_count"], "created": reg["created"],
                             "coverage": cov}  # fmt: skip
        out["reports"][ds] = rep

    if run.wants(DS_GDPNOW):
        body, sha = await _get(run, GDPNOW_URL, "gdpnow:workbook", XLSX_MEDIA)
        rows = Rows()
        rep = parse_gdpnow(body, raw_sha256=sha, rows=rows)
        del body
        register(DS_GDPNOW, rows, [sha], "Atlanta Fed GDPNow archives (each update by Forecast Date; deep archive "
                 "2011Q4-2014Q1 made before the public launch) + current quarter; BEA advance estimates as recorded.",
                 {"cells": rep.cells, **rep.notes, "skipped": dict(rep.skipped)})  # fmt: skip
    if run.wants(DS_NYFED):
        body, sha = await _get(run, NYFED_URL, "nyfed:workbook", XLSX_MEDIA)
        rows = Rows()
        rep = parse_nyfed(body, raw_sha256=sha, rows=rows)
        register(DS_NYFED, rows, [sha], "New York Fed Staff Nowcast, Forecasts By Quarter (weekly). Quarters before "
                 "2016Q1 are historical reconstructions: loaded without availability (never selected as of a date).",
                 {"cells": rep.cells, "skipped": dict(rep.skipped)})  # fmt: skip
    if run.wants(DS_CLEV):
        rows, shas, notes, sk = Rows(), [], {}, Counter()
        for freq in ("month", "quarter"):
            body, sha = await _get(run, CLEV_URL.format(freq=freq), f"cleveland:{freq}", JSON_MEDIA)
            shas.append(sha)
            rep = parse_cleveland(body, freq=freq, raw_sha256=sha, rows=rows)
            del body
            notes.update(rep.notes)
            sk.update(rep.skipped)
        register(DS_CLEV, rows, shas, "Cleveland Fed inflation nowcasting (daily, month-over-month and quarterly "
                 "annualized; CPI, core CPI, PCE, core PCE) and the actual values as published on release days.",
                 {"cells": len(rows), **notes, "skipped": dict(sk)})  # fmt: skip
    if run.wants(DS_RTDSM):
        rows, shas, notes = Rows(), [], {}
        for file in RTDSM_MONTHLY:
            body, sha = await _get(run, PHL_XLSX.format(file=file), f"rtdsm_monthly:{file}", XLSX_MEDIA)
            shas.append(sha)
            rep = parse_rtdsm_monthly(body, file=file, raw_sha256=sha, rows=rows)
            del body
            notes.update(rep.notes)
        register(DS_RTDSM, rows, shas, f"Philadelphia Fed RTDSM monthly vintages from {FIRST_VINTAGE[0]}M"
                 f"{FIRST_VINTAGE[1]:02d}, observations from {FIRST_OBS[0]}-{FIRST_OBS[1]:02d}; vintage usable from the "
                 "1st of the following month.", {"cells": len(rows), **notes})  # fmt: skip
    if run.wants(DS_REL):
        body, sha = await _get(run, PHL_XLSX.format(file=RELEASE_FILE), "release_values:routput", XLSX_MEDIA)
        rows = Rows()
        rep = parse_release_values(body, raw_sha256=sha, rows=rows)
        register(DS_REL, rows, [sha], "Philadelphia Fed real GDP first/second/third/most-recent release values "
                 "(annualized Q/Q growth).", {"cells": rep.cells, **rep.notes})  # fmt: skip
    return out


__all__ = [
    "NAME",
    "FedNowcastError",
    "atlas_ingest",
    "atlas_source",
    "clev_date",
    "clev_target",
    "excel_date",
    "parse_cleveland",
    "parse_gdpnow",
    "parse_nyfed",
    "parse_release_values",
    "parse_rtdsm_monthly",
    "rt_vintage",
]
