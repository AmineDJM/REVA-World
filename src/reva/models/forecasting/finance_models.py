"""Finance benchmarks and candidate models (V6, agent PRED-FIN). Pure numerics, no I/O.

Research forecasts for evaluation only: **not investment advice, not a recommendation, not a trading signal**
(:data:`DISCLAIMER`). The literature is blunt: exchange rates and bond yields are close to unpredictable beyond a
random walk at 1-12 month horizons (Meese & Rogoff 1983; Rossi 2013 survey; Duffee 2013 for yields); volatility is
the part that is forecastable (GARCH, Bollerslev 1986). Every model here is therefore scored *against* the random
walk; only a model that beats it with a significant Diebold-Mariano/HLN test after a Holm correction may issue
registry forecasts (``docs/dev/v6_pred_fin.md``).

Every forecaster sees one **daily** series ``[(day, value), ...]`` (strictly increasing days, published values
only — no row for a day the provider did not publish) known at the forecast origin, plus declared auxiliary inputs,
and returns a :class:`~reva.models.forecasting.registry_baselines.Predictive` for a :class:`Target` (the value on
the last weekday of a month, or the mean over the weekdays of a month) or an
:class:`~reva.models.forecasting.registry_baselines.Abstain` with the reason.

Scale: ``"log"`` series (FX rates, oil prices) move multiplicatively — the predictive is log-normal, stored as a
199-point quantile grid (``empirical`` family); ``"level"`` series (yields, spreads, policy rates) move additively —
the predictive is Gaussian (Bachelier).

Benchmarks (issued as baselines):

* ``fin.rw_histvol`` — random walk / martingale: point = latest published value; spread from the historical
  volatility of daily changes over the last 250 published days (zero mean), aggregated exactly over the target's
  weekdays (``target_variance``).
* ``fin.fx_forward_cip`` — UIP / covered-interest-parity forward *proxy*: expected log change = (policy rate of
  the quote currency − policy rate of the base currency) × time. Market forward points are licensed (not open), so
  central-bank policy rates stand in for money-market rates (labelled a proxy). Same spread as the random walk.
* ``fin.ust_eh_forward`` — expectations hypothesis: the forward yield implied by today's par curve (par yields
  treated as zero-coupon yields, linear interpolation in maturity — an approximation, stated). Same spread as the
  random walk.
* Policy meetings: ``fin.meeting_base_rate`` (share of historical windows of the same length that ended with a move
  in the asked direction; rule of succession) and ``fin.meeting_momentum`` (the same, conditional on the direction
  of the last move in the previous 365 days).

Candidates (issued only where they significantly beat the reference in the back-test):

* ``fin.garch11`` — random-walk point, Gaussian GARCH(1,1) daily variance (QMLE, variance targeting) aggregated over
  the target's weekdays.
* ``fin.ns_dl`` — dynamic Nelson-Siegel (Diebold & Li 2006, lambda = 0.0609 per month): factors fitted by OLS on
  each month-end curve, AR(1) per factor, forecast yield at the asked maturity; random-walk spread.
* ``fin.ppp_reversion`` — real exchange rate ``q = log S + log P_base − log P_quote`` mean-reverting (AR(1) on 20
  years of monthly data published at the origin) plus the trailing 12-month inflation differential; random-walk
  spread.
* ``fin.taylor_logit`` — Taylor-rule fundamentals for policy meetings: logistic regressions of "rate higher" /
  "rate lower" at the end of a window on ``1.5·inflation − rate`` (Taylor 1993 coefficients on inflation, no output
  gap: inflation-only, stated) and the last-move direction.
"""

from __future__ import annotations

import calendar
import math
from bisect import bisect_right
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

import numpy as np

from reva.models.forecasting.registry_baselines import Abstain, Predictive, _norm_ppf

DISCLAIMER = (
    "Research forecast for evaluation only: not investment advice, not a recommendation to buy, sell or hold any "
    "asset, not a trading signal. Exchange rates, bond yields and oil prices are close to unpredictable beyond a "
    "random walk; every REVA finance forecast is scored against one."
)
DISCLAIMER_FR = (
    "Prévision de recherche, à des fins d'évaluation uniquement : ni un conseil en investissement, ni une "
    "recommandation d'achat, de vente ou de conservation d'un actif, ni un signal de trading. Les taux de change, "
    "les rendements obligataires et les prix du pétrole sont presque imprévisibles au-delà d'une marche aléatoire ; "
    "chaque prévision financière de REVA est notée contre elle."
)

