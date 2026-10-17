"""Energy forecasters benchmarked against the EIA Short-Term Energy Outlook (V6, agent PRED-ENERGY). Pure numerics.

Every forecaster sees one **information set**: the monthly series *as published at the forecast origin* (a STEO
issue's own history columns plus older months from earlier issues, read through the vintage accessor) and, for
the STEO-based forecasters, the projection cells of the issue in force. Nothing here reads data or the clock.

Target families (EIA STEO series codes):

* prices — ``BREPUUS`` (Brent spot), ``WTIPUUS`` (WTI spot), ``NGHHUUS`` (Henry Hub spot): modelled on logs; the
  predictive distribution is log-normal;
* quantities — ``COPRPUS`` (U.S. crude oil production), ``PAPR_WORLD`` (world liquids production), ``COPR_WORLD``
  (world crude oil production), ``PASC_OECD_T3`` (OECD commercial inventories): modelled on levels; Gaussian.

Forecasters (``k`` = months from the last month of the information set to the target):

* ``rw`` — random walk (no change), the benchmark; spread ``sigma_1 * sqrt(k)`` with ``sigma_1`` the standard
  deviation of monthly (log) changes over the last 60 months. **Futures-free**: no futures curve is used (no open
  licensed futures settlement series is loaded in REVA).
* ``steo`` — EIA's projection for the target month in the issue in force: point only, **attributed to EIA**.
* ``inventory`` (prices) — direct ``k``-step regression of the log price change on the OECD commercial inventory
  gap (log stocks minus the mean of the same calendar month over the previous five years), OLS on the as-of history.
* ``ardl`` (prices) — direct ``k``-step regression on last month's and last quarter's log price change, the 3-month
  log change of OECD stocks and the 3-month mean of EIA's implied world stock change (``T3_STCHANGE_WORLD``).
* ``drift`` (quantities) — last value plus ``k`` times the mean monthly change of the last 24 months.
* ``seasonal`` (quantities) — last value plus the mean ``k``-step change from the same calendar month in the last
  five years.
* ``ar_diff`` (quantities) — AR(p) on monthly changes (p <= 3 by AIC, last 120 months), iterated; psi-weight variance.
* ``combo_steo_rw`` — equal-weight average of the STEO projection and the random walk (logs for prices); spread of
  the random walk. A REVA combination of EIA's published number, not an EIA forecast.
* ``combo_models`` — equal-weight average of the REVA models available (and ``rw``); mean spread.

A forecaster that lacks an input abstains with a reason (never a filled-in value).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np

PRICE_CODES = ("BREPUUS", "WTIPUUS", "NGHHUUS")
QUANTITY_CODES = ("COPRPUS", "PAPR_WORLD", "COPR_WORLD", "PASC_OECD_T3")
TARGET_CODES = PRICE_CODES + QUANTITY_CODES
INVENTORY = "PASC_OECD_T3"
STOCK_CHANGE = "T3_STCHANGE_WORLD"
INPUT_CODES = (*TARGET_CODES, STOCK_CHANGE)
BENCHMARK = "rw"
REFERENCE = "steo"
PRICE_MODELS = ("rw", "steo", "inventory", "ardl", "combo_steo_rw", "combo_models")
QUANTITY_MODELS = ("rw", "steo", "drift", "seasonal", "ar_diff", "combo_steo_rw", "combo_models")
SIGMA_WINDOW = 60
REG_WINDOW = 120
MIN_REG = 36

#: model -> (version, kind, description); registered in ``registry/models/methods.yaml`` as ``forecast.energy_<m>``.
ENERGY_MODELS: dict[str, tuple[str, str, str]] = {
    "forecast.energy_rw": ("1.0.0", "baseline", "random walk on the STEO information set, futures-free"),
    "forecast.reference_eia_steo": ("1.0.0", "reference", "EIA STEO projection of the issue in force (attributed)"),
    "forecast.energy_inventory": ("1.0.0", "candidate", "direct k-step log price change on the OECD inventory gap"),
    "forecast.energy_ardl": ("1.0.0", "candidate", "direct k-step ARDL: price momentum, OECD stocks, world balance"),
    "forecast.energy_drift": ("1.0.0", "candidate", "random walk with 24-month drift (levels)"),
    "forecast.energy_seasonal": ("1.0.0", "candidate", "last value + mean same-month k-step change (5 years)"),
    "forecast.energy_ar_diff": ("1.0.0", "candidate", "AR(p) on monthly changes, iterated"),
    "forecast.energy_combo_steo_rw": ("1.0.0", "candidate", "equal-weight STEO + random walk (REVA combination)"),
    "forecast.energy_combo_models": ("1.0.0", "candidate", "equal-weight average of REVA energy models"),
}
#: FIX7 (QA-PRED X3 / §3.D): candidates never issued to the registry and never "winners", whatever their CRPS.
NOT_ISSUABLE: dict[str, str] = {
    "combo_steo_rw": "EIA-derived: half of it is EIA's own STEO projection, and it is worse than STEO alone on point "
    "loss (U.S. crude h4 MAE 0.445 vs STEO 0.337) - a win over the random walk dominated by the attributed reference",
}
MODEL_KEYS: dict[str, str] = {
    "rw": "forecast.energy_rw",
    "steo": "forecast.reference_eia_steo",
    "inventory": "forecast.energy_inventory",
    "ardl": "forecast.energy_ardl",
    "drift": "forecast.energy_drift",
    "seasonal": "forecast.energy_seasonal",
    "ar_diff": "forecast.energy_ar_diff",
    "combo_steo_rw": "forecast.energy_combo_steo_rw",
    "combo_models": "forecast.energy_combo_models",
}


@dataclass(frozen=True)
class Pred:
    """Predictive of the target value: ``lognormal`` (mu, sigma of the log), ``normal`` or ``point``."""

    dist: str
    point: float
    mu: float | None = None
    sigma: float | None = None

    def quantile(self, p: float) -> float | None:
        from reva.models.forecasting.registry_baselines import _norm_ppf

        if self.dist == "point" or self.mu is None or self.sigma is None:
            return None
        z = _norm_ppf(p)
        return math.exp(self.mu + self.sigma * z) if self.dist == "lognormal" else self.mu + self.sigma * z


@dataclass(frozen=True)
class Abstain:
    reason: str


Series = Mapping[int, float]  # month index (year*12 + month-1) -> value


def kind_of(code: str) -> str:
    if code in PRICE_CODES:
        return "price"
    if code in QUANTITY_CODES:
        return "quantity"
    raise ValueError(f"not an energy target code: {code}")


def models_for(code: str) -> tuple[str, ...]:
    return PRICE_MODELS if kind_of(code) == "price" else QUANTITY_MODELS


def run(series: Sequence[float], window: int) -> list[float]:
    return list(series[-window:])


def consecutive(s: Series, last: int, n: int) -> list[float] | None:
    """Values of months ``last-n+1 .. last`` (all present), else None."""
    out = []
    for i in range(last - n + 1, last + 1):
        v = s.get(i)
        if v is None or not math.isfinite(v):
            return None
        out.append(v)
    return out


def _usable_run(s: Series, last: int, cap: int) -> list[tuple[int, float]]:
    out: list[tuple[int, float]] = []
    i = last
    while i in s and len(out) < cap:
        out.append((i, s[i]))
        i -= 1
    return out[::-1]


def _log_series(s: Series) -> dict[int, float] | None:
    if any(v <= 0 for v in s.values()):
        return None
    return {i: math.log(v) for i, v in s.items()}


def sigma1(x: Series, last: int) -> float | Abstain:
    r = _usable_run(x, last, SIGMA_WINDOW + 1)
    if len(r) < 25:
        return Abstain("fewer than 24 monthly changes in the information set")
    d = np.diff([v for _, v in r])
    s = float(np.std(d, ddof=1))
    return s if s > 0 else Abstain("degenerate spread")


def _ols(x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, float] | None:
    n, p = x.shape
    if n < max(MIN_REG, p + 10):
        return None
    beta, *_ = np.linalg.lstsq(x, y, rcond=None)
    res = y - x @ beta
    s2 = float(res @ res) / (n - p)
    return beta, math.sqrt(s2) if s2 > 0 else 0.0


def _direct(y_of: dict[int, float], feats: dict[int, list[float]], last: int, k: int) -> tuple[float, float] | None:
    """Direct k-step regression y(t)=L[t+k]-L[t] on features(t), trained on t+k <= last (last REG_WINDOW)."""
    ts = [t for t in sorted(feats) if t + k <= last and t in y_of and (t + k) in y_of and t > last - REG_WINDOW - k]
    if last not in feats:
        return None
    rows = [[1.0, *feats[t]] for t in ts]
    if not rows:
        return None
    ys = [y_of[t + k] - y_of[t] for t in ts]
    fit = _ols(np.asarray(rows), np.asarray(ys))
    if fit is None:
        return None
    beta, s = fit
    if s <= 0:
        return None
    return float(np.asarray([1.0, *feats[last]]) @ beta), s


def inventory_gap(inv: Series, t: int) -> float | None:
    if t not in inv or inv[t] <= 0:
        return None
    past = [inv.get(t - 12 * j) for j in range(1, 6)]
    if any(v is None or v <= 0 for v in past):
        return None
    return math.log(inv[t]) - sum(math.log(v) for v in past if v is not None) / 5.0


def _ar_diff(x: Series, last: int, k: int) -> tuple[float, float] | None:
    r = _usable_run(x, last, REG_WINDOW + 1)
    if len(r) < 48:
        return None
    d = np.diff([v for _, v in r])
    best: tuple[float, int, np.ndarray, float] | None = None
    pmax = 3
    n_common = len(d) - pmax
    for p in range(1, pmax + 1):
        y = d[pmax:]
        X = np.column_stack([np.ones(n_common)] + [d[pmax - j : len(d) - j] for j in range(1, p + 1)])
        beta, *_ = np.linalg.lstsq(X, y, rcond=None)
        res = y - X @ beta
        s2 = float(res @ res) / (n_common - p - 1)
        aic = n_common * math.log(max(float(res @ res) / n_common, 1e-300)) + 2 * (p + 1)
        if best is None or aic < best[0]:
            best = (aic, p, beta, s2)
    assert best is not None
    _, p, beta, s2 = best
    phi = beta[1:]
    if np.any(np.abs(np.roots(np.r_[1.0, -phi])) >= 1.0):
        return None
    hist = list(d[-p:])
    cum = 0.0
    for _ in range(k):
        nxt = float(beta[0] + sum(phi[j] * hist[-1 - j] for j in range(p)))
        hist.append(nxt)
        cum += nxt
    psi = [1.0]
    for j in range(1, k):
        psi.append(sum(phi[i] * psi[j - 1 - i] for i in range(min(p, j))))
    cpsi = np.cumsum(psi)
    var = s2 * float(np.sum(cpsi**2))
    return r[-1][1] + cum, math.sqrt(var)


def forecast(
    model: str,
    code: str,
    info: Mapping[str, Series],
    steo: Mapping[str, Series],
    last: int,
    target: int,
) -> Pred | Abstain:
    """One forecaster's predictive for ``code`` at month ``target`` from information set ``info`` ending ``last``."""
    k = target - last
    if k < 1:
        return Abstain("target is not after the last month of the information set")
    x = info.get(code) or {}
    if last not in x:
        return Abstain("no value for the last month of the information set")
    price = kind_of(code) == "price"
    if model == "steo":
        v = (steo.get(code) or {}).get(target)
        if v is None:
            return Abstain("no STEO projection for the target month in the issue in force")
        return Pred("point", v)
    lx = _log_series(x) if price else dict(x)
    if lx is None:
        return Abstain("non-positive price in the information set")
    s1 = sigma1(lx, last)
    if isinstance(s1, Abstain):
        return s1
    rw_sigma = s1 * math.sqrt(k)

    def make(mu: float, sigma: float) -> Pred:
        if price:
            return Pred("lognormal", math.exp(mu), mu, sigma)
        return Pred("normal", mu, mu, sigma)

    if model == "rw":
        return make(lx[last], rw_sigma)
    if model == "combo_steo_rw":
        v = (steo.get(code) or {}).get(target)
        if v is None:
            return Abstain("no STEO projection for the target month")
        if price and v <= 0:
            return Abstain("non-positive STEO price")
        return make(0.5 * (math.log(v) if price else v) + 0.5 * lx[last], rw_sigma)
    if model == "combo_models":
        parts = [forecast(m, code, info, steo, last, target) for m in models_for(code) if m not in COMBOS_EXCLUDED]
        ok = [p for p in parts if isinstance(p, Pred) and p.mu is not None and p.sigma is not None]
        if len(ok) < 2:
            return Abstain("fewer than two component models")
        return make(sum(p.mu for p in ok if p.mu is not None) / len(ok), sum(p.sigma or 0.0 for p in ok) / len(ok))
    if price:
        inv = info.get(INVENTORY) or {}
        if model == "inventory":
            feats = {t: [g] for t in lx if (g := inventory_gap(inv, t)) is not None}
            got = _direct(lx, feats, last, k)
            if got is None:
                return Abstain("inventory regression: too few as-of observations (needs 5 years of OECD stocks)")
            return make(lx[last] + got[0], got[1])
        if model == "ardl":
            sc = info.get(STOCK_CHANGE) or {}
            feats = {}
            for t in lx:
                need = (t - 1 in lx, t - 3 in lx, t in inv, t - 3 in inv, t in sc, t - 1 in sc, t - 2 in sc)
                if not all(need) or inv[t] <= 0 or inv[t - 3] <= 0:
                    continue
                feats[t] = [
                    lx[t] - lx[t - 1],
                    lx[t] - lx[t - 3],
                    math.log(inv[t]) - math.log(inv[t - 3]),
                    (sc[t] + sc[t - 1] + sc[t - 2]) / 3.0,
                ]
            got = _direct(lx, feats, last, k)
            if got is None:
                return Abstain("ARDL: too few as-of observations with every regressor")
            return make(lx[last] + got[0], got[1])
        return Abstain(f"model {model} does not forecast prices")
    if model == "drift":
        r = consecutive(lx, last, 25)
        if r is None:
            return Abstain("drift: fewer than 24 consecutive monthly changes")
        return make(lx[last] + k * (r[-1] - r[0]) / 24.0, rw_sigma)
    if model == "seasonal":
        ch = []
        for j in range(1, 6):
            t0 = last - 12 * j
            if t0 in lx and t0 + k in lx and t0 + k <= last:
                ch.append(lx[t0 + k] - lx[t0])
        if len(ch) < 3:
            return Abstain("seasonal: fewer than 3 same-month k-step changes")
        return make(lx[last] + sum(ch) / len(ch), rw_sigma)
    if model == "ar_diff":
        got = _ar_diff(lx, last, k)
        if got is None:
            return Abstain("AR on changes: short history or non-stationary fit")
        return make(*got)
    return Abstain(f"model {model} does not forecast quantities")


