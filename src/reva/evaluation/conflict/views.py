"""VIEWS forecasts from the atlas table ``atlas.views.cm_sb`` -> per-run annual sums (pure).

VIEWS forecasts country-months. REVA's country-year quantity questions get a VIEWS reference value only where a
run's window covers all twelve months of the calendar year: the **sum of the twelve monthly point predictions**
(``main_mean``; for ``fatalities001`` runs ``expm1`` of ``sc_cm_sb_main``, which VIEWS states in ln(fatalities+1)).
This aggregation is REVA's; VIEWS' point predictions come from a model fitted on the log scale, so the sum is a sum
of point predictions, not an expected annual total. No calendar-year probability is derived from the monthly
``main_dich`` values (that would need a dependence model REVA does not have): VIEWS abstains on binary country-year
questions.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import polars as pl

FATALITY_VARS = {"main_mean": "level", "sc_cm_sb_main": "ln1p"}
PROB_VARS = ("main_dich", "sc_cm_sb_dich_main")


@dataclass
class ViewsRun:
    run: str
    release_date: str | None
    last_input: tuple[int, int]
    variable: str
    monthly: dict[tuple[str, int, int], float] = field(default_factory=dict)  # (gw, year, month) -> fatalities
    refs: dict[tuple[str, int, int], dict[str, Any]] = field(default_factory=dict)
    prob: dict[tuple[str, int, int], float] = field(default_factory=dict)  # P(>= 25 BRD in the country-month)

    def annual(self, gw: str, year: int) -> float | None:
        vals = [self.monthly.get((gw, year, m)) for m in range(1, 13)]
        if any(v is None for v in vals):
            return None
        return math.fsum(v for v in vals if v is not None)

    def covers_year(self, year: int) -> bool:
        ly, lm = self.last_input
        return (year, 1) > (ly, lm)

    def geos(self) -> set[str]:
        return {k[0] for k in self.monthly}


def runs_from_frame(frame: pl.DataFrame) -> dict[str, ViewsRun]:
    out: dict[str, ViewsRun] = {}
    f = frame.filter(pl.col("indicator").str.split("|").list.get(1).is_in([*FATALITY_VARS, *PROB_VARS]))
    for r in f.select(
        "indicator", "provider_geo", "period_start", "value", "flags", "raw_sha256", "selector"
    ).iter_rows(named=True):
        run, var = r["indicator"].split("|", 1)
        fl = json.loads(r["flags"]) if r["flags"] else {}
        up = fl.get("projection_upstream") or {}
        vr = out.get(run)
        if vr is None:
            gen_year, gen_month = run.split("_")[1:3]
            vr = ViewsRun(run, up.get("release_date"), (int(gen_year), int(gen_month)), var)
            out[run] = vr
        if var in FATALITY_VARS:
            vr.variable = var
        v = float(Decimal(str(r["value"])))
        ps = r["period_start"]
        if var in PROB_VARS:
            vr.prob[(str(r["provider_geo"]), ps.year, ps.month)] = v
            continue
        if FATALITY_VARS[var] == "ln1p":
            v = math.expm1(v)
        key = (str(r["provider_geo"]), ps.year, ps.month)
        vr.monthly[key] = v
        vr.refs[key] = {"raw_sha256": r["raw_sha256"], "selector": r["selector"]}
    return out


def december_runs(runs: dict[str, ViewsRun]) -> dict[int, ViewsRun]:
    """Origin year -> the run whose last input month is December of that year (one per year: the newest model
    generation if several)."""
    out: dict[int, ViewsRun] = {}
    for vr in sorted(runs.values(), key=lambda r: r.run):
        ly, lm = vr.last_input
        if lm == 12:
            out[ly] = vr
    return out


def latest_covering(runs: dict[str, ViewsRun], year: int, released_before: str) -> ViewsRun | None:
    """The newest run released on or before ``released_before`` (ISO date) whose window covers all of ``year``."""
    best = None
    for vr in runs.values():
        rd = vr.release_date or ""
        if not (len(rd) == 10 and rd <= released_before) or not vr.covers_year(year):
            continue
        if not any(vr.monthly.get((g, year, 12)) is not None for g in list(vr.geos())[:5]):
            continue
        if best is None or vr.last_input > best.last_input:
            best = vr
    return best


def annual_table(vr: ViewsRun) -> dict[tuple[str, int], float]:
    years = defaultdict(set)
    for g, y, _m in vr.monthly:
        years[g].add(y)
    out = {}
    for g, ys in years.items():
        for y in ys:
            v = vr.annual(g, y)
            if v is not None:
                out[(g, y)] = v
    return out


__all__ = [
    "FATALITY_VARS",
    "PROB_VARS",
    "ViewsRun",
    "annual_table",
    "december_runs",
    "latest_covering",
    "runs_from_frame",
]
