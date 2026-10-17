"""Real-time (vintage) backtests enabled by the vintages layer (V5, agent PRED-VINTAGE).

Each backtest opens a :class:`reva.evaluation.vintages.VintageAccessor` **at every forecast origin**, so
forecasts and baselines use only what was published by then; outcomes are first releases taken from a later
vintage whose availability instant is reported (``outcome_known_at``): a score is not knowable before it.

* :func:`weo_backtest` — IMF WEO projections (current year h=0, next year h=1) vs a persistence baseline
  (last year's value **as published in the same vintage**), outcome = the target year as published in the
  first WEO vintage available on/after 1 October of the following year (the "October t+1" outturn used in
  the WEO forecast-evaluation literature; when that release is missing the next one is used and counted).
* :func:`spf_backtest` — Philadelphia Fed SPF median real GDP growth forecasts (annualised q/q, horizons
  2..6) vs a no-change baseline (the latest quarterly growth in the RTDSM vintage of the survey quarter, i.e. the
  panellists' information set — FIX7; ``baseline_vintage="available_then"`` gives the V5 one-quarter-staler rule),
  outcome = the first-release growth (the first RTDSM vintage holding the quarter; growth computed
  inside that vintage).

No probability is produced; these are point-forecast accuracy comparisons (MAE, RMSE) with counts.
"""

from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import polars as pl

from reva.evaluation.cutoff import CutoffMode
from reva.evaluation.vintages import VintageAccessor

LABEL = "real_time_vintage_backtest"
HONESTY = (
    "Real-time backtest on provider vintages: at each origin only vintages whose evidenced, conservative "
    "availability instant is at/before the origin are used (available_then reconstruction from archived "
    "vintages; REVA itself did not exist at these origins). Outcomes are first releases from later vintages."
)


def _f(v: str | None) -> float | None:
    if v is None:
        return None
    x = float(Decimal(v))
    return x if math.isfinite(x) else None


def _scores(pairs: list[tuple[float, float, float]]) -> dict[str, Any]:
    """pairs of (forecast, baseline, outcome)."""
    n = len(pairs)
    if not n:
        return {"n": 0}
    ef = [f - y for f, _b, y in pairs]
    eb = [b - y for _f, b, y in pairs]
    mae_f = sum(abs(e) for e in ef) / n
    mae_b = sum(abs(e) for e in eb) / n
    return {
        "n": n,
        "mae_forecast": round(mae_f, 4),
        "mae_baseline": round(mae_b, 4),
        "rmse_forecast": round(math.sqrt(sum(e * e for e in ef) / n), 4),
        "rmse_baseline": round(math.sqrt(sum(e * e for e in eb) / n), 4),
        "relative_mae": round(mae_f / mae_b, 4) if mae_b else None,
        "share_forecast_closer": round(sum(abs(a) < abs(b) for a, b in zip(ef, eb, strict=True)) / n, 4),
        "mean_error_forecast": round(sum(ef) / n, 4),
        # medians: robust to hyperinflation / collapse outliers that dominate means in cross-country panels
        "median_ae_forecast": round(statistics.median(abs(e) for e in ef), 4),
        "median_ae_baseline": round(statistics.median(abs(e) for e in eb), 4),
        # FIX7 (QA-PRED X6): ratio of medians, the headline for pooled inflation panels (hyperinflation dominates means)
        "relative_median_ae": (
            round(statistics.median(abs(e) for e in ef) / statistics.median(abs(e) for e in eb), 4)
            if statistics.median(abs(e) for e in eb)
            else None
        ),
    }


#: variables whose pooled cross-country means are dominated by hyperinflation episodes: headline = medians
MEDIAN_HEADLINE_VARIABLES = frozenset({"cpi_inflation", "inflation", "cpi_inflation_eop", "gdp_deflator_inflation"})


def default_weo_origins(first_year: int = 2008, last_year: int = 2025) -> list[datetime]:
    """1 June (April WEO in force) and 1 December (October WEO in force) of each year."""
    out = []
    for y in range(first_year, last_year + 1):
        out += [datetime(y, 6, 1, tzinfo=UTC), datetime(y, 12, 1, tzinfo=UTC)]
    return out