Scale = Literal["log", "level"]
Daily = Sequence[tuple[date, float]]

#: model_key -> (version, kind, one-line description)
FIN_MODELS: dict[str, tuple[str, str, str]] = {
    "fin.rw_histvol": ("1.0.0", "benchmark", "random walk / martingale, historical-volatility spread (250 days)"),
    "fin.fx_forward_cip": ("1.0.0", "benchmark", "UIP / CIP forward proxy from policy rates, random-walk spread"),
    "fin.ust_eh_forward": ("1.0.0", "benchmark", "expectations-hypothesis forward yield from the par curve"),
    "fin.meeting_base_rate": ("1.0.0", "benchmark", "historical frequency of a move in the asked direction"),
    "fin.meeting_momentum": ("1.0.0", "benchmark", "frequency of a move given the direction of the last move"),
    "fin.garch11": ("1.0.0", "candidate", "random-walk point, GARCH(1,1) daily variance"),
    "fin.ns_dl": ("1.0.0", "candidate", "dynamic Nelson-Siegel (Diebold-Li), AR(1) factors"),
    "fin.ppp_reversion": ("1.0.0", "candidate", "PPP real-exchange-rate mean reversion + inflation differential"),
    "fin.taylor_logit": ("1.0.0", "candidate", "Taylor-rule fundamentals logit (inflation-only) for meetings"),
}

VOL_WINDOW = 250
MIN_VOL_CHANGES = 60
GARCH_WINDOW = 2500
GARCH_MIN = 500
GRID = 199
NS_LAMBDA = 0.0609
NS_WINDOW = 120
PPP_WINDOW = 240
MEETING_YEARS = 25
MIN_WINDOWS = 24


# --------------------------------------------------------------------------- calendar helpers


def last_weekday(year: int, month: int) -> date:
    d = date(year, month, calendar.monthrange(year, month)[1])
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def month_weekdays(year: int, month: int) -> list[date]:
    n = calendar.monthrange(year, month)[1]
    return [date(year, month, i) for i in range(1, n + 1) if date(year, month, i).weekday() < 5]


def weekdays_between(d0: date, d1: date) -> int:
    """Number of weekdays in ``(d0, d1]`` (negative when ``d1 < d0``)."""
    if d1 < d0:
        return -weekdays_between(d1, d0)
    days = (d1 - d0).days
    full, rem = divmod(days, 7)
    n = full * 5
    for i in range(1, rem + 1):
        if (d0 + timedelta(days=full * 7 + i)).weekday() < 5:
            n += 1
    return n


@dataclass(frozen=True)
class Target:
    """What a question resolves on: the value on the last weekday of a month, or the mean over its weekdays."""

    kind: Literal["month_end", "month_mean"]
    year: int
    month: int

    def days(self) -> list[date]:
        if self.kind == "month_end":
            return [last_weekday(self.year, self.month)]
        return month_weekdays(self.year, self.month)

    @property
    def end(self) -> date:
        return last_weekday(self.year, self.month)


def steps(origin: date, target: Target) -> list[int] | Abstain:
    ks = [weekdays_between(origin, d) for d in target.days()]
    if not ks or min(ks) < 1:
        return Abstain("target month not entirely after the last published day")
    return ks


def target_variance(step_vars: Sequence[float], ks: Sequence[int]) -> float:
    """Variance of the mean of ``X_k`` over ``ks`` when ``X_k = X_0 + e_1 + ... + e_k`` with independent
    increments of variances ``step_vars[i-1]``: ``sum_i v_i (c_i / m)^2``, ``c_i = #{k in ks: k >= i}``."""
    m = len(ks)
    kmax = max(ks)
    if len(step_vars) < kmax:
        raise ValueError("step_vars shorter than the longest step")
    srt = sorted(ks)
    tot = 0.0
    for i in range(1, kmax + 1):
        c = m - bisect_right(srt, i - 1)
        tot += step_vars[i - 1] * (c / m) ** 2
    return tot


def daily_changes(series: Daily, scale: Scale, window: int) -> np.ndarray:
    vals = np.asarray([v for _, v in series[-(window + 1) :]], dtype=float)
    if scale == "log":
        if np.any(vals <= 0):
            return np.asarray([], dtype=float)
        return np.diff(np.log(vals))
    return np.diff(vals)


