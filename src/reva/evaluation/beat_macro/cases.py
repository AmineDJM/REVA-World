"""BEAT-MACRO cases: on date t, REVA's nowcast vs the latest official forecast published on or before t.

Tasks (all targets = the calendar year of the origin, "cy"):

* ``weo_cpi`` — every country with an IMF WEO CPI projection and an IMF CPI monthly index; annual-average CPI
  inflation, scored on ``z = 100 ln(1 + pi/100)`` (QA-PRED X6: no pooled means on raw hyperinflation rates).
  Official: the WEO projection of the vintage in force at t. Inputs: IMF CPI (lag rule, ``inputs``), Brent.
* ``g7rt`` — G7, real GDP growth / CPI inflation (z) / unemployment rate, inputs **only** from the OECD MEI
  real-time editions in force at t (``VintageAccessor``) plus Brent. Official: the WEO in force.
* ``ecb_hicp`` — euro area HICP annual-average inflation (z). Official: the ECB SPF average point of the round in
  force at t (quarter-start origins, where the conservative round rule equals the true publication order).
  Inputs: Eurostat HICP (lag rule), Brent.

Outcomes: WEO tasks — the target year in the first WEO vintage available on/after 1 October of the next year (the
PRED-MODELS / PRED-VINTAGE rule, same-institution outcome, QA-PRED X7); ``ecb_hicp`` — the annual average of the
Eurostat HICP index (final; known from the 1st of February of the next year).

Raw REVA points (stored in ``Case.info["raw"]``; densities and combinations are added by ``combine``):

* ``bridge`` — the calendar-year average implied by the months/quarters already published, the remaining
  months filled by persistence of the last published year-on-year rate (CPI), the last level (unemployment, plus
  the level gap to the WEO's last observed year) or the median of the last 8 q/q growth rates (GDP);
* ``bridge_oil`` (CPI only) — the same with the attributed oil pass-through: the filled year-on-year rate moves by
  ``OIL_PASS`` x (change of Brent's y/y rate between the last CPI month and the last complete Brent month).
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Any

import polars as pl

from reva.evaluation.beat_macro.inputs import LaggedMonthly, mi, month_start
from reva.evaluation.cutoff import CutoffMode
from reva.evaluation.pred_models import realtime as R
from reva.evaluation.pred_models.realtime import Case
from reva.evaluation.vintages import VintageAccessor
from reva.models.forecasting.registry_baselines import Predictive

#: pp of headline CPI inflation per pp of global oil-price inflation, in the year of the shock (attributed estimate)
OIL_PASS = 0.04
OIL_EFFECT_REF = (
    "registry/effects/literature/choi_furceri_loungani_mishra_poplawski_ribeiro_2017.yaml"
    "#headline_inflation_impact_10pct_oil (10 % oil price increase -> +0.4 pp headline inflation in the year)"
)
WEO = R.WEO
ECB = "benchmark.ecb_spf"
PERSIST = R.PERSIST
G7 = R.G7
WEO_VAR = {"cpi": "cpi_inflation", "gdp": "real_gdp_growth", "unemp": "unemployment_rate"}
#: CPI consistency check (fiscal-year / definition mismatch between the WEO and the calendar-year CPI index)
CONSIST_MAX_ABS = 0.5
CONSIST_MIN_YEARS = 3


def z(pi: float | None) -> float | None:
    """100 ln(1 + pi/100) (log-percent); None outside the domain."""
    if pi is None or not pi > -100.0:
        return None
    return 100.0 * math.log1p(pi / 100.0)


# =========================================================================== bridges (pure functions)
def oil_adjustment(oil: dict[int, float] | None, last_cpi_month: int) -> float:
    """OIL_PASS x (Brent y/y at the last usable Brent month - Brent y/y at the last CPI month), in pp."""
    if not oil:
        return 0.0
    lo = max(oil)
    L = last_cpi_month
    if lo <= L or any(k not in oil for k in (L, L - 12, lo - 12)):
        return 0.0
    g_l = 100.0 * (oil[L] / oil[L - 12] - 1.0)
    g_o = 100.0 * (oil[lo] / oil[lo - 12] - 1.0)
    return OIL_PASS * (g_o - g_l)


def cpi_bridge_index(idx: dict[int, float], year: int, oil: dict[int, float] | None = None) -> dict[str, Any] | None:
    """Annual-average inflation of ``year`` from a monthly index published up to its last month L; unpublished
    months of ``year - 1`` and ``year`` are filled by holding the y/y ratio of month L (plus the oil adjustment)."""
    if not idx:
        return None
    L = max(idx)
    first = year * 12 - 12
    if L < first - 6 or (L - 12) not in idx:
        return None
    adj = oil_adjustment(oil, L) if oil is not None else 0.0
    r = idx[L] / idx[L - 12] + adj / 100.0
    if not r > 0:
        return None
    full = {m: v for m, v in idx.items() if m >= first - 12}
    for m in range(L + 1, year * 12 + 12):
        if (m - 12) not in full:
            return None
        full[m] = full[m - 12] * r
    if any(m not in full for m in range(first, year * 12 + 12)):
        return None
    prev = math.fsum(full[m] for m in range(first, first + 12))
    cur = math.fsum(full[m] for m in range(year * 12, year * 12 + 12))
    return {
        "pi": 100.0 * (cur / prev - 1.0),
        "last_month": L,
        "months_known": max(0, min(L, year * 12 + 11) - year * 12 + 1),
        "oil_adj_pp": adj,
    }


def calendar_avg_inflation(idx: dict[int, float], year: int) -> float | None:
    ms = range(year * 12 - 12, year * 12 + 12)
    if any(m not in idx for m in ms):
        return None
    prev = math.fsum(idx[m] for m in range(year * 12 - 12, year * 12))
    cur = math.fsum(idx[m] for m in range(year * 12, year * 12 + 12))
    return 100.0 * (cur / prev - 1.0)


def cpi_bridge_yoy(yoy: dict[int, float], year: int, oil: dict[int, float] | None = None) -> dict[str, Any] | None:
    """Same target from published y/y rates (OECD MEI editions): mean of the 12 monthly y/y rates of ``year``
    (approximation of the index-ratio definition, error of the order of 0.01 pp), unpublished months = last y/y."""
    if not yoy:
        return None
    L = max(yoy)
    if L < year * 12 - 7:
        return None
    adj = oil_adjustment(oil, L) if oil is not None else 0.0
    vals = []
    for m in range(year * 12, year * 12 + 12):
        if m <= L:
            if m not in yoy:
                return None
            vals.append(yoy[m])
        else:
            vals.append(yoy[L] + adj)
    return {"pi": math.fsum(vals) / 12.0, "last_month": L,
            "months_known": max(0, min(L, year * 12 + 11) - year * 12 + 1), "oil_adj_pp": adj}  # fmt: skip


def unemp_bridge(u: dict[int, float], year: int, weo_hist: dict[int, float]) -> dict[str, Any] | None:
    """Calendar-year mean of the monthly rate (unpublished months = last published rate) plus the level gap
    between the WEO's last observed year and the MEI mean of that year (national vs harmonised definitions)."""
    if not u:
        return None
    L = max(u)
    if L < year * 12 - 7:
        return None
    vals = []
    for m in range(year * 12, year * 12 + 12):
        if m <= L:
            if m not in u:
                return None
            vals.append(u[m])
        else:
            vals.append(u[L])
    gap = None
    for y in sorted((y for y in weo_hist if y < year), reverse=True):
        ms = range(y * 12, y * 12 + 12)
        if all(m in u for m in ms):
            gap = weo_hist[y] - math.fsum(u[m] for m in ms) / 12.0
            break
    if gap is None:
        return None
    return {"pi": math.fsum(vals) / 12.0 + gap, "last_month": L, "level_gap": gap,
            "months_known": max(0, min(L, year * 12 + 11) - year * 12 + 1)}  # fmt: skip


