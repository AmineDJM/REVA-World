"""REVA's nowcast candidates and the second pass (combinations, bias correction, densities, fair benchmark).

Pure REVA models (no official nowcast as input; inputs selected at the REVA cutoff, see ``cases``):

* ``forecast.nc_bridge`` — GDP bridge: OLS of first-release real GDP growth (annualised q/q) on the annualised
  q/q growth of the quarterly mean of five monthly indicators (payrolls, industrial production, aggregate hours,
  housing starts, real PCE) taken from the RTDSM monthly vintage in force; unpublished months of the target (and
  previous) quarter filled by an AR(1) on monthly log changes of the same vintage; estimation on quarters from
  1993Q1 whose first release is known, excluding 2020Q2-2020Q3 (pandemic outliers); >= 30 quarters.
* ``forecast.nc_dfm`` — the V6 one-factor DFM nowcast (:func:`reva.models.forecasting.dfm.dfm_nowcast`, unchanged)
  on the same panel (monthly 100 x log changes from 1992-01).
* ``forecast.nc_cpi_bridge`` — headline CPI m/m: OLS on the mean of the last 12 known m/m values and the log change
  of the monthly mean NY Harbor gasoline spot price (EIA; days usable at the cutoff); core CPI m/m: OLS on the
  means of the last 12 and last 3 known core m/m values.

Second pass (time order of the official publication day; only errors whose outcome is known at the REVA cutoff,
in the same **bucket** = phase of the nowcast within the target period; last ``window`` target periods):

* ``forecast.nc_stack_reva`` — **uses the official nowcast as an input**: ``O + lam (R - O)``, R = mean of the pure
  REVA points available; ``lam`` = least squares of past (y - O) on past (R - O), clipped to [0, 1], shrunk toward
  0.5 with a prior worth ``n0`` target periods.
* ``forecast.nc_stack_all`` (GDP) — same with R = mean of REVA's points and the *other* official nowcast published
  by the same day (GDPNow <-> New York Fed).
* ``forecast.nc_biascorr`` — **uses the official nowcast**: ``O + b``, b = mean past error of the official in the
  bucket shrunk toward 0 (``n / (n + n0)``, n = past target periods).
* densities: candidate point + its own past errors in the bucket (empirical; >= ``min_periods`` periods and
  ``min_errors`` errors), else point only; fair benchmark ``<official>_errfan`` = N(O, RMS of the official's own
  past errors in the bucket and window) (QA-PRED X1).
"""

from __future__ import annotations

import bisect
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import numpy as np

from reva.evaluation.pred_models.realtime import Case
from reva.models.forecasting.dfm import dfm_nowcast
from reva.models.forecasting.registry_baselines import Abstain, Predictive

GDP_INDICATORS = ("EMPLOY", "IPT", "H", "HSTARTS", "RCONM")
BRIDGE_FIRST_Q = 1993 * 4  # 1993Q1
DFM_FIRST_M = 1992 * 12  # 1992-01
PANDEMIC_QIS = frozenset({2020 * 4 + 1, 2020 * 4 + 2})  # 2020Q2, 2020Q3
MIN_BRIDGE_Q = 30
AR_WINDOW = 120
CPI_FIRST_M = 2000 * 12
MIN_CPI_MONTHS = 18
MAX_SAMPLE = 400
CLIP = 10.0


def key(name: str) -> str:
    return f"forecast.nc_{name}"


def fan_key(official: str) -> str:
    return f"{official}_errfan"


PURE = ("bridge", "dfm", "cpi_bridge")
COMBOS = ("stack_reva", "stack_all", "biascorr")


# ------------------------------------------------------------------------------------------------ GDP pure models
def fill_monthly(levels: Mapping[int, float], upto: int) -> dict[int, float]:
    """Log-levels extended to month ``upto`` by an AR(1) (with mean) on the last ``AR_WINDOW`` monthly log changes."""
    if not levels:
        return {}
    ms = sorted(m for m, v in levels.items() if v > 0)
    lv = {m: math.log(levels[m]) for m in ms}
    last = ms[-1]
    out = dict(lv)
    if last >= upto:
        return out
    d = [lv[m] - lv[m - 1] for m in ms if m - 1 in lv][-AR_WINDOW:]
    if len(d) < 24:
        return out
    x, y = np.asarray(d[:-1]), np.asarray(d[1:])
    mu = float(np.mean(d))
    phi = float(np.clip(((x - mu) @ (y - mu)) / max(float((x - mu) @ (x - mu)), 1e-12), -0.9, 0.9))
    prev = d[-1]
    cur = lv[last]
    for m in range(last + 1, upto + 1):
        prev = mu + phi * (prev - mu)
        cur += prev
        out[m] = cur
    return out