def predictive(x0: float, drift: float, var: float, scale: Scale, detail: Mapping[str, str]) -> Predictive | Abstain:
    """Gaussian (level) or log-normal quantile grid (log) predictive around ``x0`` shifted by ``drift``."""
    if not (math.isfinite(var) and var > 0 and math.isfinite(drift) and math.isfinite(x0)):
        return Abstain("non-finite or degenerate predictive")
    det = tuple(sorted((k, str(v)) for k, v in detail.items()))
    s = math.sqrt(var)
    if scale == "level":
        mu = x0 + drift
        return Predictive("normal", mu, mu=mu, sigma=s, detail=det)
    if x0 <= 0:
        return Abstain("log scale needs a positive last value")
    m = math.log(x0) + drift
    sample = tuple(math.exp(m + s * _norm_ppf((i + 0.5) / GRID)) for i in range(GRID))
    return Predictive("empirical", math.exp(m), sample=sample, detail=det)


# --------------------------------------------------------------------------- random walk + historical volatility


def hist_vol_var(series: Daily, scale: Scale, window: int = VOL_WINDOW) -> float | Abstain:
    d = daily_changes(series, scale, window)
    if d.size < MIN_VOL_CHANGES:
        return Abstain(f"fewer than {MIN_VOL_CHANGES} daily changes for the historical volatility")
    v = float(np.mean(d * d))
    if v <= 0:
        return Abstain("zero historical volatility")
    return v


def rw_histvol(series: Daily, target: Target, scale: Scale, drift: float = 0.0, extra: Mapping[str, str] | None = None
               ) -> Predictive | Abstain:  # fmt: skip
    if not series:
        return Abstain("no usable history")
    ks = steps(series[-1][0], target)
    if isinstance(ks, Abstain):
        return ks
    v = hist_vol_var(series, scale)
    if isinstance(v, Abstain):
        return v
    var = target_variance([v] * max(ks), ks)
    det = {"daily_var": f"{v:.6g}", "steps": str(max(ks)), **(extra or {})}
    return predictive(series[-1][1], drift, var, scale, det)


# --------------------------------------------------------------------------- GARCH(1,1)


@dataclass(frozen=True)
class Garch:
    omega: float
    alpha: float
    beta: float
    sig2_next: float

    @property
    def persistence(self) -> float:
        return self.alpha + self.beta

    def step_vars(self, n: int) -> list[float]:
        p = self.persistence
        lr = self.omega / (1.0 - p)
        return [lr + p**k * (self.sig2_next - lr) for k in range(n)]


def _garch_filter(x2: np.ndarray, omega: float, alpha: float, beta: float, s0: float) -> np.ndarray:
    """Conditional variances ``s2[t]`` (variance of day t given days < t), ``s2[0] = s0``; length ``n + 1`` (the
    last entry is the one-step-ahead variance). Linear recursion evaluated with ``scipy.signal.lfilter``."""
    from scipy.signal import lfilter

    u = np.empty(x2.size + 1)
    u[0] = s0
    u[1:] = omega + alpha * x2
    zi = np.asarray([0.0])
    out, _ = lfilter([1.0], [1.0, -beta], u, zi=zi)
    return np.asarray(out, dtype=float)


def garch11_fit(r: Sequence[float] | np.ndarray) -> Garch | Abstain:
    """Gaussian QMLE of a zero-mean GARCH(1,1) with variance targeting (omega = (1 - a - b) * sample variance)."""
    from scipy.optimize import minimize

    x = np.asarray(r, dtype=float)
    if x.size < GARCH_MIN:
        return Abstain(f"fewer than {GARCH_MIN} daily changes for GARCH")
    var = float(np.mean(x * x))
    if not var > 0:
        return Abstain("zero variance")
    x2 = x * x

    def nll(theta: np.ndarray) -> float:
        a, b = float(theta[0]), float(theta[1])
        if a < 0 or b < 0 or a + b >= 0.9999:
            return 1e300
        s2 = _garch_filter(x2, (1.0 - a - b) * var, a, b, var)[:-1]
        if np.any(s2 <= 0):
            return 1e300
        return float(0.5 * np.sum(np.log(s2) + x2 / s2))

    best = None
    for a0, b0 in ((0.05, 0.90), (0.10, 0.85), (0.03, 0.96)):
        res = minimize(nll, np.asarray([a0, b0]), method="Nelder-Mead",
                       options={"xatol": 1e-5, "fatol": 1e-6, "maxiter": 400})  # fmt: skip
        if best is None or res.fun < best.fun:
            best = res
    assert best is not None
    a, b = float(best.x[0]), float(best.x[1])
    if not (a >= 0 and b >= 0 and a + b < 0.9999) or best.fun >= 1e299:
        return Abstain("GARCH fit failed (no stationary optimum)")
    om = (1.0 - a - b) * var
    s2 = _garch_filter(x2, om, a, b, var)
    return Garch(om, a, b, float(s2[-1]))