def gdp_bridge(g: dict[int, float], year: int) -> dict[str, Any] | None:
    """Annual-average growth of ``year`` from published q/q growth rates; unpublished quarters = median of the
    last 8 published q/q rates."""
    if not g:
        return None
    Lq = max(g)
    q0 = year * 4 - 4
    if Lq < q0 - 1:
        return None
    last8 = [g[q] for q in range(Lq - 7, Lq + 1) if q in g]
    if len(last8) < 6:
        return None
    fill = statistics.median(last8)
    lv = {q0: 1.0}
    for q in range(q0 + 1, year * 4 + 4):
        gq = g.get(q) if q <= Lq else fill
        if gq is None:
            return None
        lv[q] = lv[q - 1] * (1.0 + gq / 100.0)
    prev = math.fsum(lv[q] for q in range(q0, q0 + 4))
    cur = math.fsum(lv[q] for q in range(year * 4, year * 4 + 4))
    return {"pi": 100.0 * (cur / prev - 1.0), "last_quarter": Lq, "fill_qq": fill,
            "quarters_known": max(0, min(Lq, year * 4 + 3) - year * 4 + 1)}  # fmt: skip


# =========================================================================== origins
def monthly_origins(first: date, last: date) -> list[datetime]:
    return [month_start(m) for m in range(mi(first), mi(last) + 1)]


