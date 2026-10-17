"""Baseline forecasters of the prospective forecast registry (V5, agent PRED-REGISTRY). Pure numerics, no I/O.

The registry asks questions about regular-frequency series (monthly, quarterly, annual) indexed by an integer
*period index* (``year*12 + month-1``, ``year*4 + quarter-1`` or ``year``). Every baseline sees only the
history it is given (data published at issue time, fitted as of that time) and returns a full predictive
distribution for one target period, or an :class:`Abstain` with the reason (never a silent repair):

* ``persistence`` — random walk: point = last observed value; Gaussian spread from the root mean square of the
  historical ``h``-step changes (zero-mean random-walk errors).
* ``seasonal_naive`` — the value of the same season in the most recent observed year; Gaussian spread from the
  root mean square of the historical seasonal-naive errors at the same lag. Monthly / quarterly only.
* ``ar`` — AR(p) with intercept on levels, OLS on the last window, ``p`` by AIC on a common sample; iterated
  forecast with the MA(∞) (psi-weight) ``h``-step variance; a non-stationary fit abstains.
* ``hist_quantile`` — last value plus the empirical distribution of the historical ``h``-step changes
  (historical-quantile intervals); point = median.

Only the final consecutive run of the history is used (a gap ends the usable history). Binary probabilities from
an empirical predictive use the rule of succession ``(k + 1) / (n + 2)`` so that no baseline states certainty.
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

import numpy as np

Family = Literal["normal", "empirical", "point"]

#: model_key -> (version, one-line description)
BASELINES: dict[str, tuple[str, str]] = {
    "forecast.registry_persistence": ("1.0.0", "random walk / persistence with Gaussian h-step spread"),
    "forecast.registry_seasonal_naive": ("1.0.0", "seasonal naive (same season, most recent year)"),
    "forecast.registry_ar": ("1.0.0", "AR(p) on levels, OLS fitted as of issue, p by AIC"),
    "forecast.registry_hist_quantile": ("1.0.0", "last value + empirical h-step changes (historical quantiles)"),
}

MIN_CHANGES = 8
MIN_EMPIRICAL = 10
WINDOW = {12: 240, 4: 120, 1: 60}
AR_WINDOW = {12: 120, 4: 80, 1: 40}
AR_PMAX = {12: 6, 4: 4, 1: 2}
AR_MIN = {12: 36, 4: 24, 1: 15}


@dataclass(frozen=True)
class Abstain:
    reason: str


@dataclass(frozen=True)
class Predictive:
    """Predictive distribution of the target value (in the question's unit)."""

    family: Family
    point: float
    mu: float | None = None
    sigma: float | None = None
    sample: tuple[float, ...] = field(default=())
    detail: tuple[tuple[str, str], ...] = field(default=())

    def __post_init__(self) -> None:
        if not math.isfinite(self.point):
            raise ValueError("point must be finite")
        if self.family == "normal":
            if self.mu is None or self.sigma is None or not (math.isfinite(self.mu) and self.sigma > 0):
                raise ValueError("a normal predictive needs finite mu and sigma > 0")
        elif self.family == "empirical":
            if len(self.sample) < 2 or not all(math.isfinite(v) for v in self.sample):
                raise ValueError("an empirical predictive needs >= 2 finite sample values")
            if list(self.sample) != sorted(self.sample):
                raise ValueError("empirical sample must be sorted")
        elif self.family != "point":
            raise ValueError(f"unknown family {self.family!r}")

    @property
    def distributional(self) -> bool:
        return self.family != "point"

    def cdf(self, x: float) -> float | None:
        if self.family == "normal":
            assert self.mu is not None and self.sigma is not None
            return 0.5 * math.erfc(-(x - self.mu) / (self.sigma * math.sqrt(2.0)))
        if self.family == "empirical":
            return bisect.bisect_right(self.sample, x) / len(self.sample)
        return None

    def quantile(self, p: float) -> float | None:
        if not 0.0 < p < 1.0:
            raise ValueError("p must be in (0, 1)")
        if self.family == "normal":
            assert self.mu is not None and self.sigma is not None
            return self.mu + self.sigma * _norm_ppf(p)
        if self.family == "empirical":
            return float(np.quantile(np.asarray(self.sample), p))
        return None

    def interval(self, level: float) -> tuple[float, float] | None:
        a = (1.0 - level) / 2.0
        lo, hi = self.quantile(a), self.quantile(1.0 - a)
        return None if lo is None or hi is None else (lo, hi)

    def prob_above(self, threshold: float) -> float | None:
        """P(Y > threshold). Empirical: rule of succession (k + 1) / (n + 2)."""
        if self.family == "normal":
            c = self.cdf(threshold)
            assert c is not None
            return 1.0 - c
        if self.family == "empirical":
            n = len(self.sample)
            k = n - bisect.bisect_right(self.sample, threshold)
            return (k + 1.0) / (n + 2.0)
        return None


# --------------------------------------------------------------------------- helpers


def _norm_ppf(p: float) -> float:
    """Inverse standard normal CDF (Acklam's rational approximation, |error| < 1.2e-9 after one Newton step)."""
    a = (-3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02, 1.383577518672690e02,
         -3.066479806614716e01, 2.506628277459239e00)  # fmt: skip
    b = (-5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02, 6.680131188771972e01,
         -1.328068155288572e01)  # fmt: skip
    c = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00, -2.549732539343734e00,
         4.374664141464968e00, 2.938163982698783e00)  # fmt: skip
    d = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00, 3.754408661907416e00)
    lo = 0.02425
    if p < lo:
        q = math.sqrt(-2 * math.log(p))
        x = (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1
        )
    elif p <= 1 - lo:
        q = p - 0.5
        r = q * q
        x = (
            (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5])
            * q
            / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)
        )
    else:
        q = math.sqrt(-2 * math.log(1 - p))
        x = -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1
        )
    e = 0.5 * math.erfc(-x / math.sqrt(2.0)) - p
    u = e * math.sqrt(2 * math.pi) * math.exp(x * x / 2)
    return x - u / (1 + x * u / 2)


