"""Monthly inputs for BEAT-MACRO with explicit, conservative publication rules.

Two kinds of inputs:

* **Provider vintages** (IMF WEO, ECB SPF, OECD MEI real-time editions) are read through
  :class:`reva.evaluation.vintages.VintageAccessor` (``available_then``; leakage guard) in ``cases``.
* **Final-vintage monthly series that are not revised by convention** — headline CPI indices (IMF CPI database),
  the euro-area HICP index (Eurostat) and the Brent spot price (EIA, a market price) — are read from today's atlas
  table with a declared **publication-lag rule**: month ``m`` of country ``c`` is usable from the first instant
  of month ``m + lag[c]``. The lag is ``max(min_lag, gap_c + extra)``, where ``gap_c`` is the number of months
  between the table's recording month and the country's last published month (today's reporting delay, as
  evidence) plus a safety margin. Inputs read this way are labelled ``final_vintage_with_publication_lag``: the
  claim "as published then" rests on the non-revision of headline CPI/HICP (checked on the G7 against the OECD
  MEI real-time editions, see ``service.cpi_revision_check``), never on a vintage.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import polars as pl

LABEL_LAGGED = "final_vintage_with_publication_lag"


def mi(d: date | datetime) -> int:
    """Month index (year * 12 + month - 1)."""
    return d.year * 12 + d.month - 1


def month_start(m: int) -> datetime:
    return datetime(m // 12, m % 12 + 1, 1, tzinfo=UTC)


class LookAheadError(RuntimeError):
    """A lagged input would have been read before its publication rule allows (must never happen)."""


@dataclass(frozen=True)
class LaggedMonthly:
    """geo -> month index -> value, each geo with a publication lag in months."""

    name: str
    values: Mapping[str, Mapping[int, float]]
    lag: Mapping[str, int]
    rule: str
    recorded_at: datetime | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    label: str = LABEL_LAGGED

    def usable(self, geo: str, t: datetime) -> dict[int, float]:
        """The months of ``geo`` usable at instant ``t`` (month m usable from month_start(m + lag))."""
        lag = self.lag.get(geo)
        if lag is None:
            return {}
        cut = mi(t) - lag
        out = {m: v for m, v in self.values.get(geo, {}).items() if m <= cut}
        if out and month_start(max(out) + lag) > t:  # defence in depth
            raise LookAheadError(f"{self.name}/{geo}: month {max(out)} read before {month_start(max(out) + lag)}")
        return out


def _lags(values: Mapping[str, Mapping[int, float]], recorded_at: datetime, min_lag: int, extra: int,
          max_gap: int) -> dict[str, int]:  # fmt: skip
    rec = mi(recorded_at)
    out = {}
    for g, s in values.items():
        if not s:
            continue
        gap = rec - max(s)
        if gap <= max_gap:
            out[g] = max(min_lag, gap + extra)
    return out


def _f(v: Any) -> float | None:
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def monthly_from_frame(df: pl.DataFrame, indicator_contains: str, geo_col: str = "iso3") -> dict[str, dict[int, float]]:
    sub = df.filter(pl.col("indicator").str.contains(indicator_contains, literal=True))
    out: dict[str, dict[int, float]] = defaultdict(dict)
    for g, d, v in sub.select(geo_col, "period_start", "value").iter_rows():
        x = _f(v)
        if g is not None and x is not None:
            out[g][mi(d)] = x
    return dict(out)


def imf_cpi(df: pl.DataFrame, recorded_at: datetime) -> LaggedMonthly:
    """IMF CPI database, headline index (``CPI|coicop_1999=_T|type_of_transformation=IX``), by ISO3."""
    vals = monthly_from_frame(df, "type_of_transformation=IX", "iso3")
    vals = {g: s for g, s in vals.items() if all(x > 0 for x in s.values())}
    return LaggedMonthly(
        "imf.cpi_m", vals, _lags(vals, recorded_at, 3, 2, 12),
        "month m usable from the 1st of m + max(3, gap + 2); gap = months between the table's recording month and "
        "the country's last published month; countries with gap > 12 excluded", recorded_at,
    )  # fmt: skip


def eurostat_hicp(df: pl.DataFrame, recorded_at: datetime) -> LaggedMonthly:
    """Eurostat HICP index 2025=100 (``prc_hicp_minr|unit=I25|coicop18=TOTAL``), by Eurostat geo code."""
    vals = monthly_from_frame(df, "unit=I25", "provider_geo")
    return LaggedMonthly(
        "eurostat.hicp_m", vals, _lags(vals, recorded_at, 2, 1, 12),
        "month m usable from the 1st of m + max(2, gap + 1) (final HICP is published mid m+1)", recorded_at,
    )  # fmt: skip


def brent_monthly(df: pl.DataFrame, recorded_at: datetime) -> LaggedMonthly:
    """EIA Brent spot (RBRTE, USD/bbl, daily) -> calendar-month means; month m usable from the 1st of m + 1."""
    sub = df.filter(pl.col("indicator").str.contains("RBRTE", literal=True))
    acc: dict[int, list[float]] = defaultdict(list)
    for d, v in sub.select("period_start", "value").iter_rows():
        x = _f(v)
        if x is not None and x > 0:
            acc[mi(d)].append(x)
    vals = {"WORLD": {m: math.fsum(xs) / len(xs) for m, xs in acc.items() if len(xs) >= 10}}
    return LaggedMonthly("eia.spot_d:RBRTE", vals, {"WORLD": 1},
                         "calendar-month mean of daily Brent spot (>= 10 trading days); usable from the 1st of m + 1",
                         recorded_at)  # fmt: skip


__all__ = [
    "LABEL_LAGGED",
    "LaggedMonthly",
    "LookAheadError",
    "brent_monthly",
    "eurostat_hicp",
    "imf_cpi",
    "mi",
    "month_start",
    "monthly_from_frame",
]