def q_growth(loglev: Mapping[int, float], qi: int) -> float | None:
    """Annualised q/q growth (percent, log approximation x 400) of the quarterly mean level."""
    a = [loglev.get(3 * qi + k) for k in range(3)]
    b = [loglev.get(3 * (qi - 1) + k) for k in range(3)]
    if any(v is None for v in a + b):
        return None
    ma = math.log(sum(math.exp(v) for v in a if v is not None) / 3)
    mb = math.log(sum(math.exp(v) for v in b if v is not None) / 3)
    return 400.0 * (ma - mb)


def gdp_bridge(
    panel: Mapping[str, Mapping[int, float]], g: Mapping[int, float], target_qi: int
) -> Predictive | Abstain:
    raw = {k: {m: math.log(v) for m, v in panel[k].items() if v > 0} for k in GDP_INDICATORS if k in panel}
    if len(raw) < len(GDP_INDICATORS):
        return Abstain("indicator missing")
    filled = {k: fill_monthly(panel[k], 3 * target_qi + 2) for k in GDP_INDICATORS}
    rows, ys = [], []
    for qi in sorted(g):
        if qi < BRIDGE_FIRST_Q or qi >= target_qi or qi in PANDEMIC_QIS:
            continue
        xs = [q_growth(raw[k], qi) for k in GDP_INDICATORS]
        if any(x is None for x in xs):
            continue
        rows.append([1.0, *xs])
        ys.append(g[qi])
    if len(rows) < MIN_BRIDGE_Q:
        return Abstain(f"fewer than {MIN_BRIDGE_Q} bridge quarters")
    X, y = np.asarray(rows, dtype=float), np.asarray(ys, dtype=float)
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    x0 = [q_growth(filled[k], target_qi) for k in GDP_INDICATORS]
    if any(v is None for v in x0):
        return Abstain("target-quarter indicator missing after the fill")
    point = float(np.asarray([1.0, *x0]) @ coef)
    if not math.isfinite(point):
        return Abstain("non-finite")
    return Predictive("point", point, detail=(("n_bridge", str(len(ys))),))