def final_run(history: Sequence[tuple[int, float]], window: int) -> list[tuple[int, float]]:
    """The last consecutive run of ``(period_index, value)`` (sorted input), capped to ``window`` periods."""
    if not history:
        return []
    idx = [p for p, _ in history]
    if idx != sorted(idx) or len(set(idx)) != len(idx):
        raise ValueError("history must be strictly increasing in period index")
    start = len(history) - 1
    while start > 0 and history[start - 1][0] == history[start][0] - 1:
        start -= 1
    run = list(history[start:])
    for _, v in run:
        if not math.isfinite(v):
            raise ValueError("history values must be finite")
    return run[-window:]


def _horizon(run: Sequence[tuple[int, float]], target: int) -> int | Abstain:
    if not run:
        return Abstain("no usable history")
    h = target - run[-1][0]
    if h < 1:
        return Abstain("target period is not after the last observed period")
    return h


def _changes(values: Sequence[float], h: int) -> list[float]:
    return [values[i + h] - values[i] for i in range(len(values) - h)]


# --------------------------------------------------------------------------- baselines


def persistence(history: Sequence[tuple[int, float]], target: int, season: int) -> Predictive | Abstain:
    run = final_run(history, WINDOW.get(season, 60))
    h = _horizon(run, target)
    if isinstance(h, Abstain):
        return h
    vals = [v for _, v in run]
    d = _changes(vals, h)
    if len(d) < MIN_CHANGES:
        return Abstain(f"fewer than {MIN_CHANGES} historical {h}-step changes in the final consecutive run")
    sigma = math.sqrt(math.fsum(x * x for x in d) / len(d))
    if sigma <= 0.0:
        return Abstain("degenerate spread (all historical changes are zero)")
    last = vals[-1]
    return Predictive("normal", last, mu=last, sigma=sigma, detail=(("h", str(h)), ("n_changes", str(len(d)))))


def seasonal_naive(history: Sequence[tuple[int, float]], target: int, season: int) -> Predictive | Abstain:
    if season <= 1:
        return Abstain("seasonal naive needs a monthly or quarterly series")
    run = final_run(history, WINDOW.get(season, 60))
    h = _horizon(run, target)
    if isinstance(h, Abstain):
        return h
    lag = season * math.ceil(h / season)
    vals = [v for _, v in run]
    ref = len(vals) - 1 + h - lag
    if ref < 0:
        return Abstain("history shorter than one seasonal lag")
    errs = [vals[i] - vals[i - lag] for i in range(lag, len(vals))]
    if len(errs) < MIN_CHANGES:
        return Abstain(f"fewer than {MIN_CHANGES} historical seasonal-naive errors at lag {lag}")
    sigma = math.sqrt(math.fsum(x * x for x in errs) / len(errs))
    if sigma <= 0.0:
        return Abstain("degenerate spread (all seasonal-naive errors are zero)")
    point = vals[ref]
    return Predictive(
        "normal", point, mu=point, sigma=sigma, detail=(("h", str(h)), ("lag", str(lag)), ("n_errors", str(len(errs))))
    )


