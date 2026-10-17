"""Country-month benchmark against VIEWS on UCDP GED (pure): two vintages, never mixed.

Outcome: ``deaths_best|tov=sb`` of V6CONFLICT's ``atlas.ucdp_ged.country_month`` (GED 26.1 final, 1989-01 ..
2025-12) and ``atlas.ucdp_ged.cand_country_month`` (UCDP Candidate, preliminary, 2026-01 .. 2026-08). GED writes a
cell only where it records an event; within a table's covered months, an absent country-month of a country in the
UCDP country-year panel (``orgvio_cy``, every GW state of the year) is read as **0 recorded state-based deaths**
under that definition (V6CONFLICT report §0) -- the atlas itself never stores the zero.

Vintages: GED final is the *revised* vintage; Candidate is the *real-time* vintage (what was known within a month
or a quarter). The back-test on 2023-2025 targets scores VIEWS runs released at the time against GED final; REVA's
country-month baselines at the same origin read GED final up to the origin month -- data VIEWS did not have when it
released its run (it used Candidate): the comparison favours the baselines on information, stated with the result.
The 2026 check scores the same forecasters against the Candidate months (preliminary outcomes, labelled).

Families (ViEWS' own targets): ``ucdp.sb_deaths_cm`` (state-based deaths in the country-month, MSLE and absolute
error of the point) and ``ucdp.sb_conflict25_cm`` (at least 25 such deaths: VIEWS ``main_dich`` is "Predicted
probability of at least 25 battle-related deaths (BRDs) in state-based armed conflict per country-month"; Brier and
log loss, VIEWS probabilities, rounded by the API to 4 decimals, are floored at 5e-5 for the log loss only).

Baselines (as of the origin month T, all countries pooled over the 120 months before T):
* ``forecast.conflict_persistence``: point = deaths in T; probability = historical frequency of >= 25 deaths s
  months later among country-months in the same intensity bin as T, (k+1)/(n+2);
* ``forecast.conflict_base_rate``: point = the country's mean over the last 12 months; probability = the country's
  share of months with >= 25 deaths over the last 120 months, (k+1)/(n+2).
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import polars as pl

from reva.evaluation.conflict.stats import dm_test, holm
from reva.evaluation.conflict.views import ViewsRun

INDICATOR = "deaths_best|tov=sb"
FINAL_TABLE = "atlas.ucdp_ged.country_month"
CAND_TABLE = "atlas.ucdp_ged.cand_country_month"
THRESHOLD = 25.0
WINDOW = 120
PROB_FLOOR = 5e-5
BINS = ((0.0, 0.0), (1.0, 24.0), (25.0, 99.0), (100.0, 999.0), (1000.0, math.inf))
STEP_GROUPS = ((1, 3), (4, 6), (7, 12))
BASELINES = ("forecast.conflict_persistence", "forecast.conflict_base_rate")
VIEWS = "forecast.reference_views"


def midx(y: int, m: int) -> int:
    return y * 12 + (m - 1)


@dataclass
class MonthlyPanel:
    """deaths[gw][month index]; ``covered`` = (first, last) month index of the vintage; ``universe[year]`` = GW codes
    of the UCDP country-year panel (whose absent months mean 0 recorded deaths)."""

    deaths: dict[str, dict[int, float]] = field(default_factory=dict)
    covered: tuple[int, int] | None = None
    universe: dict[int, set[str]] = field(default_factory=dict)

    def y(self, g: str, i: int) -> float | None:
        if self.covered is None or not (self.covered[0] <= i <= self.covered[1]):
            return None
        if g not in self.universe.get(i // 12, set()) and g not in self.deaths:
            return None
        return self.deaths.get(g, {}).get(i, 0.0)

    def geos(self, year: int) -> set[str]:
        return self.universe.get(year, set())


def build_monthly(frame: pl.DataFrame, universe: Mapping[int, Iterable[str]], covered: tuple[int, int]) -> MonthlyPanel:
    p = MonthlyPanel(covered=covered, universe={y: set(v) for y, v in universe.items()})
    f = frame.filter(pl.col("indicator") == INDICATOR)
    for r in f.select("provider_geo", "period_start", "value").iter_rows(named=True):
        if r["value"] is None:
            continue
        ps = r["period_start"]
        i = midx(ps.year, ps.month)
        if covered[0] <= i <= covered[1]:
            p.deaths.setdefault(str(r["provider_geo"]), {})[i] = float(Decimal(str(r["value"])))
    return p


def coverage_of(frame: pl.DataFrame) -> tuple[int, int] | None:
    f = frame.filter(pl.col("indicator") == INDICATOR)
    if f.height == 0:
        return None
    lo, hi = f["period_start"].min(), f["period_start"].max()
    return midx(lo.year, lo.month), midx(hi.year, hi.month)  # type: ignore[union-attr]


def _bin(v: float) -> int:
    for k, (lo, hi) in enumerate(BINS):
        if lo <= v <= hi:
            return k
    return 0


def baseline_forecasts(
    p: MonthlyPanel, origin: int, steps: Sequence[int], geos: Iterable[str]
) -> dict[tuple[str, str, int], tuple[float, float]]:
    """(model, gw, step) -> (point, probability of >= 25), using only months <= origin."""
    gl = sorted(geos)
    trans: dict[tuple[int, int], list[int]] = defaultdict(lambda: [0, 0])  # (step, bin) -> [k, n]
    for g in gl:
        for t in range(origin - WINDOW, origin + 1):
            yt = p.y(g, t)
            if yt is None:
                continue
            for s in steps:
                if t + s > origin:
                    continue
                ys = p.y(g, t + s)
                if ys is None:
                    continue
                c = trans[(s, _bin(yt))]
                c[0] += ys >= THRESHOLD
                c[1] += 1
    out: dict[tuple[str, str, int], tuple[float, float]] = {}
    for g in gl:
        y0 = p.y(g, origin)
        if y0 is None:
            continue
        hist = [v for t in range(origin - WINDOW + 1, origin + 1) if (v := p.y(g, t)) is not None]
        last12 = [v for t in range(origin - 11, origin + 1) if (v := p.y(g, t)) is not None]
        k = sum(1 for v in hist if v >= THRESHOLD)
        for s in steps:
            kk, nn = trans[(s, _bin(y0))]
            out[("forecast.conflict_persistence", g, s)] = (y0, (kk + 1.0) / (nn + 2.0))
            if hist and last12:
                out[("forecast.conflict_base_rate", g, s)] = (
                    math.fsum(last12) / len(last12),
                    (k + 1.0) / (len(hist) + 2.0),
                )
    return out


def _losses(point: float, prob: float, y: float) -> dict[str, float]:
    o = 1 if y >= THRESHOLD else 0
    pc = min(max(prob, PROB_FLOOR), 1 - PROB_FLOOR)
    return {
        "o": o,
        "brier": (prob - o) ** 2,
        "logloss": -math.log(pc if o else 1 - pc),
        "msle": (math.log1p(max(point, 0.0)) - math.log1p(y)) ** 2,
        "abs_error": abs(point - y),
    }


def evaluate_runs(
    runs: Mapping[str, ViewsRun], outcome: MonthlyPanel, inputs: MonthlyPanel, *, label: str
) -> dict[str, Any]:
    """Score VIEWS runs and the baselines (fitted on ``inputs`` up to each run's last input month) on the months of
    ``outcome``'s coverage; DM tests baseline vs VIEWS per (family, step group), cluster-robust by country, Holm."""
    rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    used = []
    assert outcome.covered is not None
    for vr in sorted(runs.values(), key=lambda r: r.run):
        origin = midx(*vr.last_input)
        steps = [s for s in range(1, 13) if outcome.covered[0] <= origin + s <= outcome.covered[1]]
        if not steps or inputs.covered is None or origin > inputs.covered[1]:
            continue
        geos = {g for g, _y, _m in vr.monthly} & inputs.geos(vr.last_input[0])
        base = baseline_forecasts(inputs, origin, steps, geos)
        n = 0
        for g in sorted(geos):
            for s in steps:
                ti = origin + s
                y = outcome.y(g, ti)
                key = (g, ti // 12, ti % 12 + 1)
                if y is None or key not in vr.monthly or key not in vr.prob:
                    continue
                common = {"geo": g, "run": vr.run, "step": s, "month": f"{key[1]}-{key[2]:02d}", "y": y}
                rows[VIEWS].append({**common, **_losses(vr.monthly[key], vr.prob[key], y)})
                for mk in BASELINES:
                    bf = base.get((mk, g, s))
                    if bf is not None:
                        rows[mk].append({**common, **_losses(bf[0], bf[1], y)})
                n += 1
        if n:
            used.append({"run": vr.run, "release_date": vr.release_date, "last_input": list(vr.last_input), "n": n})
    summary = []
    tests: dict[str, dict[str, Any]] = {}
    pv: dict[str, float | None] = {}
    for lo, hi in STEP_GROUPS:
        grp = {mk: [r for r in rs if lo <= r["step"] <= hi] for mk, rs in rows.items()}
        for mk, rs in sorted(grp.items()):
            if rs:
                summary.append(
                    {
                        "model": mk,
                        "steps": f"{lo}-{hi}",
                        "n": len(rs),
                        **{m: math.fsum(r[m] for r in rs) / len(rs) for m in ("brier", "logloss", "msle", "abs_error")},
                        "event_rate": sum(r["o"] for r in rs) / len(rs),
                    }
                )
        iv = {(r["geo"], r["run"], r["step"]): r for r in grp.get(VIEWS, [])}
        for mk in BASELINES:
            pairs = [
                (r, iv[(r["geo"], r["run"], r["step"])])
                for r in grp.get(mk, [])
                if (r["geo"], r["run"], r["step"]) in iv
            ]
            for metric in ("brier", "logloss", "msle"):
                k = f"cm|steps{lo}-{hi}|{mk}|vs|{VIEWS}|{metric}"
                tests[k] = {
                    "model": mk,
                    "steps": f"{lo}-{hi}",
                    "metric": metric,
                    "model_mean": math.fsum(a[metric] for a, _ in pairs) / len(pairs) if pairs else None,
                    "views_mean": math.fsum(b[metric] for _, b in pairs) / len(pairs) if pairs else None,
                    **dm_test([a[metric] - b[metric] for a, b in pairs], [a["geo"] for a, _ in pairs]),
                }
                pv[k] = tests[k]["p_two_sided"]
    adj = holm(pv)
    for k, t in tests.items():
        t["p_holm_two_sided"] = adj.get(k)
    return {"label": label, "runs": used, "summary": summary, "tests": tests}


__all__ = [
    "CAND_TABLE",
    "FINAL_TABLE",
    "INDICATOR",
    "MonthlyPanel",
    "baseline_forecasts",
    "build_monthly",
    "coverage_of",
    "evaluate_runs",
    "midx",
]
