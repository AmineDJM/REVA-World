"""Shared conventions of the vintage (real-time) atlas tables (V5, agent PRED-VINTAGE).

A *vintage table* is an ordinary atlas table (fixed long schema, :mod:`reva.atlas.tables`) whose
cells are values **as published in one vintage** of a provider dataset: an IMF World Economic
Outlook database, a Philadelphia Fed real-time data set column, a survey round of professional
forecasters, an OECD Main Economic Indicators edition. The atlas key (indicator, provider_geo,
period) must be unique, so the vintage is folded into the indicator: ``<provider variable>|v=<vintage>``
(``NGDP_RPCH|v=2008-04``, ``ROUTPUT|v=08Q1``, ``RGDP|v=2008Q1``).

Every vintage cell carries, in ``flags`` (all keys always present, ``null`` when unknown):

``family``              the vintage family (:data:`FAMILIES`): the as-of series key is (family, variable,
                        provider_geo, period)
``family``              the vintage family (:data:`FAMILIES`); the as-of series key is (family, variable,
                        provider_geo, period)
``vintage``             the provider's vintage label, verbatim (``2008-04``, ``ROUTPUT08Q1``, ``1990 Q3``,
                        ``200502``, ``2008-Q1``)
``vintage_date_text``   the publication date as the provider states it, verbatim (``8/31/90*``,
                        ``2025-10-14T13:00:00Z``) or ``null`` when the provider states none
``available_at``        ISO-8601 UTC instant at/after which REVA asserts the vintage was public. Derived by
                        the rule named in ``availability_basis`` and always *conservative* (never earlier than
                        the evidence allows); ``null`` = no proof (the cell is then excluded from every
                        as-of selection, never guessed)
``availability_basis``  the rule id (:data:`AVAILABILITY_RULES`, quoted with its evidence in the report)
``variable``            REVA vintage variable id (:data:`VARIABLES`): the concept *of this source*; two
                        sources' cells with the same id are comparable only when their definitions agree
``provider_variable``   the provider's code, verbatim
``estimate_kind``       ``projection`` (a forecast or survey expectation at that vintage), ``actual`` (WEO cell
                        at/before the series' declared latest actual year), ``published_history`` (a data value
                        as then published), ``published_history_boundary_unknown`` (a WEO value before the
                        vintage year whose estimate/actual boundary the copy does not state)

Projections are never observations: the vintage accessor (:mod:`reva.evaluation.vintages`) returns them
only when asked for forecasts, and the atlas answer layer excludes ``estimate_kind = projection``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

import polars as pl

from reva.atlas.tables import ATLAS_SCHEMA

DECIMAL_RE = re.compile(r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][-+]?[0-9]{1,3})?$")
USER_AGENT_NOTE = "generic project User-Agent of the atlas client (no personal data)"

#: Availability rules: id -> (statement, evidence). Every rule is conservative: it never dates a vintage
#: earlier than its evidence allows. Quoted in docs/dev/v5_pred_vintage.md.
AVAILABILITY_RULES: dict[str, str] = {
    "imf_publication_date_attribute": (
        "IMF SDMX dataset attribute PUBLICATION_DATE (an instant) of the WEO vintage dataflow, used as is"
    ),
    "weo_release_month_end": (
        "IMF WEO vintage labelled YYYY-MM (the release month of the WEO database file); the day is not in "
        "the copy, so available_at = first instant of the following month (UTC)"
    ),
    "rtdsm_middle_month_end": (
        "Philadelphia Fed RTDSM documentation: quarterly vintages 'represent the data as they were known in "
        "the middle month of each quarter (February, May, August, and November)'; available_at = first "
        "instant of the month after that middle month (UTC)"
    ),
    "spf_news_release_date": (
        "Philadelphia Fed spf-release-dates.txt 'News Release Date' of the survey; available_at = the day "
        "after the release date, 00:00 UTC (end of the release day)"
    ),
    "ecb_spf_round_quarter_end": (
        "ECB SPF survey round YYYY-Qn (conducted and published within that quarter); the publication day is "
        "not in the data, so available_at = first instant of the next quarter (UTC)"
    ),
    "oecd_mei_edition_month_end": (
        "OECD STES revisions EDITION YYYYMM (monthly MEI edition, CL_EDITION name e.g. 'February 1999'); the "
        "day is not in the data, so available_at = first instant of the following month (UTC)"
    ),
    "steo_archive_release_date": (
        "EIA STEO Archives page 'Release Date' (MM/DD/YYYY) of the issue, the archived file's Last-Modified "
        "header being on/before that day; available_at = the day after the release date, 00:00 UTC"
    ),
    "steo_archive_file_last_modified": (
        "EIA STEO archive file whose Last-Modified header is after the issue's release date (the file may have "
        "been replaced after release): available_at = the first instant of the day after Last-Modified (UTC)"
    ),
    "wasde_csv_release_date": (
        "USDA OCE Historical WASDE Report Data CSV: 'ReleaseDate' column of the report (WASDE is released at 12:00 "
        "Eastern time; the CSV states no time zone), the archived file's Last-Modified header being on/before that "
        "day; available_at = the day after the release date, 00:00 UTC"
    ),
    "wasde_csv_file_last_modified": (
        "USDA OCE WASDE CSV whose Last-Modified header is after the report's ReleaseDate (posted or replaced after "
        "release, e.g. the '-V2' files): available_at = the first instant of the day after Last-Modified (UTC)"
    ),
    "fed_nowcast_publication_day": (
        "Federal Reserve Bank nowcast file (Atlanta Fed GDPNow archives 'Forecast Date', New York Fed Staff Nowcast "
        "'Forecast Date', Cleveland Fed inflation nowcasting chart date label): the value was published on that day "
        "(no time stated); available_at = the day after, 00:00 UTC (end of the publication day)"
    ),
    "rtdsm_monthly_vintage_month_end": (
        "Philadelphia Fed RTDSM monthly vintages yyMmm: 'the observations available in June 1989 are those given in "
        "the column, EMPLOY89M6' (vintage day = the agency's release day within that month, not stated); "
        "available_at = first instant of the following month (UTC)"
    ),
    "bea_release_recorded_by_atlanta_fed": (
        "Atlanta Fed GDPNow archives 'Publication Date of Advance Estimate' of the quarter (BEA's release day as "
        "recorded by the Atlanta Fed); available_at = the day after, 00:00 UTC"
    ),
    "bea_release_fourth_month_bound": (
        "upper bound for BEA's first (advance/initial) GDP estimate whose release day is not stated: every first "
        "estimate since 1996 (incl. the 2013, 2018-19 and 2025 shutdown delays) was released within three months "
        "after the quarter; available_at = first instant of the fourth month after the quarter (UTC)"
    ),
    "bea_release_sixth_month_bound": (
        "upper bound for BEA's second/third GDP estimates whose release day is not stated (normally within three "
        "months; shutdown-delayed releases within five); available_at = first instant of the sixth month after the "
        "quarter (UTC)"
    ),
    "philfed_file_last_updated": (
        "Philadelphia Fed first/second/third-release file NOTES 'Last updated on: <date>'; available_at = the day "
        "after, 00:00 UTC (the 'Most_Recent' column is the value in the latest vintage at that date)"
    ),
}

#: REVA vintage variables: id -> definition (the source's own definition is quoted per cell source in
#: the atlas table coverage note and in docs/dev/v5_pred_vintage.md).
VARIABLES: dict[str, str] = {
    "real_gdp_growth": "real GDP growth, percent change (annual: year on year; quarterly: as defined by the source)",
    "cpi_inflation": "consumer price inflation, percent change (annual average unless stated)",
    "cpi_inflation_eop": "consumer price inflation, end of period, percent change",
    "unemployment_rate": "unemployment rate, percent of labour force",
    "current_account_gdp": "current account balance, percent of GDP",
    "gg_gross_debt_gdp": "general government gross debt, percent of GDP",
    "real_gdp_level": "real GDP (volume) level, source units",
    "nominal_gdp_level": "nominal GDP level, source units",
    "gdp_price_index": "GDP price index (deflator) level, source base year",
    "cpi_level": "consumer price index level, source base",
    "real_gdp_growth_q_ann": "real GDP growth, quarter on quarter, annualised percent (SPF)",
    "nominal_gdp_growth_q_ann": "nominal GDP growth, quarter on quarter, annualised percent (SPF)",
    "gdp_price_inflation_q_ann": "GDP price index inflation, quarter on quarter, annualised percent (SPF)",
    "cpi_inflation_q_ann": "CPI inflation, quarter on quarter, annualised percent (SPF)",
    "cpi_inflation_q4q4": "CPI inflation, fourth quarter over fourth quarter, percent (SPF annual horizons)",
    "core_cpi_inflation_q4q4": "core CPI inflation, Q4 over Q4, percent (SPF annual horizons)",
    "pce_inflation_q4q4": "PCE price inflation, Q4 over Q4, percent (SPF annual horizons)",
    "core_pce_inflation_q4q4": "core PCE price inflation, Q4 over Q4, percent (SPF annual horizons)",
    "core_cpi_inflation_q_ann": "core CPI inflation, quarter on quarter, annualised percent (SPF)",
    "pce_inflation_q_ann": "PCE price inflation, quarter on quarter, annualised percent (SPF)",
    "core_pce_inflation_q_ann": "core PCE price inflation, quarter on quarter, annualised percent (SPF)",
    "tbill_3m_rate": "3-month Treasury bill rate, percent (SPF)",
    "tbond_10y_rate": "10-year Treasury bond rate, percent (SPF)",
    "industrial_production_index": "industrial production index (SPF)",
    "housing_starts": "housing starts, millions of units, annual rate (SPF)",
    "nonfarm_payroll_employment": "nonfarm payroll employment, thousands (SPF)",
    "hicp_inflation": "HICP inflation, year-on-year percent (ECB SPF)",
    "cpi_inflation_m": "CPI-U inflation, month on month, percent, seasonally adjusted (Cleveland Fed nowcasts)",
    "core_cpi_inflation_m": "core CPI-U inflation (less food and energy), month on month, percent, SA",
    "pce_inflation_m": "PCE price index inflation, month on month, percent, SA",
    "core_pce_inflation_m": "core PCE price index inflation, month on month, percent, SA",
    "aggregate_weekly_hours_index": "index of aggregate weekly hours, total private, SA (Philadelphia Fed RTDSM 'H')",
    "real_pce_monthly_level": "real personal consumption expenditures, monthly, SA (Philadelphia Fed RTDSM 'RCONM')",
}

#: Namespaced variable ids: prefix -> meaning. ``eia_steo:<CODE>`` = the EIA STEO series code (upper case; EIA's
#: codes are case-insensitive: the workbooks write ``papr_world``, the bulk file ``STEO.PAPR_WORLD.M``); the
#: definition is EIA's row label and table heading, quoted per cell in ``flags.series_label`` / ``flags.table``.
VARIABLE_PREFIXES: dict[str, str] = {
    "eia_steo:": "EIA Short-Term Energy Outlook series, by EIA series code (label quoted in each cell's flags)",
    "usda_wasde:": (
        "USDA WASDE line, by REVA slug of the report table title / commodity / attribute (each label quoted verbatim "
        "in the cell's flags)"
    ),
}


def known_variable(variable: str) -> bool:
    return variable in VARIABLES or any(variable.startswith(p) and len(variable) > len(p) for p in VARIABLE_PREFIXES)


ESTIMATE_KINDS = ("projection", "actual", "published_history", "published_history_boundary_unknown")


def utc_midnight(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=UTC)


def first_of_next_month(year: int, month: int) -> datetime:
    return utc_midnight(date(year + (month == 12), 1 if month == 12 else month + 1, 1))


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        raise ValueError("available_at must be timezone-aware")
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def day_after(d: date) -> datetime:
    return utc_midnight(d + timedelta(days=1))


#: Vintage families: cells of one family form one vintage sequence (the accessor's series key is
#: (family, variable, provider_geo, period)); two datasets of one family are copies of one provider series.
FAMILIES: dict[str, str] = {
    "imf_weo": "IMF World Economic Outlook database releases (April / October)",
    "philfed_rtdsm": "Philadelphia Fed Real-Time Data Set for Macroeconomists (quarterly vintages)",
    "philfed_spf": "Philadelphia Fed Survey of Professional Forecasters (median responses per survey)",
    "ecb_spf": "ECB Survey of Professional Forecasters (average of point forecasts per round)",
    "oecd_mei_rt": "OECD Main Economic Indicators original release data and revisions (monthly editions)",
    "eia_steo": "EIA Short-Term Energy Outlook monthly issues (archived Excel data files of each release)",
    "usda_wasde": "USDA World Agricultural Supply and Demand Estimates monthly reports (OCE historical CSV per report)",
    "atl_gdpnow": "Atlanta Fed GDPNow model nowcasts of current-quarter real GDP growth (one vintage per update date)",
    "nyfed_nowcast": "New York Fed Staff Nowcast of real GDP growth (one vintage per weekly forecast date)",
    "clev_inflation_nowcast": "Cleveland Fed inflation nowcasting (CPI, core CPI, PCE, core PCE; one vintage per day)",
    "philfed_rtdsm_monthly": "Philadelphia Fed Real-Time Data Set, monthly vintages of monthly indicators",
    "philfed_release_values": "Philadelphia Fed first-, second-, third-release and most-recent values (BEA GDP)",
}


def vintage_flags(
    *,
    family: str,
    vintage: str,
    available_at: datetime | None,
    basis: str | None,
    variable: str,
    provider_variable: str,
    estimate_kind: str,
    vintage_date_text: str | None = None,
    **extra: Any,
) -> str:
    """Canonical flags JSON of one vintage cell (see the module docstring)."""
    if family not in FAMILIES:
        raise ValueError(f"unknown vintage family {family!r}")
    if not known_variable(variable):
        raise ValueError(f"unknown vintage variable {variable!r}")
    if estimate_kind not in ESTIMATE_KINDS:
        raise ValueError(f"unknown estimate_kind {estimate_kind!r}")
    if (available_at is None) != (basis is None):
        raise ValueError("available_at and availability_basis go together (both or neither)")
    if basis is not None and basis not in AVAILABILITY_RULES:
        raise ValueError(f"unknown availability rule {basis!r}")
    obj = {
        "family": family,
        "vintage": vintage,
        "vintage_date_text": vintage_date_text,
        "available_at": iso(available_at),
        "availability_basis": basis,
        "variable": variable,
        "provider_variable": provider_variable,
        "estimate_kind": estimate_kind,
        **{k: v for k, v in extra.items() if v is not None},
    }
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def indicator(provider_variable: str, vintage: str) -> str:
    return f"{provider_variable}|v={vintage}"


def decimal_or_none(v: Any) -> str | None:
    """A strict decimal string from a provider number, or ``None`` (never a filled-in value)."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if v != v or v in (float("inf"), float("-inf")):
            return None
        s = repr(v)
    else:
        s = str(v).strip()
    if "e" in s or "E" in s:
        from decimal import Decimal, InvalidOperation

        try:
            d = Decimal(s)
        except InvalidOperation:
            return None
        if not d.is_finite():
            return None
        s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    if s in ("-0", "", "-"):
        s = "0" if s == "-0" else s
    return s if DECIMAL_RE.match(s) else None