def _ols_ar(y: np.ndarray, p: int, start: int) -> tuple[np.ndarray, float, int]:
    """AR(p) with intercept fitted on y[start:] (regressors from y[start-p:]); returns (coef, rss, n)."""
    n = len(y) - start
    x = np.ones((n, p + 1))
    for j in range(1, p + 1):
        x[:, j] = y[start - j : len(y) - j]
    coef, *_ = np.linalg.lstsq(x, y[start:], rcond=None)
    resid = y[start:] - x @ coef
    return coef, float(resid @ resid), n


def ar(history: Sequence[tuple[int, float]], target: int, season: int) -> Predictive | Abstain:
    run = final_run(history, AR_WINDOW.get(season, 40))
    h = _horizon(run, target)
    if isinstance(h, Abstain):
        return h
    y = np.asarray([v for _, v in run], dtype=float)
    pmax = AR_PMAX.get(season, 2)
    if len(y) < AR_MIN.get(season, 15):
        return Abstain(f"history too short for AR(p<= {pmax}) ({len(y)} points)")
    best: tuple[float, int] | None = None
    for p in range(1, pmax + 1):
        _, rss, n = _ols_ar(y, p, pmax)
        if rss <= 0:
            return Abstain("degenerate AR fit (zero residuals)")
        aic = n * math.log(rss / n) + 2 * (p + 1)
        if best is None or aic < best[0]:
            best = (aic, p)
    assert best is not None
    p = best[1]
    coef, rss, n = _ols_ar(y, p, p)
    dof = n - p - 1
    if dof < 5:
        return Abstain("too few degrees of freedom for the AR fit")
    s2 = rss / dof
    phi = coef[1:]
    comp = np.zeros((p, p))
    comp[0, :] = phi
    if p > 1:
        comp[1:, :-1] = np.eye(p - 1)
    if float(np.max(np.abs(np.linalg.eigvals(comp)))) >= 1.0:
        return Abstain("non-stationary AR fit (companion root modulus >= 1)")
    hist = list(y[-p:])
    for _ in range(h):
        nxt = float(coef[0] + sum(phi[j] * hist[-1 - j] for j in range(p)))
        hist.append(nxt)
    point = hist[-1]
    psi = [1.0]
    for j in range(1, h):
        psi.append(sum(float(phi[i - 1]) * psi[j - i] for i in range(1, min(j, p) + 1)))
    sigma = math.sqrt(s2 * math.fsum(x * x for x in psi))
    if not (math.isfinite(point) and sigma > 0):
        return Abstain("non-finite AR forecast")
    return Predictive("normal", point, mu=point, sigma=sigma, detail=(("h", str(h)), ("p", str(p)), ("n_fit", str(n))))


def hist_quantile(history: Sequence[tuple[int, float]], target: int, season: int) -> Predictive | Abstain:
    run = final_run(history, WINDOW.get(season, 60))
    h = _horizon(run, target)
    if isinstance(h, Abstain):
        return h
    vals = [v for _, v in run]
    d = _changes(vals, h)
    if len(d) < MIN_EMPIRICAL:
        return Abstain(f"fewer than {MIN_EMPIRICAL} historical {h}-step changes in the final consecutive run")
    last = vals[-1]
    sample = tuple(sorted(last + x for x in d))
    if sample[0] == sample[-1]:
        return Abstain("degenerate spread (all historical changes are equal)")
    point = float(np.median(np.asarray(sample)))
    return Predictive("empirical", point, sample=sample, detail=(("h", str(h)), ("n_changes", str(len(d)))))


FORECASTERS = {
    "forecast.registry_persistence": persistence,
    "forecast.registry_seasonal_naive": seasonal_naive,
    "forecast.registry_ar": ar,
    "forecast.registry_hist_quantile": hist_quantile,
}


def run_baseline(
    model_key: str, history: Sequence[tuple[int, float]], target: int, season: int
) -> Predictive | Abstain:
    fn = FORECASTERS.get(model_key)
    if fn is None:
        raise KeyError(f"unknown registry baseline {model_key!r}")
    return fn(history, target, season)


__all__ = [
    "BASELINES",
    "FORECASTERS",
    "Abstain",
    "Predictive",
    "ar",
    "final_run",
    "hist_quantile",
    "persistence",
    "run_baseline",
    "seasonal_naive",
]
