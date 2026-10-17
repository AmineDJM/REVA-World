"""Quantile forecasters and proper scores for health surveillance series (V6, agent PRED-HEALTH). Pure numerics.

Forecasts are sets of quantiles at the 23 levels the US CDC forecast hubs use (:data:`HUB_LEVELS`), so REVA's
baselines and the hubs' ensembles are scored with the same metric, the weighted interval score (WIS, Bracher et al.
2021, PLoS Comput Biol 17(2): e1008618):

    WIS = (1 / (K + 1/2)) * sum over the 2K+1 levels tau of rho_tau(y - q_tau),

with rho_tau the pinball loss (equivalently ``(1/(K+1/2)) (|y - m|/2 + sum_k alpha_k/2 IS_alpha_k)``).

Models (``history`` = ``[(period index, value), ...]`` ascending, consecutive indices not required; ``steps`` =
target index - last observed index >= 1):

* ``health.persistence`` (baseline, in the spirit of the FluSight baseline): median = last value; the other quantiles
  are the last value plus the empirical quantiles of the *symmetrised* ``steps``-step changes of the trailing window.
* ``health.climatology`` (baseline, weekly only): empirical quantiles of the values observed in the same season week
  (+/- 2 weeks) of earlier seasons (at least 26 weeks before the target; >= 3 distinct years).
* ``health.log_ar`` (candidate, weekly only): AR(2) on weekly differences of ``log(y + 1)`` fitted by OLS on the
  trailing window, ``steps``-ahead distribution by residual bootstrap (deterministic seed from the training data).
* ``health.log_drift`` (candidate, annual only): random walk with drift on ``log(y)`` (drift = mean change of the
  trailing window), quantiles from the empirical ``steps``-step deviations around the drift.
* ``health.blend`` (candidate): quantile average (Vincentisation) of the other models that did not abstain.

Every model returns :class:`Abstain` (with a reason) instead of a forecast it cannot support. Values are clipped to
``[lower, upper]`` (counts and rates >= 0; percentages <= 100).
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

HUB_LEVELS: tuple[float, ...] = (
    0.01, 0.025, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.35, 0.4, 0.45, 0.5,
    0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 0.975, 0.99,
)  # fmt: skip
WINDOW = {"W": 156, "A": 25}
MIN_CHANGES = {"W": 20, "A": 8}
AR_WINDOW = 104
AR_MIN = 52
BOOT = 2000
CLIM_HALF_WIDTH = 2
CLIM_MIN_YEARS = 3


@dataclass(frozen=True)
class Abstain:
    reason: str


@dataclass(frozen=True)
class QuantileForecast:
    levels: tuple[float, ...]
    values: tuple[float, ...]
    detail: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if len(self.levels) != len(self.values):
            raise ValueError("levels and values differ in length")
        if any(b < a - 1e-9 for a, b in zip(self.values, self.values[1:], strict=False)):
            raise ValueError("quantiles must be non-decreasing")

    @property
    def median(self) -> float:
        return self.quantile(0.5)

    def quantile(self, level: float) -> float:
        for lv, v in zip(self.levels, self.values, strict=True):
            if abs(lv - level) < 1e-12:
                return v
        raise KeyError(level)


Result = QuantileForecast | Abstain


def _finish(
    qs: Sequence[float], lower: float | None, upper: float | None, detail: Mapping[str, Any]
) -> QuantileForecast:
    arr = np.asarray(qs, dtype=float)
    if lower is not None:
        arr = np.maximum(arr, lower)
    if upper is not None:
        arr = np.minimum(arr, upper)
    arr = np.maximum.accumulate(arr)
    return QuantileForecast(HUB_LEVELS, tuple(float(x) for x in arr), dict(detail))


def _trail(history: Sequence[tuple[int, float]], window: int) -> list[tuple[int, float]]:
    if not history:
        return []
    last = history[-1][0]
    return [(i, v) for i, v in history if i > last - window]


def _changes(history: Sequence[tuple[int, float]], steps: int) -> list[float]:
    by = dict(history)
    return [by[i + steps] - v for i, v in history if (i + steps) in by]


def persistence(
    history: Sequence[tuple[int, float]],
    steps: int,
    *,
    freq: str,
    lower: float | None = 0.0,
    upper: float | None = None,
) -> Result:
    if not history:
        return Abstain("no history")
    tr = _trail(history, WINDOW[freq])
    ch = _changes(tr, steps)
    if len(ch) < MIN_CHANGES[freq]:
        return Abstain(f"fewer than {MIN_CHANGES[freq]} {steps}-step changes in the trailing window")
    sym = np.concatenate([np.asarray(ch), -np.asarray(ch)])
    last = history[-1][1]
    qs = last + np.quantile(sym, HUB_LEVELS)
    qs[HUB_LEVELS.index(0.5)] = last
    return _finish(qs, lower, upper, {"n_changes": len(ch), "last": last})


def climatology(
    history: Sequence[tuple[int, float]],
    target: int,
    season_week_of: Callable[[int], int],
    year_of: Callable[[int], int],
    *,
    lower: float | None = 0.0,
    upper: float | None = None,
    exclude_recent: int = 26,
) -> Result:
    """Values in season weeks ``season_week_of(target) +/- 2`` observed at least ``exclude_recent`` weeks before the
    target (earlier seasons only), from >= 3 distinct years."""

    def dist(a: int, b: int) -> int:
        d = abs(a - b)
        return min(d, 52 - d)

    tw = season_week_of(target)
    pool = [
        (i, v) for i, v in history if i <= target - exclude_recent and dist(season_week_of(i), tw) <= CLIM_HALF_WIDTH
    ]
    years = {year_of(i) for i, _ in pool}
    if len(years) < CLIM_MIN_YEARS or len(pool) < 3 * CLIM_MIN_YEARS:
        return Abstain(f"fewer than {CLIM_MIN_YEARS} earlier years of the same season week")
    qs = np.quantile(np.asarray([v for _, v in pool]), HUB_LEVELS)
    return _finish(qs, lower, upper, {"n_values": len(pool), "n_years": len(years)})


def _seed(history: Sequence[tuple[int, float]], steps: int) -> int:
    h = hashlib.sha256(repr([(i, round(v, 10)) for i, v in history[-AR_WINDOW:]] + [steps]).encode()).digest()
    return int.from_bytes(h[:8], "big")


def log_ar(
    history: Sequence[tuple[int, float]], steps: int, *, lower: float | None = 0.0, upper: float | None = None
) -> Result:
    tr = _trail(history, AR_WINDOW)
    idx = [i for i, _ in tr]
    if len(tr) < AR_MIN or idx != list(range(idx[0], idx[0] + len(idx))):
        return Abstain(f"needs {AR_MIN}+ consecutive weeks in the trailing {AR_WINDOW}")
    y = np.asarray([v for _, v in tr], dtype=float)
    if (y < 0).any():
        return Abstain("negative values: log(y + 1) undefined")
    z = np.log1p(y)
    d = np.diff(z)
    x = np.column_stack([np.ones(len(d) - 2), d[1:-1], d[:-2]])
    t = d[2:]
    coef, *_ = np.linalg.lstsq(x, t, rcond=None)
    res = t - x @ coef
    res = res - res.mean()
    p1, p2 = float(coef[1]), float(coef[2])
    margin = 0.02  # AR(2) stationarity triangle, kept away from the unit root
    if not np.isfinite(coef).all() or not (p1 + p2 < 1 - margin and p2 - p1 < 1 - margin and abs(p2) < 1 - margin):
        return Abstain("AR(2) on differences is not stationary")
    rng = np.random.default_rng(_seed(history, steps))
    e = rng.choice(res, size=(steps, BOOT), replace=True)
    d1, d2 = np.full(BOOT, d[-1]), np.full(BOOT, d[-2])
    paths = np.full(BOOT, z[-1])
    for s in range(steps):
        nd = coef[0] + coef[1] * d1 + coef[2] * d2 + e[s]
        paths = paths + nd
        d2, d1 = d1, nd
    vals = np.expm1(np.clip(paths, -50, 50))
    qs = np.quantile(vals, HUB_LEVELS)
    return _finish(qs, lower, upper, {"coef": [float(c) for c in coef], "n_fit": len(t), "boot": BOOT})


def log_drift(
    history: Sequence[tuple[int, float]], steps: int, *, lower: float | None = 0.0, upper: float | None = None
) -> Result:
    tr = _trail(history, WINDOW["A"])
    if len(tr) < 10 or any(v <= 0 for _, v in tr):
        return Abstain("needs 10+ strictly positive annual values")
    lz = [(i, math.log(v)) for i, v in tr]
    one = _changes(lz, 1)
    if len(one) < 8:
        return Abstain("fewer than 8 annual changes")
    drift = float(np.mean(one[-10:]))
    dev = [c - steps * drift for c in _changes(lz, steps)]
    if len(dev) < 6:
        return Abstain(f"fewer than 6 {steps}-step changes")
    sym = np.concatenate([np.asarray(dev), -np.asarray(dev)])
    qs = np.exp(lz[-1][1] + steps * drift + np.quantile(sym, HUB_LEVELS))
    return _finish(qs, lower, upper, {"drift_log": drift, "n_changes": len(dev)})


def blend(components: Mapping[str, Result], *, lower: float | None = 0.0, upper: float | None = None) -> Result:
    ok = {k: v for k, v in components.items() if isinstance(v, QuantileForecast)}
    if len(ok) < 2:
        return Abstain("fewer than 2 component forecasts")
    arr = np.mean([np.asarray(v.values) for v in ok.values()], axis=0)
    return _finish(arr, lower, upper, {"components": sorted(ok)})


# --------------------------------------------------------------------------- scores


def pinball(y: float, q: float, tau: float) -> float:
    u = y - q
    return u * (tau - (1.0 if u < 0 else 0.0))


def wis(levels: Sequence[float], values: Sequence[float], y: float) -> float:
    """Weighted interval score of a symmetric quantile set containing the median (2K+1 levels)."""
    n = len(levels)
    if n % 2 != 1 or 0.5 not in [round(lv, 12) for lv in levels]:
        raise ValueError("WIS needs an odd, symmetric level set containing 0.5")
    k = (n - 1) / 2
    return float(sum(pinball(y, q, t) for t, q in zip(levels, values, strict=True)) / (k + 0.5))


def coverage(levels: Sequence[float], values: Sequence[float], y: float, central: float) -> bool | None:
    lo, hi = round((1 - central) / 2, 6), round(1 - (1 - central) / 2, 6)
    lv = [round(x, 6) for x in levels]
    if lo not in lv or hi not in lv:
        return None
    return values[lv.index(lo)] <= y <= values[lv.index(hi)]


def score(levels: Sequence[float], values: Sequence[float], y: float) -> dict[str, Any]:
    med = values[[round(x, 12) for x in levels].index(0.5)]
    return {
        "wis": wis(levels, values, y),
        "ae_median": abs(y - med),
        "cover50": coverage(levels, values, y, 0.5),
        "cover90": coverage(levels, values, y, 0.9),
    }


def dm_test(diffs: Sequence[float], lag: int) -> dict[str, Any]:
    """Diebold-Mariano test of mean(d) = 0 with a Newey-West long-run variance (Bartlett, ``lag``) and the
    Harvey-Leybourne-Newbold small-sample correction; two-sided p-value from Student t with T-1 df.
    ``d`` = loss(model) - loss(benchmark): negative mean = model better."""
    from scipy import stats

    d = np.asarray(diffs, dtype=float)
    n = len(d)
    if n < 8:
        return {"n": n, "status": "too_few_origins"}
    mu = float(d.mean())
    e = d - mu
    lrv = float(e @ e / n)
    for j in range(1, min(lag, n - 1) + 1):
        lrv += 2 * (1 - j / (lag + 1)) * float(e[j:] @ e[:-j] / n)
    if lrv <= 0:
        return {"n": n, "mean_diff": mu, "status": "degenerate_variance"}
    dm = mu / math.sqrt(lrv / n)
    h = lag + 1
    corr = math.sqrt(max((n + 1 - 2 * h + h * (h - 1) / n) / n, 1e-12))
    stat = dm * corr
    p = float(2 * stats.t.sf(abs(stat), df=n - 1))
    return {"n": n, "mean_diff": mu, "dm": dm, "hln": stat, "p_value": p, "lag": lag, "status": "ok"}


__all__ = [
    "HUB_LEVELS",
    "Abstain",
    "QuantileForecast",
    "blend",
    "climatology",
    "coverage",
    "dm_test",
    "log_ar",
    "log_drift",
    "persistence",
    "pinball",
    "score",
    "wis",
]