def garch_forecast(series: Daily, target: Target, scale: Scale) -> Predictive | Abstain:
    if not series:
        return Abstain("no usable history")
    ks = steps(series[-1][0], target)
    if isinstance(ks, Abstain):
        return ks
    r = daily_changes(series, scale, GARCH_WINDOW)
    g = garch11_fit(r)
    if isinstance(g, Abstain):
        return g
    var = target_variance(g.step_vars(max(ks)), ks)
    det = {"alpha": f"{g.alpha:.4f}", "beta": f"{g.beta:.4f}", "sig2_next": f"{g.sig2_next:.6g}", "steps": str(max(ks))}
    return predictive(series[-1][1], 0.0, var, scale, det)


# --------------------------------------------------------------------------- UIP / CIP forward proxy


def uip_drift(i_quote: float, i_base: float, origin: date, target: Target) -> float:
    """Expected log change of S (quote units per base unit) under UIP: (i_quote - i_base) x time (ACT/365),
    averaged over the target days; rates in percent per year."""
    taus = [(d - origin).days / 365.0 for d in target.days()]
    return (i_quote - i_base) / 100.0 * (sum(taus) / len(taus))


def fx_forward_cip(series: Daily, target: Target, i_quote: float | None, i_base: float | None) -> Predictive | Abstain:
    if i_quote is None or i_base is None:
        return Abstain("policy rate of one of the two currencies not published at the origin")
    if not series:
        return Abstain("no usable history")
    drift = uip_drift(i_quote, i_base, series[-1][0], target)
    return rw_histvol(series, target, "log", drift, {"i_quote": f"{i_quote:g}", "i_base": f"{i_base:g}"})


# --------------------------------------------------------------------------- yield curve: EH forward and Nelson-Siegel

#: UST par-curve tenor labels -> maturity in years
UST_TENORS: dict[str, float] = {
    "1 Mo": 1 / 12, "1.5 Month": 1.5 / 12, "2 Mo": 2 / 12, "3 Mo": 0.25, "4 Mo": 4 / 12, "6 Mo": 0.5,
    "1 Yr": 1.0, "2 Yr": 2.0, "3 Yr": 3.0, "5 Yr": 5.0, "7 Yr": 7.0, "10 Yr": 10.0, "20 Yr": 20.0, "30 Yr": 30.0,
}  # fmt: skip


def curve_at(curve: Mapping[float, float], m: float) -> float | None:
    """Linear interpolation of a yield curve {maturity_years: yield} (no extrapolation beyond the ends, except a
    flat short end below the shortest tenor)."""
    ms = sorted(curve)
    if not ms or m > ms[-1] + 1e-9:
        return None
    if m <= ms[0]:
        return curve[ms[0]]
    i = bisect_right(ms, m)
    a, b = ms[i - 1], ms[min(i, len(ms) - 1)]
    if b == a:
        return curve[a]
    w = (m - a) / (b - a)
    return curve[a] * (1 - w) + curve[b] * w


def eh_forward(curve: Mapping[float, float], start: float, maturity: float) -> float | None:
    """Forward yield for ``maturity`` years starting in ``start`` years, par yields read as zero-coupon yields:
    ``(y(start + M)(start + M) - y(start) start) / M`` (continuous-compounding approximation)."""
    y_long = curve_at(curve, start + maturity)
    y_short = curve_at(curve, start) if start > 0 else 0.0
    if y_long is None or y_short is None:
        return None
    return (y_long * (start + maturity) - y_short * start) / maturity


