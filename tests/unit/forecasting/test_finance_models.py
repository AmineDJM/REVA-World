"""Finance benchmarks and candidates (V6 PRED-FIN): pure numerics, no look-ahead, honest abstentions."""

from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np
import pytest

from reva.models.forecasting import finance_models as F
from reva.models.forecasting.registry_baselines import Abstain, Predictive


def weekdays(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def rw_series(n: int, sigma: float, x0: float, *, log: bool, seed: int = 0) -> list[tuple[date, float]]:
    rng = np.random.default_rng(seed)
    days = weekdays(date(2015, 1, 1), n)
    x = math.log(x0) if log else x0
    out = []
    for d in days:
        out.append((d, math.exp(x) if log else x))
        x += sigma * rng.standard_normal()
    return out


def test_calendar_helpers() -> None:
    assert F.last_weekday(2026, 10) == date(2026, 10, 30)
    assert F.last_weekday(2026, 5) == date(2026, 5, 29)  # 31 May 2026 is a Sunday
    assert F.weekdays_between(date(2026, 10, 16), date(2026, 10, 19)) == 1  # Fri -> Mon
    assert F.weekdays_between(date(2026, 10, 16), date(2026, 10, 16)) == 0
    assert F.weekdays_between(date(2026, 10, 19), date(2026, 10, 16)) == -1
    assert len(F.month_weekdays(2026, 10)) == 22
    t = F.Target("month_end", 2026, 11)
    assert t.days() == [date(2026, 11, 30)] and t.end == date(2026, 11, 30)
    assert isinstance(F.steps(date(2026, 11, 30), t), Abstain)  # target not after the origin


def test_target_variance_month_end_and_mean() -> None:
    v = [1.0] * 10
    assert F.target_variance(v, [10]) == pytest.approx(10.0)  # month end: sum of the step variances
    # mean of X_1, X_2: Var = Var(e1) + Var(e2)/4
    assert F.target_variance([1.0, 1.0], [1, 2]) == pytest.approx(1.25)
    with pytest.raises(ValueError):
        F.target_variance([1.0], [2])


def test_rw_histvol_level_and_log() -> None:
    s = rw_series(600, 0.05, 4.0, log=False)
    t = F.Target("month_end", 2017, 12)
    p = F.rw_histvol(s, t, "level")
    assert isinstance(p, Predictive) and p.family == "normal"
    assert p.point == s[-1][1]  # martingale point
    k = F.weekdays_between(s[-1][0], t.end)
    assert p.sigma == pytest.approx(0.05 * math.sqrt(k), rel=0.25)
    fx = rw_series(600, 0.006, 1.1, log=True)
    q = F.rw_histvol(fx, t, "log")
    assert isinstance(q, Predictive) and q.family == "empirical" and len(q.sample) == F.GRID
    assert q.point == pytest.approx(fx[-1][1])
    assert q.prob_above(fx[-1][1]) == pytest.approx(100 / 201)  # median at the last value
    assert isinstance(F.rw_histvol(s[:30], t, "level"), Abstain)  # < 60 daily changes


def test_garch_recovers_parameters() -> None:
    rng = np.random.default_rng(1)
    om, a, b = 1e-6, 0.06, 0.92
    s2, r = om / (1 - a - b), []
    for _ in range(2500):
        e = rng.standard_normal() * math.sqrt(s2)
        r.append(e)
        s2 = om + a * e * e + b * s2
    g = F.garch11_fit(r)
    assert isinstance(g, F.Garch)
    assert 0.02 < g.alpha < 0.12 and 0.85 < g.beta < 0.97 and g.persistence < 1
    v = g.step_vars(500)
    assert v[-1] == pytest.approx(g.omega / (1 - g.persistence), rel=0.05)  # converges to the long-run variance
    assert isinstance(F.garch11_fit(r[:100]), Abstain)


def test_uip_forward_sign_and_abstention() -> None:
    fx = rw_series(400, 0.005, 1.1, log=True)
    t = F.Target("month_end", 2016, 12)
    assert F.uip_drift(5.0, 1.0, fx[-1][0], t) > 0  # higher quote-currency rate -> more quote units per euro
    p = F.fx_forward_cip(fx, t, 5.0, 1.0)
    rw = F.rw_histvol(fx, t, "log")
    assert isinstance(p, Predictive) and isinstance(rw, Predictive) and p.point > rw.point
    assert isinstance(F.fx_forward_cip(fx, t, None, 1.0), Abstain)


def test_eh_forward_flat_and_sloped_curves() -> None:
    flat = {0.25: 3.0, 2.0: 3.0, 10.0: 3.0, 30.0: 3.0}
    assert F.eh_forward(flat, 1.0, 10.0) == pytest.approx(3.0)
    up = {0.25: 1.0, 1.0: 1.5, 2.0: 2.0, 10.0: 4.0, 30.0: 5.0}
    assert F.eh_forward(up, 1.0, 10.0) > F.curve_at(up, 10.0)  # type: ignore[operator]
    assert F.eh_forward({0.25: 1.0, 10.0: 4.0}, 1.0, 10.0) is None  # no tenor beyond 10 years


def test_nelson_siegel_factors_roundtrip() -> None:
    beta = np.asarray([4.0, -2.0, 1.0])
    ms = [0.25, 0.5, 1, 2, 3, 5, 7, 10, 20, 30]
    curve = {m: F.ns_yield(beta, m) for m in ms}
    got = F.ns_factors(curve)
    assert got is not None and np.allclose(got, beta, atol=1e-8)
    assert F.ns_factors({1.0: 1.0, 2.0: 2.0}) is None


def test_ppp_reversion_pulls_towards_the_mean() -> None:
    rng = np.random.default_rng(3)
    months = list(range(2000 * 12, 2020 * 12))
    q, spot = 0.0, []
    for m in months:  # stationary AR(1) real rate around 0 with equal price levels
        q = 0.97 * q + 0.02 * rng.standard_normal()
        spot.append((m, math.exp(q)))
    prices = {m: 100.0 for m in months}
    daily = rw_series(400, 0.004, 1.5, log=True)  # today far above the PPP mean (log 1.5 = 0.41)
    t = F.Target("month_end", daily[-1][0].year + 1, daily[-1][0].month)
    p = F.ppp_reversion(daily, t, spot, prices, prices)
    assert isinstance(p, Predictive) and p.point < daily[-1][1]
    assert 0.0 <= float(dict(p.detail)["rho"]) < 1.0
    assert isinstance(F.ppp_reversion(daily, t, spot[:50], prices, prices), Abstain)


def test_meeting_windows_have_no_look_ahead_and_succession() -> None:
    days = [date(2000, 1, 3) + timedelta(days=i) for i in range(365 * 10)]
    level, series = 2.0, []
    for d in days:
        if d.day == 15 and d.month in (3, 9):
            level += 0.25
        series.append((d, level))
    origin = date(2008, 6, 30)
    ws = F.windows(series, 42, origin)
    assert ws and all(w.end <= origin for w in ws)
    up = F.meeting_base_rate(ws, "up")
    k = sum(w.up for w in ws)
    assert isinstance(up, float) and up == pytest.approx((k + 1) / (len(ws) + 2))
    assert F.meeting_base_rate(ws, "down") == pytest.approx(1 / (len(ws) + 2))
    assert F.last_move(series, date(2008, 6, 30)) == 1
    assert isinstance(F.meeting_base_rate(ws[:5], "up"), Abstain)
    assert isinstance(F.taylor_logit(ws, None, 1, "up"), Abstain)


def test_taylor_logit_learns_the_sign() -> None:
    ws = []
    for i in range(200):
        x = -3.0 + 6.0 * (i % 20) / 19.0
        up = x > 0.5
        ws.append(F.Window(date(2000, 1, 31), date(2000, 3, 13), 2.0, 2.25 if up else 2.0, 0, x))
    hi = F.taylor_logit(ws, 3.0, 0, "up")
    lo = F.taylor_logit(ws, -3.0, 0, "up")
    assert isinstance(hi, float) and isinstance(lo, float) and hi > 0.9 and lo < 0.1
    assert 0 < lo and hi < 1  # never certainty