COMBOS_EXCLUDED = ("steo", "combo_steo_rw", "combo_models")


# --------------------------------------------------------------------------- scores


def _phi(z: float) -> float:
    return math.exp(-0.5 * z * z) / math.sqrt(2 * math.pi)


def _Phi(z: float) -> float:
    return 0.5 * math.erfc(-z / math.sqrt(2.0))


def crps(p: Pred, y: float) -> float:
    """CRPS: closed forms (Gaussian; log-normal, Baran & Lerch 2015); a point forecast's CRPS is |x - y|."""
    if p.dist == "point" or p.mu is None or p.sigma is None:
        return abs(p.point - y)
    m, s = p.mu, p.sigma
    if p.dist == "normal":
        z = (y - m) / s
        return s * (z * (2 * _Phi(z) - 1) + 2 * _phi(z) - 1 / math.sqrt(math.pi))
    if y <= 0:
        return math.exp(m + s * s / 2) - y  # all mass above y
    z = (math.log(y) - m) / s
    return y * (2 * _Phi(z) - 1) - 2 * math.exp(m + s * s / 2) * (_Phi(z - s) + _Phi(s / math.sqrt(2)) - 1)


def log_score(p: Pred, y: float) -> float | None:
    if p.dist == "point" or p.mu is None or p.sigma is None:
        return None
    m, s = p.mu, p.sigma
    if p.dist == "normal":
        return -0.5 * math.log(2 * math.pi * s * s) - (y - m) ** 2 / (2 * s * s)
    if y <= 0:
        return None
    ly = math.log(y)
    return -ly - 0.5 * math.log(2 * math.pi * s * s) - (ly - m) ** 2 / (2 * s * s)


def pit(p: Pred, y: float) -> float | None:
    if p.dist == "point" or p.mu is None or p.sigma is None:
        return None
    if p.dist == "normal":
        return _Phi((y - p.mu) / p.sigma)
    return _Phi((math.log(y) - p.mu) / p.sigma) if y > 0 else 0.0


__all__ = [
    "BENCHMARK",
    "ENERGY_MODELS",
    "INPUT_CODES",
    "MODEL_KEYS",
    "NOT_ISSUABLE",
    "PRICE_CODES",
    "QUANTITY_CODES",
    "REFERENCE",
    "TARGET_CODES",
    "Abstain",
    "Pred",
    "crps",
    "forecast",
    "kind_of",
    "log_score",
    "models_for",
    "pit",
]