def eh_point(curve: Mapping[float, float], origin: date, target: Target, maturity: float,
             minus_maturity: float | None = None) -> float | None:  # fmt: skip
    """EH forecast of the yield (or of the spread ``y(maturity) - y(minus_maturity)``) averaged over target days."""
    vals = []
    for d in target.days():
        s = max((d - origin).days, 0) / 365.0
        f = eh_forward(curve, s, maturity)
        if f is None:
            return None
        if minus_maturity is not None:
            g = eh_forward(curve, s, minus_maturity)
            if g is None:
                return None
            f -= g
        vals.append(f)
    return sum(vals) / len(vals)


def ust_eh_forward(series: Daily, target: Target, curve: Mapping[float, float] | None, maturity: float,
                   minus_maturity: float | None = None) -> Predictive | Abstain:  # fmt: skip
    if not series:
        return Abstain("no usable history")
    if not curve:
        return Abstain("no par curve published on the origin day")
    p = eh_point(curve, series[-1][0], target, maturity, minus_maturity)
    if p is None:
        return Abstain("par curve too short for the forward (long tenor missing)")
    return rw_histvol(series, target, "level", p - series[-1][1], {"eh_point": f"{p:.6g}"})


def ns_loadings(tau_months: np.ndarray, lam: float = NS_LAMBDA) -> np.ndarray:
    lt = lam * tau_months
    l1 = (1 - np.exp(-lt)) / lt
    return np.column_stack([np.ones_like(tau_months), l1, l1 - np.exp(-lt)])


def ns_factors(curve: Mapping[float, float]) -> np.ndarray | None:
    """OLS level/slope/curvature of one curve (maturities in years), Diebold-Li lambda; needs >= 6 tenors."""
    if len(curve) < 6:
        return None
    ms = sorted(curve)
    x = ns_loadings(np.asarray([m * 12.0 for m in ms]))
    y = np.asarray([curve[m] for m in ms])
    beta, *_ = np.linalg.lstsq(x, y, rcond=None)
    return np.asarray(beta, dtype=float)


def ns_yield(beta: np.ndarray, maturity_years: float) -> float:
    return float((ns_loadings(np.asarray([maturity_years * 12.0])) @ beta)[0])


def ns_dl_forecast(
    month_curves: Sequence[Mapping[float, float]],
    current: Mapping[float, float],
    series: Daily,
    target: Target,
    maturity: float,
    minus_maturity: float | None = None,
) -> Predictive | Abstain:
    """Dynamic Nelson-Siegel: AR(1) with intercept per factor on the month-end factor history (last 120 months),
    iterated ``h`` months from the current curve's factors; the forecast is the change of the fitted yield (or
    spread) added to the latest published value, so that a fitting error of the NS curve does not bias the point."""
    if not series:
        return Abstain("no usable history")
    fac = [f for f in (ns_factors(c) for c in month_curves[-NS_WINDOW:]) if f is not None]
    if len(fac) < 36:
        return Abstain("fewer than 36 month-end curves for the Nelson-Siegel factor dynamics")
    now = ns_factors(current)
    if now is None:
        return Abstain("current curve has fewer than 6 tenors")
    origin = series[-1][0]
    h = max(1, round((target.end - origin).days / 30.4375))
    f = np.asarray(fac)
    fc = now.copy()
    for j in range(3):
        y = f[1:, j]
        x = np.column_stack([np.ones(y.size), f[:-1, j]])
        (c, phi), *_ = np.linalg.lstsq(x, y, rcond=None)
        phi = float(np.clip(phi, -0.999, 0.999))
        v = float(now[j])
        for _ in range(h):
            v = float(c) + phi * v
        fc[j] = v

    def val(b: np.ndarray) -> float:
        y = ns_yield(b, maturity)
        return y - ns_yield(b, minus_maturity) if minus_maturity is not None else y

    drift = val(fc) - val(now)
    return rw_histvol(series, target, "level", drift, {"ns_h_months": str(h), "ns_change": f"{drift:.6g}"})


# --------------------------------------------------------------------------- PPP mean reversion