def gdp_dfm(panel: Mapping[str, Mapping[int, float]], g: Mapping[int, float], target_qi: int) -> Predictive | Abstain:
    last_m = 3 * target_qi + 2
    months = list(range(DFM_FIRST_M, last_m + 1))
    X = np.full((len(months), len(GDP_INDICATORS)), np.nan)
    for j, k in enumerate(GDP_INDICATORS):
        s = panel.get(k, {})
        for i, m in enumerate(months):
            a, b = s.get(m), s.get(m - 1)
            if a and b and a > 0 and b > 0:
                X[i, j] = 100.0 * math.log(a / b)
    q0 = DFM_FIRST_M // 3
    mq = np.asarray([m // 3 - q0 for m in months])
    gq = np.full(target_qi - q0 + 1, np.nan)
    for qi, v in g.items():
        if q0 <= qi < target_qi:
            gq[qi - q0] = v
    p = dfm_nowcast(X, mq, gq, target_qi - q0)
    return p


# ------------------------------------------------------------------------------------------------ CPI pure models
def _ols(rows: list[list[float]], ys: list[float], x0: list[float]) -> float | None:
    X, y = np.asarray(rows, dtype=float), np.asarray(ys, dtype=float)
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    v = float(np.asarray(x0) @ coef)
    return v if math.isfinite(v) else None


def cpi_headline_bridge(hist: Mapping[int, float], gas: Mapping[int, float], gas_target: float | None,
                        target_mi: int) -> Predictive | Abstain:  # fmt: skip
    """``hist`` m/m history known at the cutoff; ``gas`` monthly log changes (x100) of complete months;
    ``gas_target`` the target month's change from the days usable at the cutoff."""
    rows, ys = [], []
    for m in sorted(hist):
        if m < CPI_FIRST_M or m >= target_mi or m not in gas:
            continue
        lags = [hist[j] for j in range(m - 12, m) if j in hist]
        if len(lags) < 12:
            continue
        rows.append([1.0, sum(lags) / 12, gas[m]])
        ys.append(hist[m])
    if len(rows) < MIN_CPI_MONTHS or gas_target is None:
        return Abstain("too few months or no gasoline price usable")
    lags = [hist[j] for j in range(target_mi - 13, target_mi) if j in hist][-12:]
    if len(lags) < 9:
        return Abstain("fewer than 9 recent CPI months known")
    v = _ols(rows, ys, [1.0, sum(lags) / len(lags), gas_target])
    return Predictive("point", v, detail=(("n", str(len(ys))),)) if v is not None else Abstain("non-finite")


def cpi_core_bridge(hist: Mapping[int, float], target_mi: int) -> Predictive | Abstain:
    def feats(m: int) -> list[float] | None:
        l12 = [hist[j] for j in range(m - 13, m) if j in hist][-12:]
        l3 = [hist[j] for j in range(m - 4, m) if j in hist][-3:]
        if len(l12) < 9 or len(l3) < 2:
            return None
        return [1.0, sum(l12) / len(l12), sum(l3) / len(l3)]

    rows, ys = [], []
    for m in sorted(hist):
        if m >= target_mi:
            continue
        f = feats(m)
        if f is not None:
            rows.append(f)
            ys.append(hist[m])
    x0 = feats(target_mi)
    if len(rows) < MIN_CPI_MONTHS or x0 is None:
        return Abstain("too few core months known")
    v = _ols(rows, ys, x0)
    return Predictive("point", v, detail=(("n", str(len(ys))),)) if v is not None else Abstain("non-finite")


# ------------------------------------------------------------------------------------------------ second pass
@dataclass(frozen=True)
class Rules:
    official: str
    window: int
    n0: float
    min_periods: int
    min_errors: int
    stacks: tuple[str, ...]  # which stack models this cell has


@dataclass
class _Book:
    known: list[float] = field(default_factory=list)
    rows: list[tuple[float, object]] = field(default_factory=list)

    def add(self, known_at: float, period: float, value: object) -> None:
        i = bisect.bisect_right(self.known, known_at)
        self.known.insert(i, known_at)
        self.rows.insert(i, (period, value))

    def window(self, t: float, n: int) -> tuple[list[object], int]:
        avail = self.rows[: bisect.bisect_right(self.known, t)]
        periods = sorted({p for p, _ in avail})[-n:]
        keep = set(periods)
        return [v for p, v in avail if p in keep], len(periods)


def shrunk_lambda(pairs: Sequence[tuple[float, float]], n: int, n0: float) -> float | None:
    sxy = math.fsum(max(-CLIP, min(CLIP, a)) * max(-CLIP, min(CLIP, b)) for a, b in pairs)
    sxx = math.fsum(max(-CLIP, min(CLIP, b)) ** 2 for _, b in pairs)
    if sxx <= 0:
        return None
    lam = min(1.0, max(0.0, sxy / sxx))
    return (n * lam + n0 * 0.5) / (n + n0)


def _thin(xs: list[float]) -> tuple[float, ...]:
    s = sorted(xs)
    if len(s) <= MAX_SAMPLE:
        return tuple(s)
    n = len(s)
    return tuple(s[int((i + 0.5) * n / MAX_SAMPLE)] for i in range(MAX_SAMPLE))


def run_second_pass(cases: Sequence[Case], rules: Rules) -> None:
    """In place. ``case.info``: ``reva_cut`` (POSIX seconds of the REVA cutoff), ``raw`` (pure points), ``other``
    (the other official point, optional); ``case.preds[rules.official]`` is the official point.

    Error books are keyed by the outcome's ``known_at`` instant and queried at the case's REVA cutoff, so a case
    only ever sees errors of outcomes published by then (filling a book up front is therefore safe). Sweep 1:
    stacks / bias / fair fan (books of official errors and of (y - O, R - O) pairs: first-pass values only);
    then the books of every model's errors; sweep 2: the candidates' empirical densities."""
    off = rules.official
    pairs: dict[tuple[str, str], _Book] = defaultdict(_Book)
    errs: dict[tuple[str, str], _Book] = defaultdict(_Book)
    order = sorted(cases, key=lambda c: (c.info["reva_cut"], c.origin, c.task))
    # books of pairs and official errors depend only on first-pass values: fill them up front (queried by instant)
    for c in order:
        if c.outcome is None or c.outcome_known_at is None:
            continue
        wp = c.preds.get(off)
        if not isinstance(wp, Predictive):
            continue
        k, p, b, w = c.outcome_known_at.timestamp(), c.origin.timestamp(), c.horizon, wp.point
        raw = c.info.get("raw", {})
        errs[(b, "official")].add(k, p, c.outcome - w)
        reva = [raw[m] for m in ("bridge", "dfm", "cpi_bridge") if m in raw]
        if reva:
            pairs[(b, "stack_reva")].add(k, p, (c.outcome - w, sum(reva) / len(reva) - w))
        allv = reva + ([c.info["other"]] if c.info.get("other") is not None else [])
        if allv:
            pairs[(b, "stack_all")].add(k, p, (c.outcome - w, sum(allv) / len(allv) - w))
    # sweep 1: stack / bias points
    for c in order:
        t = float(c.info["reva_cut"])
        b = c.horizon
        wp = c.preds.get(off)
        raw = c.info.setdefault("raw", {})
        if not isinstance(wp, Predictive):
            continue
        w = wp.point
        reva = [raw[m] for m in ("bridge", "dfm", "cpi_bridge") if m in raw]
        alts = {"stack_reva": reva, "stack_all": reva + ([c.info["other"]] if c.info.get("other") is not None else [])}
        for st in rules.stacks:
            a = alts[st]
            if not a:
                continue
            pv, n = pairs[(b, st)].window(t, rules.window)
            if n < rules.min_periods:
                continue
            lam = shrunk_lambda(pv, n, rules.n0)  # type: ignore[arg-type]
            if lam is None:
                continue
            r = sum(a) / len(a)
            raw[st] = w + lam * (r - w)
            c.info[f"{st}_lambda"] = round(lam, 4)
        ev, n = errs[(b, "official")].window(t, rules.window)
        if n >= rules.min_periods:
            bias = math.fsum(float(e) for e in ev) / len(ev)  # type: ignore[arg-type]
            raw["biascorr"] = w + n / (n + rules.n0) * bias
            c.info["bias_shrunk"] = round(n / (n + rules.n0) * bias, 4)
        if n >= rules.min_periods and len(ev) >= rules.min_errors:
            rms = math.sqrt(math.fsum(float(e) ** 2 for e in ev) / len(ev))  # type: ignore[arg-type]
            c.preds[fan_key(off)] = Predictive("normal", w, mu=w, sigma=rms) if rms > 0 else Abstain("zero")
        else:
            c.preds[fan_key(off)] = Abstain("too few past official errors known at the cutoff")
    # model error books (now that every point exists), then sweep 2: densities
    for c in order:
        if c.outcome is None or c.outcome_known_at is None:
            continue
        k, p, b = c.outcome_known_at.timestamp(), c.origin.timestamp(), c.horizon
        for m, v in c.info.get("raw", {}).items():
            errs[(b, m)].add(k, p, c.outcome - v)
    for c in order:
        t = float(c.info["reva_cut"])
        for m, v in c.info.get("raw", {}).items():
            ev, n = errs[(c.horizon, m)].window(t, rules.window)
            if n >= rules.min_periods and len(ev) >= rules.min_errors:
                c.preds[key(m)] = Predictive("empirical", v, sample=_thin([v + float(e) for e in ev]))  # type: ignore[arg-type]
            else:
                c.preds[key(m)] = Predictive("point", v)


__all__ = [
    "COMBOS",
    "GDP_INDICATORS",
    "PANDEMIC_QIS",
    "PURE",
    "Rules",
    "cpi_core_bridge",
    "cpi_headline_bridge",
    "fan_key",
    "fill_monthly",
    "gdp_bridge",
    "gdp_dfm",
    "key",
    "q_growth",
    "run_second_pass",
    "shrunk_lambda",
]