class Rows:
    """Column buffers for an atlas frame with explicit period dates."""

    def __init__(self) -> None:
        self.c: dict[str, list[Any]] = {k: [] for k in ATLAS_SCHEMA.names()}

    def __len__(self) -> int:
        return len(self.c["indicator"])

    def add(
        self,
        *,
        source: str,
        dataset: str,
        indicator: str,
        provider_geo: str,
        start: date,
        end: date,
        value: str,
        unit: str,
        flags: str,
        raw_sha256: str,
        selector: Mapping[str, Any],
        iso3: str | None = None,
    ) -> None:
        c = self.c
        c["source"].append(source)
        c["dataset"].append(dataset)
        c["indicator"].append(indicator)
        c["concept_id"].append(None)
        c["iso3"].append(iso3)
        c["provider_geo"].append(provider_geo)
        c["entity_qid"].append(None)
        c["period_start"].append(start)
        c["period_end"].append(end)
        c["value"].append(value)
        c["value_text"].append(None)
        c["unit"].append(unit[:100])
        c["flags"].append(flags)
        c["raw_sha256"].append(raw_sha256)
        c["selector"].append(json.dumps(dict(selector), sort_keys=True, separators=(",", ":"), default=str))

    def frame(self) -> pl.DataFrame:
        return pl.DataFrame(self.c, schema=ATLAS_SCHEMA)