def ppp_reversion(
    series: Daily,
    target: Target,
    month_spot: Sequence[tuple[int, float]],
    p_base: Mapping[int, float],
    p_quote: Mapping[int, float],
) -> Predictive | Abstain:
    """``S`` = quote units per base unit; ``q = log S + log P_base - log P_quote`` (monthly, month index keys).
    AR(1) of ``q`` around its mean over the last 240 months with both price indices published; expected
    ``dlog S = (rho^h - 1)(q_now - qbar) + (pi_quote - pi_base) h/12`` with the trailing 12-month inflation
    differential (relative PPP). ``q_now`` uses the latest spot and the latest published price indices."""
    if not series:
        return Abstain("no usable history")
    common = sorted(set(p_base) & set(p_quote) & {i for i, _ in month_spot})
    spot = dict(month_spot)
    if len(common) < 120:
        return Abstain("fewer than 120 months with spot and both price indices")
    common = common[-PPP_WINDOW:]
    q = np.asarray([math.log(spot[i]) + math.log(p_base[i]) - math.log(p_quote[i]) for i in common])
    qbar = float(q.mean())
    y, x = q[1:] - qbar, q[:-1] - qbar
    rho = float(np.dot(x, y) / np.dot(x, x)) if np.dot(x, x) > 0 else 1.0
    rho = min(max(rho, 0.0), 0.999)
    last_p = common[-1]
    if last_p - 12 not in p_base or last_p - 12 not in p_quote:
        return Abstain("no price index 12 months before the latest published month")
    pi_b = math.log(p_base[last_p] / p_base[last_p - 12])
    pi_q = math.log(p_quote[last_p] / p_quote[last_p - 12])
    s_now = series[-1][1]
    q_now = math.log(s_now) + math.log(p_base[last_p]) - math.log(p_quote[last_p])
    origin = series[-1][0]
    h = max(1, round((target.end - origin).days / 30.4375))
    drift = (rho**h - 1.0) * (q_now - qbar) + (pi_q - pi_b) * h / 12.0
    return rw_histvol(series, target, "log", drift, {"rho": f"{rho:.4f}", "q_gap": f"{q_now - qbar:.4f}",
                                                       "h_months": str(h)})  # fmt: skip


# --------------------------------------------------------------------------- policy meetings


def level_on(series: Daily, d: date, days: Sequence[date] | None = None) -> float | None:
    """Level in force on day ``d``: the latest published value on or before ``d`` (None before the first day).
    ``days`` = the series' days, precomputed by callers that look up many dates."""
    i = bisect_right(days if days is not None else [x for x, _ in series], d)
    return series[i - 1][1] if i else None


@dataclass(frozen=True)
class Window:
    start: date
    end: date
    level0: float
    level1: float
    last_move: int  # +1 / -1 / 0: direction of the last change in the 365 days up to start
    x: float | None = None  # Taylor feature at start (None when inflation unpublished)

    @property
    def up(self) -> int:
        return 1 if self.level1 > self.level0 + 1e-9 else 0

    @property
    def down(self) -> int:
        return 1 if self.level1 < self.level0 - 1e-9 else 0


def last_move(series: Daily, d: date, lookback_days: int = 365, days: Sequence[date] | None = None) -> int:
    ds = days if days is not None else [x for x, _ in series]
    i = bisect_right(ds, d) - 1
    lo = d - timedelta(days=lookback_days)
    while i > 0 and series[i][0] > lo:
        if series[i][1] > series[i - 1][1] + 1e-9:
            return 1
        if series[i][1] < series[i - 1][1] - 1e-9:
            return -1
        i -= 1
    return 0


def all_windows(
    series: Daily, length_days: int, until: date, feature: Callable[[date, float], float | None] | None = None
) -> list[Window]:
    """Windows of ``length_days`` starting on each month's last weekday from the series' first month, ending on or
    before ``until``."""
    if not series:
        return []
    days = [x for x, _ in series]
    out: list[Window] = []
    y, m = days[0].year, days[0].month
    while True:
        s = last_weekday(y, m)
        e = s + timedelta(days=length_days)
        if e > until:
            break
        if s >= days[0]:
            a, b = level_on(series, s, days), level_on(series, e, days)
            if a is not None and b is not None:
                out.append(Window(s, e, a, b, last_move(series, s, days=days), feature(s, a) if feature else None))
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def windows_known_at(ws: Sequence[Window], origin: date, years: int = MEETING_YEARS) -> list[Window]:
    """The windows whose outcome is known at ``origin`` (end on or before it), over the last ``years`` years."""
    lo = date(origin.year - years, origin.month, 1)
    return [w for w in ws if w.end <= origin and w.start >= lo]