#: WEO Updates (January / July) revise GDP projections for ~30 economies (incl. the G7); REVA has no vintages of
#: them, so GDP is compared only at origins where the full WEO in force is also the latest IMF GDP projection.
#: Exceptions where a WEO Update was published before 1 July (from the IMF WEO Update archive titles, June 2011
#: and June 2020; not machine-verified because www.imf.org blocks this client).
GDP_FAIR_MONTHS = (1, 5, 7, 11)
GDP_UNFAIR = {date(2011, 7, 1), date(2020, 7, 1)}


def gdp_fair(t: datetime) -> bool:
    return t.month in GDP_FAIR_MONTHS and t.date() not in GDP_UNFAIR


# =========================================================================== WEO panel access (vintages)
class WeoPanel:
    """WEO vintage cells of one variable, read through the vintage accessor at each origin (cached per state)."""

    def __init__(self, cells: pl.DataFrame, variable: str) -> None:
        self.variable = variable
        self.weo = cells.filter(
            (pl.col("family") == "imf_weo") & (pl.col("variable") == variable) & pl.col("iso3").is_not_null()
        )
        self.vlist = list(
            self.weo.filter(pl.col("available_at").is_not_null())
            .group_by("vintage")
            .agg(pl.col("available_at").max())
            .sort("available_at")
            .iter_rows()
        )
        self._state: dict[tuple[str, int], tuple[dict[str, float], dict[str, dict[int, float]]]] = {}
        self._out: dict[int, tuple[datetime, dict[str, float]] | None] = {}

    def in_force_label(self, t: datetime) -> str | None:
        lab = [v for v, a in self.vlist if a <= t]
        return lab[-1] if lab else None

    def state(self, t: datetime) -> tuple[str, dict[str, float], dict[str, dict[int, float]]] | None:
        """(vintage in force, projection for the origin's year per ISO3, observed history per ISO3)."""
        lab = self.in_force_label(t)
        if lab is None:
            return None
        y = t.year
        key = (lab, y)
        if key not in self._state:
            acc = VintageAccessor(self.weo, t, CutoffMode.available_then)
            inf = acc.vintage_in_force("imf_weo")
            assert inf is not None and inf["vintage"] == lab
            fc = acc.forecasts(family="imf_weo", period_start=date(y, 1, 1), period_end=date(y + 1, 1, 1)).rows
            proj = {
                g: x
                for g, ps, s in fc.select("iso3", "period_start", "value").iter_rows()
                if ps.year == y and (x := R._f(s)) is not None
            }
            hist: dict[str, dict[int, float]] = defaultdict(dict)
            hr = acc.latest(family="imf_weo", period_start=date(y - 30, 1, 1), period_end=date(y, 1, 1)).rows
            for g, ps, s in hr.select("iso3", "period_start", "value").iter_rows():
                if (x := R._f(s)) is not None and ps.year < y:
                    hist[g][ps.year] = x
            self._state[key] = (proj, dict(hist))
        proj, hist = self._state[key]
        return lab, proj, hist

    def outcome(self, year: int) -> tuple[datetime, dict[str, float]] | None:
        if year not in self._out:
            floor = datetime(year + 1, 10, 1, tzinfo=UTC)
            cand = [(v, a) for v, a in self.vlist if a >= floor]
            res = None
            if cand:
                v, a = cand[0]
                rows = self.weo.filter(
                    (pl.col("vintage") == v)
                    & (pl.col("period_start") == date(year, 1, 1))
                    & (pl.col("estimate_kind") != "projection")
                )
                res = (a, {g: x for g, s in rows.select("iso3", "value").iter_rows() if (x := R._f(s)) is not None})
            self._out[year] = res
        return self._out[year]


def _scale(hist: dict[int, float], year: int, transform: bool) -> float | None:
    ys = sorted(y for y in hist if y < year)[-21:]
    vals = [z(hist[y]) if transform else hist[y] for y in ys]
    vv = [v for v in vals if v is not None]
    s = R.series_scale(vv, 20)
    return max(s, 0.3) if s is not None else None


def _persistence(hist: dict[int, float], year: int, transform: bool) -> Predictive | None:
    ys = [y for y in hist if y < year]
    if not ys:
        return None
    v = hist[max(ys)]
    x = z(v) if transform else v
    return Predictive("point", x) if x is not None else None


