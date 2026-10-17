"""Same-date cases: one case per official nowcast publication (V7, BEAT-NOWCAST).

For an official nowcast of target period P published on day ``d`` (strictly before the truth's release day):

* **REVA cutoff** ``C = d 00:00 UTC`` — pure REVA inputs (RTDSM monthly vintages, gasoline spot prices, CPI
  history, GDP first releases for estimation, past errors for the second pass) must be available at ``C``, i.e.
  published before the official nowcast's day: REVA never has more information than the official forecaster on
  that day (it usually has less: the RTDSM month-end availability rule lags each indicator release by up to a
  month, the EIA spot prices by >= 8 days);
* **official cutoff** ``C + 1 day`` — the official nowcast of day ``d`` itself and, for ``stack_all``, the other
  official nowcast of P published on/before ``d``.

Each case's inputs are checked by a :class:`~reva.evaluation.leakage.LeakageGuard` (``available_then``) at the
right cutoff. ``Case.origin`` is the target period's first day (the unit of inference: the loss differential is
averaged over all publication days of a target period); ``Case.horizon`` is the phase bucket of ``d``.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from reva.evaluation.beat_nowcast import data as D
from reva.evaluation.beat_nowcast import models as M
from reva.evaluation.pred_models.realtime import Case
from reva.models.forecasting.registry_baselines import Predictive

OFFICIAL = {
    "gdpnow": "benchmark.atl_gdpnow",
    "nyfed": "benchmark.nyfed_nowcast",
    "clev": "benchmark.clev_nowcast",
}
CELLS = ("gdp_gdpnow", "gdp_nyfed", "cpi_clev", "core_clev")
CELL_OFFICIAL = {"gdp_gdpnow": "gdpnow", "gdp_nyfed": "nyfed", "cpi_clev": "clev", "core_clev": "clev"}
CELL_VARIABLE = {
    "gdp_gdpnow": "real_gdp_growth_q_ann",
    "gdp_nyfed": "real_gdp_growth_q_ann",
    "cpi_clev": "cpi_inflation_m",
    "core_clev": "core_cpi_inflation_m",
}
RULES = {
    "gdp_gdpnow": M.Rules(OFFICIAL["gdpnow"], 12, 4.0, 4, 8, ("stack_reva", "stack_all")),
    "gdp_nyfed": M.Rules(OFFICIAL["nyfed"], 12, 4.0, 4, 8, ("stack_reva", "stack_all")),
    "cpi_clev": M.Rules(OFFICIAL["clev"], 24, 6.0, 6, 12, ("stack_reva",)),
    "core_clev": M.Rules(OFFICIAL["clev"], 24, 6.0, 6, 12, ("stack_reva",)),
}
GAS_SERIES = "EIA_SPOT|series=EER_EPMRU_PF4_Y35NY_DPG"
GAS_LAG_DAYS = 7


@dataclass
class Inputs:
    gdpnow: D.OfficialBook
    nyfed: D.OfficialBook
    clev_cpi: D.OfficialBook
    clev_core: D.OfficialBook
    gdp_first: dict[date, D.Truth]
    gdp_third: dict[date, D.Truth]
    cpi_actual: dict[date, D.Truth]
    core_actual: dict[date, D.Truth]
    rt: dict[str, D.VintageSeries]
    cpi_hist: D.CpiHistory
    gas: D.DailySeries
    pins: dict[str, Any] = field(default_factory=dict)


def bucket_gdp(d: date, target: date) -> str:
    k = D.mi_of(d) - D.mi_of(target)
    return f"m{max(-1, min(3, k))}"


def bucket_cpi(d: date, target: date) -> str:
    k = D.mi_of(d) - D.mi_of(target)
    return f"m{max(0, min(1, k))}"


@dataclass
class BuildLog:
    skipped: Counter[str] = field(default_factory=Counter)
    guard_checks: int = 0
    cache_hits: int = 0
    cache_misses: int = 0


def _ts(dt: datetime) -> float:
    return dt.timestamp()


class _GdpPure:
    """Pure REVA GDP points per (target, vintage state, known first releases), cached."""

    def __init__(self, inp: Inputs) -> None:
        self.inp = inp
        self.cache: dict[tuple[Any, ...], dict[str, float]] = {}
        self.refs: dict[tuple[Any, ...], list[Any]] = {}
        self.first_sorted = sorted(inp.gdp_first.values(), key=lambda t: t.known_at)

    def __call__(self, target: date, cut: datetime, log: BuildLog) -> tuple[dict[str, float], list[Any]]:
        poss, panel = [], {}
        for k in M.GDP_INDICATORS:
            s = self.inp.rt.get(k)
            got = s.as_of(cut) if s is not None else None
            if got is None:
                return {}, []
            poss.append(got[0])
            panel[k] = got[1]
        known = [t for t in self.first_sorted if t.known_at <= cut]
        ck = (target, tuple(poss), len(known))
        if ck in self.cache:
            log.cache_hits += 1
            return self.cache[ck], self.refs[ck]
        log.cache_misses += 1
        g = {D.qi_of(k): t.value for k, t in self.inp.gdp_first.items() if t.known_at <= cut}
        out: dict[str, float] = {}
        tq = D.qi_of(target)
        for name, fn in (("bridge", M.gdp_bridge), ("dfm", M.gdp_dfm)):
            p = fn(panel, g, tq)
            if isinstance(p, Predictive):
                out[name] = p.point
        refs: list[Any] = [self.inp.rt[k].stamp(p) for k, p in zip(M.GDP_INDICATORS, poss, strict=True)]
        if known:
            refs.append(D.truth_ref(known[-1]))
            refs.append(D.model_ref("forecast.nc_bridge/dfm", known[-1].known_at))
        self.cache[ck], self.refs[ck] = out, refs
        return out, refs


def gdp_cases(
    inp: Inputs,
    cell: str,
    first_qi: int,
    last_qi: int,
    log: BuildLog,
    open_targets: frozenset[date] = frozenset(),
    as_of: date | None = None,
) -> list[Case]:
    """Cases of every official update of targets in [first_qi, last_qi] published before the truth's release day;
    ``open_targets`` (prospective, truth not yet published): updates published on/before ``as_of``, outcome None."""
    off = CELL_OFFICIAL[cell]
    book, other = (inp.gdpnow, inp.nyfed) if off == "gdpnow" else (inp.nyfed, inp.gdpnow)
    pure = _GdpPure(inp)
    out: list[Case] = []
    for target, ups in sorted(book.updates.items()):
        qi = D.qi_of(target)
        if not first_qi <= qi <= last_qi:
            continue
        truth = inp.gdp_first.get(target)
        is_open = truth is None and target in open_targets and as_of is not None
        if truth is None and not is_open:
            log.skipped[f"{cell}:no_truth_yet"] += len(ups)
            continue
        third = inp.gdp_third.get(target)
        for pub, st in ups:
            if (truth is not None and pub >= truth.release_day) or (is_open and as_of is not None and pub > as_of):
                log.skipped[f"{cell}:published_on_or_after_release_or_after_as_of"] += 1
                continue
            cut = D.midnight(pub)
            ocut = cut + timedelta(days=1)
            raw, refs = pure(target, cut, log)
            log.guard_checks += D.guard_refs(cut, refs)
            oth = other.latest(target, ocut)
            log.guard_checks += D.guard_refs(ocut, [D.stamp_ref(st), D.stamp_ref(oth[1]) if oth else None])
            c = Case(
                task=cell, variable=CELL_VARIABLE[cell], geo="USA", origin=D.midnight(target),
                horizon=bucket_gdp(pub, target), target=D.qlabel(qi), outcome=truth.value if truth else None,
                outcome_known_at=truth.known_at if truth else None, scale=1.0,
                preds={OFFICIAL[off]: Predictive("point", st.value)},
                info={"date": pub.isoformat(), "reva_cut": _ts(cut), "raw": dict(raw),
                      "other": oth[1].value if oth else None, "third": third.value if third else None,
                      "official_ref": st.ref},
            )  # fmt: skip
            out.append(c)
    M.run_second_pass(out, RULES[cell])
    return out


def _gas_month_change(gas: D.DailySeries, mi: int, cut: datetime) -> float | None:
    cur = gas.month_mean(mi, cut)
    prev = gas.month_mean(mi - 1, cut)
    if prev is None:
        return None
    if cur is None:
        lm = gas.last_mean(cut)
        if lm is None:
            return None
        return 100.0 * math.log(lm[0] / prev[0])
    return 100.0 * math.log(cur[0] / prev[0])


def cpi_cases(
    inp: Inputs,
    cell: str,
    first_mi: int,
    last_mi: int,
    log: BuildLog,
    open_targets: frozenset[date] = frozenset(),
    as_of: date | None = None,
) -> list[Case]:
    book = inp.clev_cpi if cell == "cpi_clev" else inp.clev_core
    truths = inp.cpi_actual if cell == "cpi_clev" else inp.core_actual
    cache: dict[tuple[Any, ...], tuple[dict[str, float], list[Any]]] = {}
    out: list[Case] = []
    for target, ups in sorted(book.updates.items()):
        mi = D.mi_of(target)
        if not first_mi <= mi <= last_mi:
            continue
        truth = truths.get(target)
        is_open = truth is None and target in open_targets and as_of is not None
        if truth is None and not is_open:
            log.skipped[f"{cell}:no_truth_yet"] += len(ups)
            continue
        for pub, st in ups:
            if (truth is not None and pub >= truth.release_day) or (is_open and as_of is not None and pub > as_of):
                log.skipped[f"{cell}:published_on_or_after_release_or_after_as_of"] += 1
                continue
            cut = D.midnight(pub)
            ocut = cut + timedelta(days=1)
            ck = (target, cut)
            if ck not in cache:
                raw: dict[str, float] = {}
                refs: list[Any] = []
                if cell == "cpi_clev":
                    hist, refs = inp.cpi_hist.as_of(cut)
                    gas = {
                        m: v for m in range(M.CPI_FIRST_M, mi) if (v := _gas_month_change(inp.gas, m, cut)) is not None
                    }
                    p = M.cpi_headline_bridge(hist, gas, _gas_month_change(inp.gas, mi, cut), mi)
                    gref = inp.gas.ref(cut)
                    if gref is not None:
                        refs.append(gref)
                else:
                    known = {D.mi_of(k): t for k, t in truths.items() if t.known_at <= cut}
                    p = M.cpi_core_bridge({m: t.value for m, t in known.items()}, mi)
                    if known:
                        refs.append(D.truth_ref(max(known.values(), key=lambda t: t.known_at)))
                if isinstance(p, Predictive):
                    raw["cpi_bridge"] = p.point
                cache[ck] = (raw, refs)
            raw, refs = cache[ck]
            log.guard_checks += D.guard_refs(cut, refs)
            log.guard_checks += D.guard_refs(ocut, [D.stamp_ref(st)])
            c = Case(
                task=cell, variable=CELL_VARIABLE[cell], geo="USA", origin=D.midnight(target),
                horizon=bucket_cpi(pub, target), target=D.mlabel(mi), outcome=truth.value if truth else None,
                outcome_known_at=truth.known_at if truth else None, scale=1.0,
                preds={OFFICIAL["clev"]: Predictive("point", st.value)},
                info={"date": pub.isoformat(), "reva_cut": _ts(cut), "raw": dict(raw), "official_ref": st.ref},
            )  # fmt: skip
            out.append(c)
    M.run_second_pass(out, RULES[cell])
    return out


__all__ = [
    "CELLS",
    "CELL_OFFICIAL",
    "GAS_LAG_DAYS",
    "GAS_SERIES",
    "OFFICIAL",
    "RULES",
    "BuildLog",
    "Inputs",
    "bucket_cpi",
    "bucket_gdp",
    "cpi_cases",
    "gdp_cases",
]