def year_bounds(y: int) -> tuple[date, date]:
    return date(y, 1, 1), date(y + 1, 1, 1)


def quarter_bounds(y: int, q: int) -> tuple[date, date]:
    m0 = 3 * (q - 1) + 1
    return date(y, m0, 1), (date(y + 1, 1, 1) if q == 4 else date(y, m0 + 3, 1))


def month_bounds(y: int, m: int) -> tuple[date, date]:
    return date(y, m, 1), (date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1))


def coverage_line(frame: pl.DataFrame) -> dict[str, Any]:
    """Small coverage summary (vintages, variables, geographies) of a vintage frame."""
    if frame.height == 0:
        return {"rows": 0}
    f = frame.select(
        pl.col("indicator").str.split("|v=").list.get(1).alias("v"),
        pl.col("indicator").str.split("|v=").list.get(0).alias("var"),
        "provider_geo",
        "period_start",
    )
    vs = sorted(f["v"].unique().to_list())
    return {
        "rows": frame.height,
        "vintages": len(vs),
        "first_vintage": vs[0],
        "last_vintage": vs[-1],
        "provider_variables": sorted(f["var"].unique().to_list()),
        "geographies": f["provider_geo"].n_unique(),
        "period_min": str(f["period_start"].min()),
        "period_max": str(f["period_start"].max()),
    }


def check_known_keys(found: Sequence[str], allowed: Sequence[str], what: str) -> None:
    bad = sorted(set(found) - set(allowed))
    if bad:
        raise ValueError(f"unexpected {what}: {bad[:10]}")
