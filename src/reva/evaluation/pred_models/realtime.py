"""Forecast cases built origin by origin from provider vintages (V6, agent PRED-MODELS).

Every model input at an origin comes from a :class:`reva.evaluation.vintages.VintageAccessor` opened at that
origin in ``available_then`` mode (only vintages whose evidenced, conservative availability instant is at or
before the origin; every selected cell passes the leakage guard). Outcomes are first releases taken from later
vintages, and each case records ``outcome_known_at`` (the availability instant of the outcome vintage): a
second-pass model may use an error only if it was known at its own origin.

Three tasks:

* ``weo`` — IMF WEO panel (≈190 countries), annual real GDP growth / CPI inflation / unemployment rate, origins
  1 June and 1 December 2008–2025, horizons ``h0`` (current year) and ``h1`` (next year). Benchmarks: the WEO
  projection of the vintage in force (point) and persistence. Outcome: the target year in the first WEO vintage
  available on/after 1 October of the following year (same rule as ``vintage_backtests.weo_backtest``).
* ``us`` — United States, quarterly real GDP growth and GDP-price inflation (annualised q/q, %) and the
  unemployment rate (quarterly mean of monthly), origins = Philadelphia Fed SPF news-release dates (1990Q2+),
  SPF horizons 2..6 (current quarter .. +4). Models see the RTDSM vintage in force at the release (conservative
  rule: one quarter staler than what SPF panellists knew). Outcome: first release in the RTDSM.
* ``g7`` — G7 economies, quarterly real GDP growth (q/q %), CPI inflation (y/y % of the quarterly mean) and the
  unemployment rate (quarterly mean), origins = OECD MEI real-time editions (quarterly 1999–2026), target
  quarters ``k0`` (the quarter of the origin: a nowcast) .. ``k3``. Transformations are computed **inside each
  edition** (never across editions: base years change); a period's value is then taken from the latest edition
  available at the origin that holds it.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import numpy as np
import polars as pl

from reva.evaluation.cutoff import CutoffMode
from reva.evaluation.vintages import CELL_SCHEMA, VintageAccessor
from reva.models.forecasting import bvar as BV
from reva.models.forecasting import dfm as DF
from reva.models.forecasting.direct import direct_forecast
from reva.models.forecasting.registry_baselines import Abstain, Predictive, ar, final_run, persistence

Pred = Predictive | Abstain

# model keys used in the cases (benchmarks and candidates)
WEO = "benchmark.imf_weo"
SPF = "benchmark.spf_median"
PERSIST = "benchmark.persistence"
REG_AR = "benchmark.registry_ar"
DIRECT_AR = "forecast.direct_ar"
BVAR = "forecast.bvar_minnesota"
DFM = "forecast.dfm_nowcast"
WEO_GAUSS = "benchmark.imf_weo_gauss"
SPF_GAUSS = "benchmark.spf_gauss"


def gauss_around(point: float, ref: Pred) -> Pred:
    """Naive density benchmark: N(point, sigma of the persistence predictive at the same horizon)."""
    if isinstance(ref, Abstain) or ref.sigma is None:
        return Abstain("no persistence spread for the naive density")
    return Predictive("normal", point, mu=point, sigma=ref.sigma)


@dataclass
class Case:
    task: str
    variable: str
    geo: str
    origin: datetime
    horizon: str
    target: str
    outcome: float | None
    outcome_known_at: datetime | None
    scale: float | None
    preds: dict[str, Pred] = field(default_factory=dict)
    info: dict[str, Any] = field(default_factory=dict)


def _f(v: str | None) -> float | None:
    if v is None:
        return None
    x = float(Decimal(v))
    return x if math.isfinite(x) else None


def series_scale(values: Sequence[float], n: int = 20) -> float | None:
    """Root mean square of 1-step changes over the last ``n`` changes (the series' own volatility)."""
    v = np.asarray(values[-(n + 1) :], dtype=float)
    if len(v) < 9:
        return None
    d = np.diff(v)
    s = float(math.sqrt(float(d @ d) / len(d)))
    return s if s > 0 else None


# =========================================================================== WEO panel (annual)
WEO_VARIABLES = ("real_gdp_growth", "cpi_inflation", "unemployment_rate")


def default_weo_origins(first_year: int = 2008, last_year: int = 2025) -> list[datetime]:
    out = []
    for y in range(first_year, last_year + 1):
        out += [datetime(y, 6, 1, tzinfo=UTC), datetime(y, 12, 1, tzinfo=UTC)]
    return out


def weo_cases(cells: pl.DataFrame, variable: str, origins: Sequence[datetime] | None = None) -> list[Case]:
    weo = cells.filter(
        (pl.col("family") == "imf_weo") & (pl.col("variable") == variable) & pl.col("iso3").is_not_null()
    )
    vlist = list(
        weo.filter(pl.col("available_at").is_not_null())
        .group_by("vintage")
        .agg(pl.col("available_at").max())
        .sort("available_at")
        .iter_rows()
    )
    cache: dict[int, tuple[str, datetime, dict[str, float]] | None] = {}

    def outcome(year: int) -> tuple[str, datetime, dict[str, float]] | None:
        if year not in cache:
            floor = datetime(year + 1, 10, 1, tzinfo=UTC)
            cand = [(v, a) for v, a in vlist if a >= floor]
            res = None
            if cand:
                v, a = cand[0]
                rows = weo.filter(
                    (pl.col("vintage") == v)
                    & (pl.col("period_start") == date(year, 1, 1))
                    & (pl.col("estimate_kind") != "projection")
                )
                res = (v, a, {g: x for g, s in rows.select("iso3", "value").iter_rows() if (x := _f(s)) is not None})
            cache[year] = res
        return cache[year]

    out: list[Case] = []
    for t in origins or default_weo_origins():
        acc = VintageAccessor(weo, t, CutoffMode.available_then)
        inforce = acc.vintage_in_force("imf_weo")
        if inforce is None:
            continue
        y0 = t.year
        hist_rows = acc.latest(period_end=date(y0, 1, 1)).rows
        hist: dict[str, list[tuple[int, float]]] = defaultdict(list)
        for g, ps, s in hist_rows.select("iso3", "period_start", "value").sort("iso3", "period_start").iter_rows():
            x = _f(s)
            if x is not None:
                hist[g].append((ps.year, x))
        fc = acc.forecasts(family="imf_weo", period_start=date(y0, 1, 1), period_end=date(y0 + 2, 1, 1)).rows
        proj = {(g, ps.year): _f(s) for g, ps, s in fc.select("iso3", "period_start", "value").iter_rows()}
        for h in (0, 1):
            ty = y0 + h
            oc = outcome(ty)
            for g, series in hist.items():
                w = proj.get((g, ty))
                if w is None:
                    continue
                run = final_run(series, 60)
                if not run or run[-1][0] >= ty:
                    continue
                vals = [v for _, v in run]
                c = Case(
                    "weo",
                    variable,
                    g,
                    t,
                    f"h{h}",
                    str(ty),
                    oc[2].get(g) if oc else None,
                    oc[1] if oc and g in oc[2] else None,
                    series_scale(vals),
                    info={"vintage_in_force": inforce["vintage"], "last_obs": run[-1][0]},
                )
                c.preds[WEO] = Predictive("point", w)
                c.preds[PERSIST] = persistence(run, ty, 1)
                c.preds[WEO_GAUSS] = gauss_around(w, c.preds[PERSIST])
                c.preds[REG_AR] = ar(run, ty, 1)
                c.preds[DIRECT_AR] = direct_forecast(vals, ty - run[-1][0], pmax=2, window=40, min_obs=15)
                out.append(c)
    return out


# =========================================================================== US: RTDSM + SPF (quarterly)
US_VARIABLES = {
    # task variable: (SPF variable, RTDSM variable, transform)
    "real_gdp_growth": ("real_gdp_growth_q_ann", "real_gdp_level", "growth_ann"),
    "gdp_price_inflation": ("gdp_price_inflation_q_ann", "gdp_price_index", "growth_ann"),
    "unemployment_rate": ("unemployment_rate", "unemployment_rate", "quarterly_mean"),
}
US_HORIZONS = ("2", "3", "4", "5", "6")
US_BVAR_ORDER = ("real_gdp_growth", "gdp_price_inflation", "unemployment_rate")
US_OWN_LAG_MEAN = (0.0, 1.0, 1.0)


def _qidx(d: date) -> int:
    return d.year * 4 + (d.month - 1) // 3


def _qlabel(i: int) -> str:
    return f"{i // 4:04d}-Q{i % 4 + 1}"


def _quarterly_from_vintage(rows: Iterable[tuple[date, float]], transform: str) -> dict[int, float]:
    """One vintage's levels -> quarterly series (inside that vintage only)."""
    if transform == "growth_ann":
        lv = dict((_qidx(d), x) for d, x in rows)
        return {q: 100.0 * ((x / lv[q - 1]) ** 4 - 1.0) for q, x in lv.items() if q - 1 in lv and lv[q - 1] > 0}
    if transform == "growth_qq":
        lv = dict((_qidx(d), x) for d, x in rows)
        return {q: 100.0 * (x / lv[q - 1] - 1.0) for q, x in lv.items() if q - 1 in lv and lv[q - 1] > 0}
    months: dict[int, list[float]] = defaultdict(list)
    for d, x in rows:
        months[_qidx(d)].append(x)
    q = {k: sum(v) / 3.0 for k, v in months.items() if len(v) == 3}
    if transform == "quarterly_mean":
        return q
    if transform == "yoy_of_quarterly_mean":
        return {k: 100.0 * (v / q[k - 4] - 1.0) for k, v in q.items() if k - 4 in q and q[k - 4] > 0}
    raise ValueError(f"unknown transform {transform!r}")


def _by_vintage(df: pl.DataFrame) -> dict[str, list[tuple[date, float]]]:
    out: dict[str, list[tuple[date, float]]] = defaultdict(list)
    for v, d, s in df.select("vintage", "period_start", "value").sort("vintage", "period_start").iter_rows():
        x = _f(s)
        if x is not None:
            out[v].append((d, x))
    return out


def _consecutive_tail(series: dict[int, float], last: int, maxlen: int) -> list[tuple[int, float]]:
    out = []
    q = last
    while q in series and len(out) < maxlen:
        out.append((q, series[q]))
        q -= 1
    return out[::-1]


def us_cases(cells: pl.DataFrame, *, bvar_seed: int = 20261017) -> list[Case]:
    rt = cells.filter(pl.col("family") == "philfed_rtdsm")
    spf = cells.filter(pl.col("family") == "philfed_spf")
    # first releases: per target quarter, the earliest vintage that holds it (transform inside that vintage)
    first: dict[str, dict[int, tuple[float, datetime]]] = {}
    for tv, (_sv, rv, tr) in US_VARIABLES.items():
        sub = rt.filter(pl.col("variable") == rv)
        avail = dict(sub.group_by("vintage").agg(pl.col("available_at").max()).iter_rows())
        fr: dict[int, tuple[float, datetime]] = {}
        for v, rows in sorted(_by_vintage(sub).items(), key=lambda kv: avail[kv[0]]):
            for q, x in _quarterly_from_vintage(rows, tr).items():
                if q not in fr:
                    fr[q] = (x, avail[v])
        first[tv] = fr
    surveys = (
        spf.filter(pl.col("available_at").is_not_null())
        .group_by("vintage")
        .agg(pl.col("available_at").first())
        .sort("available_at")
    )
    both = pl.concat([rt, spf])
    out: list[Case] = []
    for sv, sa in surveys.iter_rows():
        acc = VintageAccessor(both, sa, CutoffMode.available_then)
        series: dict[str, dict[int, float]] = {}
        vin = None
        for tv, (_s, rv, tr) in US_VARIABLES.items():
            rows = acc.latest(family="philfed_rtdsm", variable=rv).rows
            if rows.height == 0:
                break
            byv = _by_vintage(rows)
            vin = max(byv, key=lambda v: len(byv[v]))
            series[tv] = _quarterly_from_vintage(byv[vin], tr)  # one vintage: never mix vintages in a transform
        if len(series) < 3:
            continue
        fc = acc.forecasts(family="philfed_spf").rows.filter(pl.col("vintage") == sv)
        survey_q = int(sv[:4]) * 4 + int(sv[-1]) - 1
        lasts = {tv: max(s) for tv, s in series.items()}
        L = min(lasts.values())
        tails = {tv: _consecutive_tail(series[tv], L, 120) for tv in US_BVAR_ORDER}
        n = min(len(x) for x in tails.values())
        bv: list[list[Predictive]] | Abstain = Abstain("no balanced sample")
        if n >= 40:
            Y = np.column_stack([[v for _, v in tails[tv][-n:]] for tv in US_BVAR_ORDER])
            bv = BV.bvar_forecast(Y, own_lag_mean=US_OWN_LAG_MEAN, horizons=8, seed=bvar_seed + survey_q)
        for tv, (sv_var, _rv, _tr) in US_VARIABLES.items():
            s = series[tv]
            last = lasts[tv]
            hist = _consecutive_tail(s, last, 160)
            vals = [v for _, v in hist]
            sf = {
                r["horizon"]: _f(r["value"])
                for r in fc.filter(pl.col("variable") == sv_var).select("horizon", "value").to_dicts()
            }
            for hz in US_HORIZONS:
                tq = survey_q + int(hz) - 2
                if tq <= last or sf.get(hz) is None:
                    continue
                o = first[tv].get(tq)
                c = Case(
                    "us",
                    tv,
                    "USA",
                    sa,
                    f"spf{hz}",
                    _qlabel(tq),
                    o[0] if o else None,
                    o[1] if o else None,
                    series_scale(vals, 40),
                    info={"survey": sv, "rtdsm_vintage": vin, "last_obs": _qlabel(last)},
                )
                c.preds[SPF] = Predictive("point", float(sf[hz]))  # type: ignore[arg-type]
                c.preds[PERSIST] = persistence(hist, tq, 4)
                c.preds[SPF_GAUSS] = gauss_around(float(sf[hz]), c.preds[PERSIST])  # type: ignore[arg-type]
                c.preds[REG_AR] = ar(hist, tq, 4)
                c.preds[DIRECT_AR] = direct_forecast(vals, tq - last, pmax=4, window=120, min_obs=40)
                if isinstance(bv, Abstain):
                    c.preds[BVAR] = bv
                else:
                    step = tq - L
                    c.preds[BVAR] = (
                        bv[step - 1][US_BVAR_ORDER.index(tv)] if 1 <= step <= len(bv) else Abstain("beyond horizon")
                    )
                out.append(c)
    return out


# =========================================================================== G7: OECD MEI real-time editions
G7 = ("USA", "JPN", "DEU", "FRA", "GBR", "ITA", "CAN")
G7_VARIABLES = {
    # task variable: (OECD RT variable, transform inside an edition, frequency of the derived series)
    "real_gdp_growth": ("real_gdp_level", "growth_qq", "Q"),
    "cpi_inflation": ("cpi_level", "yoy_of_quarterly_mean", "Q"),
    "unemployment_rate": ("unemployment_rate", "quarterly_mean", "Q"),
}
G7_BVAR_ORDER = ("real_gdp_growth", "cpi_inflation", "unemployment_rate")
G7_OWN_LAG_MEAN = (0.0, 1.0, 1.0)
G7_MONTHLY = {"d_unemployment": ("unemployment_rate", "diff"), "cpi_yoy": ("cpi_level", "yoy")}


def _monthly_from_vintage(rows: Iterable[tuple[date, float]], transform: str) -> dict[int, float]:
    m = {d.year * 12 + d.month - 1: x for d, x in rows}
    if transform == "diff":
        return {k: x - m[k - 1] for k, x in m.items() if k - 1 in m}
    if transform == "yoy":
        return {k: 100.0 * (x / m[k - 12] - 1.0) for k, x in m.items() if k - 12 in m and m[k - 12] > 0}
    raise ValueError(transform)


def derive_g7_cells(cells: pl.DataFrame) -> pl.DataFrame:
    """Per edition transformations of the OECD MEI real-time cells, as vintage cells (CELL_SCHEMA).

    A derived cell keeps the edition's availability evidence and the provenance (raw sha256, selector) of the
    *latest* source cell it uses, so the vintage accessor and its leakage guard apply unchanged."""
    o = cells.filter(pl.col("family") == "oecd_mei_rt")
    recs: list[dict[str, Any]] = []
    base_cols = ["dataset_key", "table_recorded_at", "family", "provider_geo", "iso3", "vintage", "vintage_date_text",
                 "available_at", "availability_basis", "unit", "raw_sha256", "selector"]  # fmt: skip
    for (geo, var, ver), grp in o.group_by("provider_geo", "variable", "vintage"):
        g = grp.sort("period_start")
        meta = g.select(base_cols).row(-1, named=True)
        rows = [(d, x) for d, s in g.select("period_start", "value").iter_rows() if (x := _f(s)) is not None]
        outs: list[tuple[str, str, dict[int, float]]] = []
        for tv, (rv, tr, _fq) in G7_VARIABLES.items():
            if rv == var:
                outs.append((tv, "Q", _quarterly_from_vintage(rows, tr)))
        for mv, (rv, tr) in G7_MONTHLY.items():
            if rv == var:
                outs.append((mv, "M", _monthly_from_vintage(rows, tr)))
        for tv, fq, ser in outs:
            for k, x in ser.items():
                ps = date(k // 4, (k % 4) * 3 + 1, 1) if fq == "Q" else date(k // 12, k % 12 + 1, 1)
                pe = (
                    date((k + 1) // 4, ((k + 1) % 4) * 3 + 1, 1)
                    if fq == "Q"
                    else date((k + 1) // 12, (k + 1) % 12 + 1, 1)
                )
                recs.append(
                    {
                        **meta,
                        "variable": tv,
                        "provider_variable": f"{var}|derived={tv}",
                        "indicator": f"{var}|derived={tv}|v={ver}",
                        "period_start": ps,
                        "period_end": pe,
                        "estimate_kind": "published_history",
                        "horizon": None,
                        "value": repr(float(x)),
                    }
                )
        del geo
    if not recs:
        return pl.DataFrame(schema=CELL_SCHEMA)
    return pl.DataFrame(recs).select(CELL_SCHEMA.names()).cast(dict(CELL_SCHEMA))  # type: ignore[arg-type]


def default_g7_origins(derived: pl.DataFrame) -> list[datetime]:
    return sorted({a for (a,) in derived.select("available_at").unique().iter_rows() if a is not None})


def _series_of(rows: pl.DataFrame, freq: str) -> dict[str, dict[int, float]]:
    out: dict[str, dict[int, float]] = defaultdict(dict)
    for g, d, s in rows.select("provider_geo", "period_start", "value").iter_rows():
        x = _f(s)
        if x is not None:
            out[g][_qidx(d) if freq == "Q" else d.year * 12 + d.month - 1] = x
    return out


def g7_cases(
    derived: pl.DataFrame,
    origins: Sequence[datetime] | None = None,
    *,
    bvar_seed: int = 20261017,
    extra_monthly: Callable[[str, datetime], dict[str, dict[int, float]]] | None = None,
) -> list[Case]:
    """Cases for the G7 task. ``extra_monthly(geo, origin)`` may add monthly indicators to a *separate*
    pseudo-real-time DFM variant (``forecast.dfm_nowcast_x``); the real-time models never see them."""
    origins = list(origins or default_g7_origins(derived))
    # first releases (earliest edition holding the derived period)
    first: dict[str, dict[tuple[str, int], tuple[float, datetime]]] = {}
    for tv in G7_VARIABLES:
        sub = derived.filter(pl.col("variable") == tv).sort("available_at")
        fr: dict[tuple[str, int], tuple[float, datetime]] = {}
        for g, d, s, a in sub.select("provider_geo", "period_start", "value", "available_at").iter_rows():
            key = (g, _qidx(d))
            if key not in fr and (x := _f(s)) is not None:
                fr[key] = (x, a)
        first[tv] = fr
    out: list[Case] = []
    for t in origins:
        acc = VintageAccessor(derived, t, CutoffMode.available_then)
        qs = {tv: _series_of(acc.latest(variable=tv).rows, "Q") for tv in G7_VARIABLES}
        ms = {mv: _series_of(acc.latest(variable=mv).rows, "M") for mv in G7_MONTHLY}
        oq = _qidx((t - timedelta(days=1)).date())
        for geo in G7:
            ser = {tv: qs[tv].get(geo, {}) for tv in G7_VARIABLES}
            if any(not s for s in ser.values()):
                continue
            lasts = {tv: max(s) for tv, s in ser.items()}
            L = min(lasts.values())
            tails = {tv: _consecutive_tail(ser[tv], L, 120) for tv in G7_BVAR_ORDER}
            n = min(len(x) for x in tails.values())
            bv: list[list[Predictive]] | Abstain = Abstain(f"balanced sample too short ({n} quarters)")
            if n >= 28:
                Y = np.column_stack([[v for _, v in tails[tv][-n:]] for tv in G7_BVAR_ORDER])
                bv = BV.bvar_forecast(
                    Y, own_lag_mean=G7_OWN_LAG_MEAN, horizons=8, seed=bvar_seed + oq + G7.index(geo) * 100000
                )
            dfm_rt = _dfm_inputs(ms, geo, oq, ser["real_gdp_growth"], None)
            dfm_x = _dfm_inputs(ms, geo, oq, ser["real_gdp_growth"], extra_monthly(geo, t)) if extra_monthly else None
            for tv in G7_VARIABLES:
                s = ser[tv]
                last = lasts[tv]
                hist = _consecutive_tail(s, last, 160)
                vals = [v for _, v in hist]
                for k in range(4):
                    tq = oq + k
                    if tq <= last:
                        continue
                    o = first[tv].get((geo, tq))
                    c = Case(
                        "g7",
                        tv,
                        geo,
                        t,
                        f"k{k}",
                        _qlabel(tq),
                        o[0] if o else None,
                        o[1] if o else None,
                        series_scale(vals, 40),
                        info={"last_obs": _qlabel(last), "n_hist": len(hist)},
                    )
                    c.preds[PERSIST] = persistence(hist, tq, 4)
                    c.preds[REG_AR] = ar(hist, tq, 4)
                    c.preds[DIRECT_AR] = direct_forecast(vals, tq - last, pmax=4, window=120, min_obs=24)
                    step = tq - L
                    c.preds[BVAR] = (
                        bv
                        if isinstance(bv, Abstain)
                        else (bv[step - 1][G7_BVAR_ORDER.index(tv)] if 1 <= step <= 8 else Abstain("beyond horizon"))
                    )
                    if tv == "real_gdp_growth" and tq == last + 1:
                        c.preds[DFM] = _run_dfm(dfm_rt, tq)
                        if dfm_x is not None:
                            c.preds[DFM + "_x"] = _run_dfm(dfm_x, tq)
                    out.append(c)
    return out


def _dfm_inputs(
    ms: dict[str, dict[str, dict[int, float]]],
    geo: str,
    oq: int,
    gdp: dict[int, float],
    extra: dict[str, dict[int, float]] | None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, int] | Abstain:
    cols = {mv: ms[mv].get(geo, {}) for mv in G7_MONTHLY}
    if extra:
        cols.update(extra)
    if any(not c for c in cols.values()):
        return Abstain("a monthly indicator has no published value")
    first_m = max(min(c) for c in cols.values())
    end_m = (oq + 4) * 3 - 1  # last month of the quarter after the origin quarter
    first_m += (3 - first_m % 3) % 3  # start on a quarter boundary
    months = np.arange(first_m, end_m + 1)
    X = np.array([[cols[c].get(int(m), np.nan) for c in sorted(cols)] for m in months], dtype=float)
    mq = months // 3 - first_m // 3
    q0 = first_m // 3
    Q = int(mq.max()) + 1
    g = np.array([gdp.get(q0 + i, np.nan) for i in range(Q)], dtype=float)
    return X, mq, g, q0


def _run_dfm(inp: tuple[np.ndarray, np.ndarray, np.ndarray, int] | Abstain, tq: int) -> Pred:
    if isinstance(inp, Abstain):
        return inp
    X, mq, g, q0 = inp
    ti = tq - q0
    if not 0 < ti < len(g):
        return Abstain("target quarter outside the monthly sample")
    gg = g.copy()
    gg[ti:] = np.nan
    return DF.dfm_nowcast(X[mq <= ti], mq[mq <= ti], gg[: ti + 1], ti)


__all__ = [
    "BVAR",
    "DFM",
    "DIRECT_AR",
    "G7",
    "PERSIST",
    "REG_AR",
    "SPF",
    "SPF_GAUSS",
    "WEO",
    "WEO_GAUSS",
    "Case",
    "default_g7_origins",
    "default_weo_origins",
    "derive_g7_cells",
    "g7_cases",
    "series_scale",
    "us_cases",
    "weo_cases",
]