# =========================================================================== task: weo_cpi (panel)
def cpi_consistent(idx: dict[int, float], hist: dict[int, float], year: int) -> tuple[bool, str]:
    """True when the calendar-year average of the CPI index matches the WEO's observed inflation over the last
    years known at the origin (excludes fiscal-year and definition mismatches with information at t only)."""
    diffs = []
    for y in range(year - 6, year - 1):
        if y in hist:
            a = calendar_avg_inflation(idx, y)
            if a is not None and (za := z(a)) is not None and (zw := z(hist[y])) is not None:
                diffs.append(abs(za - zw))
    if len(diffs) < CONSIST_MIN_YEARS:
        return False, f"fewer than {CONSIST_MIN_YEARS} comparable years ({len(diffs)})"
    med = statistics.median(diffs)
    if med > CONSIST_MAX_ABS:
        return False, f"median |calendar CPI - WEO| = {med:.2f} > {CONSIST_MAX_ABS} (fiscal year / definition)"
    return True, "ok"


def weo_cpi_cases(cells: pl.DataFrame, cpi: LaggedMonthly, oil: LaggedMonthly,
                  origins: Sequence[datetime]) -> list[Case]:  # fmt: skip
    panel = WeoPanel(cells, "cpi_inflation")
    out: list[Case] = []
    for t in origins:
        st = panel.state(t)
        if st is None:
            continue
        lab, proj, hist = st
        y = t.year
        oc = panel.outcome(y)
        oil_t = oil.usable("WORLD", t)
        for g, w in proj.items():
            h = hist.get(g, {})
            zw = z(w)
            if zw is None:
                continue
            o = oc[1].get(g) if oc else None
            c = Case("weo_cpi", "cpi_inflation", g, t, "cy", str(y), z(o) if o is not None else None,
                     oc[0] if oc and o is not None else None, _scale(h, y, True),
                     info={"vintage_in_force": lab, "raw": {}})  # fmt: skip
            c.preds[WEO] = Predictive("point", zw)
            if (p := _persistence(h, y, True)) is not None:
                c.preds[PERSIST] = p
            idx = cpi.usable(g, t)
            ok, why = cpi_consistent(idx, h, y) if idx else (False, "no CPI index usable at the origin")
            c.info["consistency"] = why
            if ok:
                for name, ol in (("bridge", None), ("bridge_oil", oil_t)):
                    b = cpi_bridge_index(idx, y, ol)
                    if b is not None and (zb := z(b["pi"])) is not None:
                        c.info["raw"][name] = zb
                        c.info[name] = {k: v for k, v in b.items() if k != "pi"}
            out.append(c)
    return out


# =========================================================================== task: g7rt (OECD MEI real-time)
def mei_cells(cells: pl.DataFrame) -> pl.DataFrame:
    """Derived per-edition cells (q/q GDP growth, monthly CPI y/y) + the raw monthly unemployment rates."""
    der = R.derive_g7_cells(cells)
    der = der.filter(pl.col("variable").is_in(["real_gdp_growth", "cpi_yoy"]))
    raw = cells.filter((pl.col("family") == "oecd_mei_rt") & (pl.col("variable") == "unemployment_rate"))
    return pl.concat([der, raw.select(der.columns).cast(dict(der.schema))])  # type: ignore[arg-type]