def weo_backtest(
    cells: pl.DataFrame,
    *,
    variable: str = "real_gdp_growth",
    origins: Sequence[datetime] | None = None,
    horizons: Sequence[int] = (0, 1),
    geos: Sequence[str] | None = None,
) -> dict[str, Any]:
    weo = cells.filter((pl.col("family") == "imf_weo") & (pl.col("variable") == variable))
    if geos is not None:
        weo = weo.filter(pl.col("provider_geo").is_in(list(geos)))
    vint = (
        weo.filter(pl.col("available_at").is_not_null())
        .group_by("vintage")
        .agg(pl.col("available_at").max())
        .sort("available_at")
    )
    vlist = list(vint.iter_rows())
    origins = list(origins or default_weo_origins())
    counts: Counter[str] = Counter()
    pairs: dict[tuple[int, str], list[tuple[float, float, float]]] = defaultdict(list)
    details: list[dict[str, Any]] = []
    outcome_cache: dict[int, tuple[str, datetime, dict[str, float]] | None] = {}

    def outcome(year: int) -> tuple[str, datetime, dict[str, float]] | None:
        if year in outcome_cache:
            return outcome_cache[year]
        floor = datetime(year + 1, 10, 1, tzinfo=UTC)
        cand = [(v, a) for v, a in vlist if a >= floor]
        res = None
        if cand:
            v, a = cand[0]
            if v != f"{year + 1}-10":
                counts["outcome_from_later_vintage"] += 1
            rows = weo.filter(
                (pl.col("vintage") == v)
                & (pl.col("period_start") == date(year, 1, 1))
                & (pl.col("estimate_kind") != "projection")
            )
            vals = {g: x for g, s in rows.select("provider_geo", "value").iter_rows() if (x := _f(s)) is not None}
            res = (v, a, vals)
        outcome_cache[year] = res
        return res

    for t in origins:
        acc = VintageAccessor(weo, t, CutoffMode.available_then)
        y0 = t.year
        inforce = acc.vintage_in_force("imf_weo")
        if inforce is None:
            counts["origin_without_vintage"] += 1
            continue
        base = acc.latest(period_start=date(y0 - 1, 1, 1), period_end=date(y0, 1, 1)).rows
        bvals = {g: x for g, s in base.select("provider_geo", "value").iter_rows() if (x := _f(s)) is not None}
        for h in horizons:
            ty = y0 + h
            fc = acc.forecasts(family="imf_weo", period_start=date(ty, 1, 1), period_end=date(ty + 1, 1, 1)).rows
            oc = outcome(ty)
            if oc is None:
                counts[f"h{h}_no_outcome_vintage_yet"] += fc.height
                continue
            ov, oa, ovals = oc
            for g, s, _fv in fc.select("provider_geo", "value", "vintage").iter_rows():
                f = _f(s)
                if f is None:
                    continue
                if g not in bvals:
                    counts[f"h{h}_no_baseline"] += 1
                    continue
                if g not in ovals:
                    counts[f"h{h}_no_outcome"] += 1
                    continue
                pairs[(h, "june" if t.month < 9 else "december")].append((f, bvals[g], ovals[g]))
            details.append(
                {
                    "origin": t.isoformat(),
                    "vintage_in_force": inforce["vintage"],
                    "horizon": h,
                    "target_year": ty,
                    "outcome_vintage": ov,
                    "outcome_known_at": oa.isoformat(),
                }
            )
    by_h: dict[str, Any] = {}
    for h in horizons:
        allp = pairs[(h, "june")] + pairs[(h, "december")]
        by_h[f"h{h}"] = {
            "all": _scores(allp),
            "june_origins": _scores(pairs[(h, "june")]),
            "december_origins": _scores(pairs[(h, "december")]),
        }
    known = [d["outcome_known_at"] for d in details]
    return {
        "label": LABEL,
        "honesty_statement": HONESTY,
        "backtest": "imf_weo_vs_persistence",
        "variable": variable,
        "design": {
            "forecast": "WEO projection for the target year in the vintage in force at the origin",
            "baseline": "persistence: the previous year's value as published in that same vintage",
            "outcome": "target year as published in the first WEO vintage available on/after 1 Oct of year+1",
            "origins": [o.isoformat() for o in origins],
        },
        "scores": by_h,
        "headline_metric": "relative_median_ae" if variable in MEDIAN_HEADLINE_VARIABLES else "relative_mae",
        "headline_note": (
            "pooled cross-country inflation: means are dominated by hyperinflation episodes (WEO CPI h0 MAE is 10x "
            "persistence while the median AE is 0.38x); quote the ratio of medians, never a pooled-mean ratio"
            if variable in MEDIAN_HEADLINE_VARIABLES
            else None
        ),
        "outcome_vintage_note": "outcomes are the WEO's own October t+1 estimate (same institution; for many "
        "low-capacity countries an IMF staff estimate): flatters the WEO; see QA-PRED X7",
        "counts": dict(counts),
        "scored_as_of": max(known) if known else None,
        "details": details,
    }