def windows(
    series: Daily,
    length_days: int,
    origin: date,
    *,
    years: int = MEETING_YEARS,
    feature: Callable[[date, float], float | None] | None = None,
) -> list[Window]:
    """Historical windows of ``length_days`` starting on each month's last weekday, **ending on or before the
    origin** (their outcome is known at the origin), over the last ``years`` years."""
    return windows_known_at(all_windows(series, length_days, origin, feature), origin, years)


def _succession(k: int, n: int) -> float:
    return (k + 1.0) / (n + 2.0)


def meeting_base_rate(ws: Sequence[Window], direction: Literal["up", "down"]) -> float | Abstain:
    if len(ws) < MIN_WINDOWS:
        return Abstain(f"fewer than {MIN_WINDOWS} historical windows")
    k = sum(w.up if direction == "up" else w.down for w in ws)
    return _succession(k, len(ws))


def meeting_momentum(ws: Sequence[Window], state: int, direction: Literal["up", "down"]) -> float | Abstain:
    same = [w for w in ws if w.last_move == state]
    if len(same) < 12:
        return Abstain("fewer than 12 historical windows in the same last-move state")
    k = sum(w.up if direction == "up" else w.down for w in same)
    return _succession(k, len(same))


def _logit_fit(x: np.ndarray, y: np.ndarray, ridge: float = 1e-2) -> np.ndarray | None:
    """Logistic regression by Newton-Raphson with a small ridge on the slopes (separation-safe)."""
    b = np.zeros(x.shape[1])
    pen = np.full(x.shape[1], ridge)
    pen[0] = 0.0
    for _ in range(50):
        p = 1.0 / (1.0 + np.exp(-(x @ b)))
        g = x.T @ (y - p) - pen * b
        w = p * (1 - p)
        hmat = (x * w[:, None]).T @ x + np.diag(pen) + 1e-9 * np.eye(x.shape[1])
        try:
            step = np.linalg.solve(hmat, g)
        except np.linalg.LinAlgError:
            return None
        b = b + step
        if float(np.max(np.abs(step))) < 1e-8:
            break
    return b if np.all(np.isfinite(b)) else None


def taylor_logit(ws: Sequence[Window], x_now: float | None, state: int, direction: Literal["up", "down"]
                 ) -> float | Abstain:  # fmt: skip
    """P(move in ``direction``) = logistic(a + b (1.5 pi - i) + c up_state + d down_state), fitted on the windows."""
    if x_now is None:
        return Abstain("inflation not published at the origin")
    tr = [w for w in ws if w.x is not None]
    if len(tr) < MIN_WINDOWS * 2:
        return Abstain(f"fewer than {MIN_WINDOWS * 2} windows with published inflation")
    yv = np.asarray([w.up if direction == "up" else w.down for w in tr], dtype=float)
    if yv.sum() < 3 or yv.sum() > len(yv) - 3:
        return Abstain("fewer than 3 events (or non-events) in the training windows")
    xm = np.asarray([[1.0, float(w.x), 1.0 if w.last_move > 0 else 0.0, 1.0 if w.last_move < 0 else 0.0]
                     for w in tr if w.x is not None])  # fmt: skip
    b = _logit_fit(xm, yv)
    if b is None:
        return Abstain("logit fit failed")
    z = float(b @ np.asarray([1.0, x_now, 1.0 if state > 0 else 0.0, 1.0 if state < 0 else 0.0]))
    p = 1.0 / (1.0 + math.exp(-max(min(z, 30.0), -30.0)))
    n = len(tr)
    return min(max(p, 1.0 / (n + 2.0)), 1.0 - 1.0 / (n + 2.0))


__all__ = [
    "DISCLAIMER",
    "DISCLAIMER_FR",
    "FIN_MODELS",
    "UST_TENORS",
    "Garch",
    "Target",
    "Window",
    "all_windows",
    "curve_at",
    "eh_forward",
    "eh_point",
    "fx_forward_cip",
    "garch11_fit",
    "garch_forecast",
    "hist_vol_var",
    "last_move",
    "last_weekday",
    "level_on",
    "meeting_base_rate",
    "meeting_momentum",
    "month_weekdays",
    "ns_dl_forecast",
    "ns_factors",
    "ppp_reversion",
    "predictive",
    "rw_histvol",
    "steps",
    "target_variance",
    "taylor_logit",
    "uip_drift",
    "ust_eh_forward",
    "weekdays_between",
    "windows",
    "windows_known_at",
]