def _mei_series(acc: VintageAccessor, variable: str, since: date, freq: str) -> dict[str, dict[int, float]]:
    rows = acc.latest(family="oecd_mei_rt", variable=variable, geos=list(G7), period_start=since).rows
    out: dict[str, dict[int, float]] = defaultdict(dict)
    for g, d, s in rows.select("provider_geo", "period_start", "value").iter_rows():
        if (x := R._f(s)) is not None:
            out[g][(d.year * 4 + (d.month - 1) // 3) if freq == "Q" else mi(d)] = x
    return out


def g7_cases(cells: pl.DataFrame, oil: LaggedMonthly, origins: Sequence[datetime]) -> list[Case]:
    mei = mei_cells(cells)
    avail = sorted({a for (a,) in mei.select("available_at").unique().iter_rows() if a is not None})
    panels = {k: WeoPanel(cells, v) for k, v in WEO_VAR.items()}
    cache: dict[tuple[datetime, int], dict[str, dict[str, dict[int, float]]]] = {}
    out: list[Case] = []
    for t in origins:
        state = [a for a in avail if a <= t]
        if not state:
            continue
        y = t.year
        key = (state[-1], y)
        if key not in cache:
            acc = VintageAccessor(mei, t, CutoffMode.available_then)
            since = date(y - 8, 1, 1)
            cache[key] = {
                "gdp": _mei_series(acc, "real_gdp_growth", since, "Q"),
                "cpi": _mei_series(acc, "cpi_yoy", since, "M"),
                "unemp": _mei_series(acc, "unemployment_rate", since, "M"),
            }
        ser = cache[key]
        oil_t = oil.usable("WORLD", t)
        for var, panel in panels.items():
            if var == "gdp" and not gdp_fair(t):
                continue
            st = panel.state(t)
            if st is None:
                continue
            lab, proj, hist = st
            oc = panel.outcome(y)
            tr = var == "cpi"
            for g in G7:
                w = proj.get(g)
                if w is None:
                    continue
                h = hist.get(g, {})
                o = oc[1].get(g) if oc else None
                c = Case("g7rt", WEO_VAR[var], g, t, "cy", str(y),
                         (z(o) if tr else o) if o is not None else None,
                         oc[0] if oc and o is not None else None, 1.0,
                         info={"vintage_in_force": lab, "mei_state": key[0].isoformat(), "raw": {}})  # fmt: skip
                zw = z(w) if tr else w
                if zw is None:
                    continue
                c.preds[WEO] = Predictive("point", zw)
                if (p := _persistence(h, y, tr)) is not None:
                    c.preds[PERSIST] = p
                s = ser[var].get(g, {})
                variants: list[tuple[str, dict[str, Any] | None]]
                if var == "gdp":
                    variants = [("bridge", gdp_bridge(s, y))]
                elif var == "unemp":
                    variants = [("bridge", unemp_bridge(s, y, h))]
                else:
                    variants = [("bridge", cpi_bridge_yoy(s, y)), ("bridge_oil", cpi_bridge_yoy(s, y, oil_t))]
                for name, b in variants:
                    if b is not None and (v := (z(b["pi"]) if tr else b["pi"])) is not None:
                        c.info["raw"][name] = v
                        c.info[name] = {k: x for k, x in b.items() if k != "pi"}
                out.append(c)
    return out


# =========================================================================== task: ecb_hicp
def ecb_cases(cells: pl.DataFrame, hicp: LaggedMonthly, oil: LaggedMonthly, origins: Sequence[datetime],
              geo: str = "EA") -> list[Case]:  # fmt: skip
    spf = cells.filter((pl.col("family") == "ecb_spf") & (pl.col("variable") == "hicp_inflation"))
    full = hicp.values.get(geo, {})
    lag = hicp.lag[geo]
    out: list[Case] = []
    for t in origins:
        y = t.year
        acc = VintageAccessor(spf, t, CutoffMode.available_then)
        inf = acc.vintage_in_force("ecb_spf")
        if inf is None:
            continue
        rows = acc.forecasts(family="ecb_spf", variable="hicp_inflation").rows.filter(
            (pl.col("vintage") == inf["vintage"]) & (pl.col("horizon") == str(y))
        )
        if rows.height != 1:
            continue
        w = R._f(rows["value"][0])
        o = calendar_avg_inflation(full, y)
        known = month_start(y * 12 + 11 + lag) if o is not None else None
        idx = hicp.usable(geo, t)
        hist = {yy: a for yy in range(y - 25, y) if (a := calendar_avg_inflation(idx, yy)) is not None}
        c = Case("ecb_hicp", "hicp_inflation", "EA", t, "cy", str(y), z(o), known, 1.0,
                 info={"spf_round": inf["vintage"], "raw": {}})  # fmt: skip
        if (zw := z(w)) is None:
            continue
        c.preds[ECB] = Predictive("point", zw)
        if (p := _persistence(hist, y, True)) is not None:
            c.preds[PERSIST] = p
        oil_t = oil.usable("WORLD", t)
        for name, ol in (("bridge", None), ("bridge_oil", oil_t)):
            b = cpi_bridge_index(idx, y, ol)
            if b is not None and (zb := z(b["pi"])) is not None:
                c.info["raw"][name] = zb
                c.info[name] = {k: v for k, v in b.items() if k != "pi"}
        out.append(c)
    return out


__all__ = [
    "ECB",
    "GDP_FAIR_MONTHS",
    "OIL_EFFECT_REF",
    "OIL_PASS",
    "WEO",
    "WeoPanel",
    "calendar_avg_inflation",
    "cpi_bridge_index",
    "cpi_bridge_yoy",
    "cpi_consistent",
    "ecb_cases",
    "g7_cases",
    "gdp_bridge",
    "gdp_fair",
    "mei_cells",
    "monthly_origins",
    "oil_adjustment",
    "unemp_bridge",
    "weo_cpi_cases",
    "z",
]