def _growth_by_vintage(levels: pl.DataFrame) -> pl.DataFrame:
    """Annualised q/q growth of each quarter inside each vintage (percent)."""
    df = levels.with_columns(pl.col("value").cast(pl.Float64).alias("x")).sort("vintage", "period_start")
    df = df.with_columns(
        pl.col("x").shift(1).over("vintage").alias("x_prev"),
        pl.col("period_start").shift(1).over("vintage").alias("p_prev"),
    )
    df = df.filter(pl.col("x_prev").is_not_null() & (pl.col("p_prev").dt.offset_by("3mo") == pl.col("period_start")))
    return df.with_columns((100 * ((pl.col("x") / pl.col("x_prev")) ** 4 - 1)).alias("growth"))


BASELINE_VINTAGE_RULES = {
    "survey_quarter": "no change: latest quarterly growth in the RTDSM vintage OF THE SURVEY QUARTER (data 'as known "
    "in the middle month' of that quarter, i.e. the information set SPF panellists had at their mid-quarter "
    "deadline) - FIX7, QA-PRED X8: the same information set as the forecasters",
    "available_then": "no change: latest quarterly growth in the RTDSM vintage in force at the release under the "
    "conservative availability rule (vintage counted from the month after its middle month): one quarter staler "
    "than what panellists knew - V5 rule, kept for reproducibility; inflates the SPF's advantage",
}


def spf_backtest(
    cells: pl.DataFrame,
    *,
    horizons: Sequence[str] = ("2", "3", "4", "5", "6"),
    baseline_vintage: str = "survey_quarter",
) -> dict[str, Any]:
    if baseline_vintage not in BASELINE_VINTAGE_RULES:
        raise ValueError(f"unknown baseline_vintage {baseline_vintage!r}")
    spf = cells.filter((pl.col("family") == "philfed_spf") & (pl.col("variable") == "real_gdp_growth_q_ann"))
    rt = cells.filter((pl.col("family") == "philfed_rtdsm") & (pl.col("variable") == "real_gdp_level"))
    g = _growth_by_vintage(rt)
    # first release of each quarter: the earliest vintage holding it (growth computed inside that vintage)
    first = g.sort("available_at").unique(subset=["period_start"], keep="first")
    first_map = {r["period_start"]: (r["growth"], r["available_at"], r["vintage"]) for r in first.to_dicts()}
    surveys = (
        spf.filter(pl.col("available_at").is_not_null())
        .group_by("vintage")
        .agg(pl.col("available_at").first())
        .sort("available_at")
    )
    counts: Counter[str] = Counter()
    pairs: dict[str, list[tuple[float, float, float]]] = defaultdict(list)
    for sv, sa in surveys.iter_rows():
        acc = VintageAccessor(cells, sa, CutoffMode.available_then)
        fc = acc.forecasts(family="philfed_spf", variable="real_gdp_growth_q_ann").rows.filter(pl.col("vintage") == sv)
        if baseline_vintage == "survey_quarter":
            # the RTDSM vintage labelled with the survey quarter (both are 'YYYYQn'); its conservative available_at
            # is after the SPF release, but its content is the data panellists saw (documented RTDSM definition)
            rt_now = rt.filter(pl.col("vintage") == sv)
            if rt_now.height < 2:
                counts["survey_quarter_vintage_missing"] += 1
                continue
        else:
            rt_now = acc.latest(family="philfed_rtdsm", variable="real_gdp_level").rows
        if rt_now.height < 2:
            counts["no_rtdsm_vintage_at_survey"] += 1
            continue
        gg = _growth_by_vintage(rt_now.with_columns(pl.lit("asof").alias("vintage"))).sort("period_start")
        if gg.height == 0:
            counts["no_baseline"] += 1
            continue
        base = float(gg["growth"][-1])
        for r in fc.to_dicts():
            h = r["horizon"]
            if h not in horizons:
                continue
            fo = first_map.get(r["period_start"])
            if fo is None:
                counts[f"h{h}_no_first_release_yet"] += 1
                continue
            pairs[h].append((float(r["value"]), base, float(fo[0])))
    return {
        "label": LABEL,
        "honesty_statement": HONESTY,
        "backtest": "us_spf_median_rgdp_growth_vs_no_change",
        "design": {
            "forecast": "SPF median annualised q/q real GDP growth (medianGrowth.xlsx drgdp2..6) of each survey, "
            "as of its News Release Date",
            "baseline": BASELINE_VINTAGE_RULES[baseline_vintage],
            "baseline_vintage": baseline_vintage,
            "outcome": "first release: growth of the target quarter in the first RTDSM vintage holding it",
        },
        "scores": {f"h{h}": _scores(pairs[h]) for h in horizons},
        "counts": dict(counts),
        "surveys": surveys.height,
    }


__all__ = [
    "BASELINE_VINTAGE_RULES",
    "HONESTY",
    "LABEL",
    "MEDIAN_HEADLINE_VARIABLES",
    "default_weo_origins",
    "spf_backtest",
    "weo_backtest",
]
